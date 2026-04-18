"""
VOL-SNIPER: Early volume spike detector for crypto tokens.
Monitors DexScreener for abnormal volume surges on SOL/ETH/BASE/BNB chains.
Sends Telegram alerts when all filter conditions are met.
"""

import asyncio
import json
import time
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from telegram import Bot
from telegram.constants import ParseMode

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("vol_sniper.log"),
    ],
)
log = logging.getLogger("vol-sniper")

# ─── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "chains": ["solana", "ethereum", "base", "bsc"],
    "scan_interval_seconds": 60,
    "filters": {
        "volume_ratio_min": 10.0,        # current 15m vol / 20-period MA vol
        "price_change_pct_min": 50.0,    # % move vs 20-period MA price
        "min_15m_volume_usd": 20000,     # minimum absolute vol in current candle
        "atr_4h_below_median": True,     # require low prior volatility
        "atr_lookback_candles": 16,      # 16 × 15min = 4 hours
    },
    "cooldown_minutes": 60,              # don't re-alert same token within N min
    "top_pairs_per_chain": 50,           # how many pairs to scan per chain
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        # merge defaults for any missing keys
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
            elif isinstance(v, dict):
                for kk, vv in v.items():
                    if kk not in cfg[k]:
                        cfg[k][kk] = vv
        return cfg
    else:
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log.warning(f"Created default config at {CONFIG_PATH}. Edit it and restart.")
        sys.exit(1)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ─── DexScreener API ──────────────────────────────────────────────────────────

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_PAIRS_URL = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_address}"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_CHAIN_PAIRS = "https://api.dexscreener.com/latest/dex/pairs/{chain_id}"

# We use the token search endpoint to discover active pairs
DEXSCREENER_TOKEN_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={query}"

# Screener endpoint - gets trending/top pairs per chain
DEXSCREENER_SCREENER = "https://api.dexscreener.com/orders/v1/{chain_id}"


async def fetch_trending_pairs(client: httpx.AsyncClient, chain: str, limit: int = 50) -> list:
    """
    Fetch pairs from DexScreener for a given chain.
    Uses the search endpoint with chain filter.
    """
    chain_map = {
        "solana": "solana",
        "ethereum": "ethereum",
        "base": "base",
        "bsc": "bsc",
    }
    chain_id = chain_map.get(chain, chain)

    # Strategy: search for trending tokens on the chain
    # DexScreener's /latest/dex/search endpoint can filter by chain
    url = f"https://api.dexscreener.com/latest/dex/search/?q=chain:{chain_id}"

    try:
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", [])
        # Filter to only our chain
        pairs = [p for p in pairs if p.get("chainId") == chain_id]
        return pairs[:limit]
    except Exception as e:
        log.warning(f"Search endpoint failed for {chain}: {e}. Trying boosted tokens...")

    # Fallback: use token-boosts endpoint
    try:
        resp = await client.get(DEXSCREENER_SEARCH_URL, timeout=15)
        resp.raise_for_status()
        tokens = resp.json()
        # Filter by chain
        chain_tokens = [t for t in tokens if t.get("chainId") == chain_id]
        # Fetch pair data for each token
        all_pairs = []
        for token in chain_tokens[:20]:
            token_addr = token.get("tokenAddress", "")
            if not token_addr:
                continue
            try:
                pair_resp = await client.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
                    timeout=10,
                )
                pair_resp.raise_for_status()
                pair_data = pair_resp.json()
                for p in pair_data.get("pairs", []):
                    if p.get("chainId") == chain_id:
                        all_pairs.append(p)
            except Exception:
                continue
            await asyncio.sleep(0.3)  # rate limiting
        return all_pairs[:limit]
    except Exception as e:
        log.error(f"Failed to fetch pairs for {chain}: {e}")
        return []


async def fetch_pair_ohlcv(client: httpx.AsyncClient, pair_address: str, chain: str) -> list | None:
    """
    Fetch OHLCV candle data for a pair.
    DexScreener doesn't have a direct candle API, so we use pair info
    which includes volume and price data.
    
    Returns the pair data dict with volume/price info, or None.
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_address}"
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", []) if isinstance(data, dict) else data
        if pairs:
            return pairs[0] if isinstance(pairs, list) else pairs
    except Exception as e:
        log.debug(f"Failed to fetch pair {pair_address}: {e}")
    return None


# ─── Signal Analysis ──────────────────────────────────────────────────────────

class TokenHistory:
    """Tracks rolling 15m volume/price history for a token pair."""

    def __init__(self, max_candles: int = 30):
        self.max_candles = max_candles
        self.volumes: list[float] = []
        self.prices: list[float] = []
        self.timestamps: list[float] = []
        self.highs: list[float] = []
        self.lows: list[float] = []

    def add_candle(self, volume: float, price: float, high: float, low: float, ts: float):
        self.volumes.append(volume)
        self.prices.append(price)
        self.highs.append(high)
        self.lows.append(low)
        self.timestamps.append(ts)
        # trim
        if len(self.volumes) > self.max_candles:
            self.volumes = self.volumes[-self.max_candles:]
            self.prices = self.prices[-self.max_candles:]
            self.highs = self.highs[-self.max_candles:]
            self.lows = self.lows[-self.max_candles:]
            self.timestamps = self.timestamps[-self.max_candles:]

    def volume_ma(self, period: int = 20) -> float | None:
        # MA of volumes EXCLUDING the current (last) candle
        if len(self.volumes) < period + 1:
            return None
        historical = self.volumes[-(period + 1):-1]
        return sum(historical) / len(historical)

    def price_ma(self, period: int = 20) -> float | None:
        if len(self.prices) < period + 1:
            return None
        historical = self.prices[-(period + 1):-1]
        return sum(historical) / len(historical)

    def atr(self, period: int = 16) -> float | None:
        """Average True Range over last N candles (excluding current)."""
        if len(self.highs) < period + 1:
            return None
        trs = []
        for i in range(-(period + 1), -1):
            tr = self.highs[i] - self.lows[i]
            trs.append(tr)
        return sum(trs) / len(trs) if trs else None

    def current_volume(self) -> float:
        return self.volumes[-1] if self.volumes else 0

    def current_price(self) -> float:
        return self.prices[-1] if self.prices else 0


def evaluate_signal(
    pair_data: dict,
    history: TokenHistory,
    filters: dict,
) -> dict | None:
    """
    Evaluate whether a token pair triggers a signal.
    Returns signal dict if all conditions met, else None.
    """
    # Extract current data from pair
    try:
        current_vol_5m = float(pair_data.get("volume", {}).get("m5", 0) or 0)
        current_vol_1h = float(pair_data.get("volume", {}).get("h1", 0) or 0)
        current_vol_6h = float(pair_data.get("volume", {}).get("h6", 0) or 0)
        current_vol_24h = float(pair_data.get("volume", {}).get("h24", 0) or 0)

        # Estimate 15m volume from 5m × 3 (rough approximation)
        current_vol_15m = current_vol_5m * 3

        price_usd = float(pair_data.get("priceUsd", 0) or 0)
        price_change_5m = float(pair_data.get("priceChange", {}).get("m5", 0) or 0)
        price_change_1h = float(pair_data.get("priceChange", {}).get("h1", 0) or 0)
        price_change_6h = float(pair_data.get("priceChange", {}).get("h6", 0) or 0)
        price_change_24h = float(pair_data.get("priceChange", {}).get("h24", 0) or 0)

        fdv = float(pair_data.get("fdv", 0) or 0)
        mcap = float(pair_data.get("marketCap", 0) or 0)
        liquidity_usd = float(pair_data.get("liquidity", {}).get("usd", 0) or 0)

        # Use high/low from price changes to estimate range
        # Since DexScreener doesn't give candle-level OHLCV directly,
        # we approximate from available data
        high_est = price_usd * (1 + abs(price_change_5m) / 100)
        low_est = price_usd * (1 - abs(price_change_5m) / 100)

    except (TypeError, ValueError) as e:
        log.debug(f"Data parsing error: {e}")
        return None

    # Update history
    history.add_candle(
        volume=current_vol_15m,
        price=price_usd,
        high=high_est,
        low=low_est,
        ts=time.time(),
    )

    # ── Filter 1: Minimum absolute volume ──
    min_vol = filters.get("min_15m_volume_usd", 20000)
    if current_vol_15m < min_vol:
        return None

    # ── Filter 2: Volume ratio vs 20-period MA ──
    vol_ma = history.volume_ma(20)
    if vol_ma is None or vol_ma <= 0:
        # Not enough history yet — use 1h vs 6h ratio as proxy
        if current_vol_6h > 0:
            vol_ratio = (current_vol_1h * 6) / current_vol_6h
        else:
            return None
    else:
        vol_ratio = current_vol_15m / vol_ma

    vol_ratio_min = filters.get("volume_ratio_min", 10.0)
    if vol_ratio < vol_ratio_min:
        return None

    # ── Filter 3: Price change vs MA ──
    price_ma = history.price_ma(20)
    price_change_pct_min = filters.get("price_change_pct_min", 50.0)

    if price_ma and price_ma > 0:
        price_change_from_ma = ((price_usd - price_ma) / price_ma) * 100
    else:
        # Fallback: use DexScreener's reported price changes
        # Use max of available timeframes
        price_change_from_ma = max(abs(price_change_5m * 3), abs(price_change_1h))

    if abs(price_change_from_ma) < price_change_pct_min:
        return None

    # ── Filter 4 (optional): ATR below median (low prior volatility) ──
    atr_check_passed = True
    atr_val = None
    if filters.get("atr_4h_below_median", True):
        lookback = filters.get("atr_lookback_candles", 16)
        atr_val = history.atr(lookback)
        if atr_val is not None and price_usd > 0:
            atr_pct = (atr_val / price_usd) * 100
            # "Below median" = relatively low volatility before the spike
            # We consider ATR < 5% of price as "quiet"
            # This is a heuristic — adjust as needed
            if atr_pct > 10:
                atr_check_passed = False

    if not atr_check_passed:
        return None

    # ── All filters passed — generate signal ──
    token_name = pair_data.get("baseToken", {}).get("name", "???")
    token_symbol = pair_data.get("baseToken", {}).get("symbol", "???")
    pair_address = pair_data.get("pairAddress", "")
    chain_id = pair_data.get("chainId", "")
    dex_url = pair_data.get("url", f"https://dexscreener.com/{chain_id}/{pair_address}")

    signal = {
        "token_name": token_name,
        "token_symbol": token_symbol,
        "chain": chain_id,
        "pair_address": pair_address,
        "price_usd": price_usd,
        "volume_15m_est": current_vol_15m,
        "volume_1h": current_vol_1h,
        "volume_ratio": round(vol_ratio, 1),
        "price_change_from_ma": round(price_change_from_ma, 1),
        "price_change_5m": price_change_5m,
        "price_change_1h": price_change_1h,
        "fdv": fdv,
        "mcap": mcap,
        "liquidity_usd": liquidity_usd,
        "atr_pct": round((atr_val / price_usd) * 100, 2) if atr_val and price_usd else None,
        "dex_url": dex_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return signal


# ─── Telegram ─────────────────────────────────────────────────────────────────

def format_signal_message(signal: dict) -> str:
    """Format a signal dict into a Telegram message."""
    chain_emoji = {
        "solana": "◎",
        "ethereum": "⟠",
        "base": "🔵",
        "bsc": "🟡",
    }
    chain_icon = chain_emoji.get(signal["chain"], "🔗")

    direction = "🟢" if signal["price_change_from_ma"] > 0 else "🔴"

    # Format numbers nicely
    def fmt_usd(val):
        if val >= 1_000_000:
            return f"${val/1_000_000:.1f}M"
        elif val >= 1_000:
            return f"${val/1_000:.1f}K"
        else:
            return f"${val:.0f}"

    def fmt_price(val):
        if val >= 1:
            return f"${val:.2f}"
        elif val >= 0.001:
            return f"${val:.4f}"
        elif val >= 0.0000001:
            return f"${val:.8f}"
        else:
            return f"${val:.12f}"

    msg = (
        f"{direction} <b>VOL SPIKE</b> {chain_icon} <b>{signal['token_symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Price: <code>{fmt_price(signal['price_usd'])}</code>\n"
        f"📊 Vol 15m: <code>{fmt_usd(signal['volume_15m_est'])}</code> "
        f"(×{signal['volume_ratio']} vs avg)\n"
        f"📈 Price Δ from MA: <code>{signal['price_change_from_ma']:+.1f}%</code>\n"
        f"⚡ 5m: <code>{signal['price_change_5m']:+.1f}%</code> | "
        f"1h: <code>{signal['price_change_1h']:+.1f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 MCap: <code>{fmt_usd(signal['mcap'])}</code> | "
        f"Liq: <code>{fmt_usd(signal['liquidity_usd'])}</code>\n"
    )
    if signal.get("atr_pct") is not None:
        msg += f"📉 Prior ATR: <code>{signal['atr_pct']:.1f}%</code> (quiet)\n"
    msg += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <a href=\"{signal['dex_url']}\">DexScreener</a> | "
        f"{signal['chain'].upper()}\n"
        f"⏰ {signal['timestamp'][:19]}Z"
    )
    return msg


async def send_telegram_alert(bot: Bot, chat_id: str, signal: dict):
    """Send formatted signal to Telegram."""
    msg = format_signal_message(signal)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        log.info(f"📤 Alert sent: {signal['token_symbol']} on {signal['chain']}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# ─── Main Loop ─────────────────────────────────────────────────────────────────

async def run_scanner():
    cfg = load_config()

    if not cfg["telegram_bot_token"] or not cfg["telegram_chat_id"]:
        log.error("Set telegram_bot_token and telegram_chat_id in config.json!")
        sys.exit(1)

    bot = Bot(token=cfg["telegram_bot_token"])
    chat_id = cfg["telegram_chat_id"]

    # State
    histories: dict[str, TokenHistory] = {}  # pair_address -> TokenHistory
    cooldowns: dict[str, float] = {}         # pair_address -> last_alert_timestamp
    cooldown_sec = cfg.get("cooldown_minutes", 60) * 60

    log.info("=" * 50)
    log.info("VOL-SNIPER started")
    log.info(f"Chains: {cfg['chains']}")
    log.info(f"Filters: {json.dumps(cfg['filters'], indent=2)}")
    log.info(f"Scan interval: {cfg['scan_interval_seconds']}s")
    log.info("=" * 50)

    # Send startup message
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "🔫 <b>VOL-SNIPER online</b>\n\n"
                f"Chains: {', '.join(cfg['chains'])}\n"
                f"Vol ratio: ×{cfg['filters']['volume_ratio_min']}\n"
                f"Price Δ: {cfg['filters']['price_change_pct_min']}%\n"
                f"Min vol: ${cfg['filters']['min_15m_volume_usd']:,}\n"
                f"ATR filter: {'ON' if cfg['filters']['atr_4h_below_median'] else 'OFF'}\n"
                f"Scan interval: {cfg['scan_interval_seconds']}s"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"Failed to send startup message: {e}")

    async with httpx.AsyncClient(
        headers={"User-Agent": "vol-sniper/1.0"},
        follow_redirects=True,
    ) as client:
        while True:
            try:
                # Reload config each cycle to pick up changes
                cfg = load_config()
                filters = cfg["filters"]
                cooldown_sec = cfg.get("cooldown_minutes", 60) * 60

                for chain in cfg["chains"]:
                    log.info(f"Scanning {chain}...")
                    pairs = await fetch_trending_pairs(
                        client, chain, cfg.get("top_pairs_per_chain", 50)
                    )
                    log.info(f"  Found {len(pairs)} pairs on {chain}")

                    for pair in pairs:
                        pair_addr = pair.get("pairAddress", "")
                        if not pair_addr:
                            continue

                        # Check cooldown
                        now = time.time()
                        if pair_addr in cooldowns:
                            if now - cooldowns[pair_addr] < cooldown_sec:
                                continue

                        # Get or create history
                        if pair_addr not in histories:
                            histories[pair_addr] = TokenHistory(max_candles=30)

                        signal = evaluate_signal(pair, histories[pair_addr], filters)

                        if signal:
                            await send_telegram_alert(bot, chat_id, signal)
                            cooldowns[pair_addr] = now

                    await asyncio.sleep(1)  # rate limit between chains

                # Clean up old cooldowns and histories (memory management)
                now = time.time()
                expired = [k for k, v in cooldowns.items() if now - v > cooldown_sec * 3]
                for k in expired:
                    del cooldowns[k]
                    if k in histories:
                        del histories[k]

                log.info(
                    f"Cycle done. Tracking {len(histories)} pairs. "
                    f"Sleeping {cfg['scan_interval_seconds']}s..."
                )

            except Exception as e:
                log.error(f"Scanner error: {e}", exc_info=True)

            await asyncio.sleep(cfg.get("scan_interval_seconds", 60))


if __name__ == "__main__":
    asyncio.run(run_scanner())
