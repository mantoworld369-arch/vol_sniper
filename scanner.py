"""
VOL-SNIPER: Early volume spike detector for crypto tokens.
Uses GeckoTerminal API to monitor ALL pools on SOL/ETH/BASE/BNB chains.
Sends Telegram alerts when all filter conditions are met.

GeckoTerminal endpoints used:
  - /networks/{net}/trending_pools   — trending pools by visits + on-chain activity
  - /networks/{net}/new_pools        — newly created pools
  - /networks/{net}/pools            — top pools sorted by volume
  - /networks/{net}/pools/{addr}/ohlcv/minute?aggregate=15  — real 15m candles
"""

import asyncio
import json
import time
import logging
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
        "volume_ratio_min": 10.0,
        "price_change_pct_min": 50.0,
        "min_15m_volume_usd": 20000,
        "atr_4h_below_median": True,
        "atr_lookback_candles": 16,
    },
    "cooldown_minutes": 60,
    "pages_per_chain": 3,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
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


# ─── GeckoTerminal API ────────────────────────────────────────────────────────

GT_BASE = "https://api.geckoterminal.com/api/v2"

CHAIN_TO_NETWORK = {
    "solana": "solana",
    "ethereum": "eth",
    "base": "base",
    "bsc": "bsc",
}


async def _gt_get(client: httpx.AsyncClient, path: str, params: dict = None) -> dict | None:
    """Rate-limited GET to GeckoTerminal."""
    url = f"{GT_BASE}{path}"
    try:
        resp = await client.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("retry-after", 30))
            log.warning(f"GT rate limit, sleeping {retry_after}s...")
            await asyncio.sleep(retry_after)
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.debug(f"GT error [{path}]: {e}")
        return None


async def fetch_pools_for_chain(client: httpx.AsyncClient, chain: str, pages: int = 3) -> list:
    """
    Fetch pools from 3 GeckoTerminal sources:
      1. Trending pools — most active by visits + on-chain activity
      2. Top pools — sorted by 24h volume
      3. New pools — recently created
    Returns deduplicated pool list.
    """
    network = CHAIN_TO_NETWORK.get(chain)
    if not network:
        return []

    all_pools = []
    seen = set()

    def _add(data):
        if not data or "data" not in data:
            return 0
        added = 0
        for pool in data["data"]:
            addr = pool.get("attributes", {}).get("address", "")
            if addr and addr not in seen:
                seen.add(addr)
                all_pools.append(pool)
                added += 1
        return added

    # 1) Trending pools
    for page in range(1, pages + 1):
        data = await _gt_get(client, f"/networks/{network}/trending_pools", {"page": page})
        if _add(data) == 0:
            break
        await asyncio.sleep(2.1)

    # 2) Top pools by volume
    for page in range(1, pages + 1):
        data = await _gt_get(
            client, f"/networks/{network}/pools",
            {"page": page, "sort": "h24_volume_usd_desc"},
        )
        if _add(data) == 0:
            break
        await asyncio.sleep(2.1)

    # 3) New pools
    data = await _gt_get(client, f"/networks/{network}/new_pools")
    _add(data)
    await asyncio.sleep(2.1)

    return all_pools


async def fetch_ohlcv_15m(
    client: httpx.AsyncClient, chain: str, pool_address: str, limit: int = 25
) -> list | None:
    """Fetch 15m OHLCV candles. Returns [[ts, o, h, l, c, vol], ...] chronological."""
    network = CHAIN_TO_NETWORK.get(chain)
    if not network:
        return None
    data = await _gt_get(
        client,
        f"/networks/{network}/pools/{pool_address}/ohlcv/minute",
        {"aggregate": 15, "limit": limit, "currency": "usd"},
    )
    if not data:
        return None
    candles = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    return list(reversed(candles)) if candles else None


def parse_pool(pool: dict) -> dict | None:
    """Extract fields from GeckoTerminal pool object."""
    a = pool.get("attributes", {})
    if not a:
        return None
    try:
        name = a.get("name", "???") or "???"
        symbol = name.split("/")[0].strip() if "/" in name else name

        return {
            "address": a.get("address", ""),
            "name": name,
            "symbol": symbol,
            "price_usd": float(a.get("base_token_price_usd", 0) or 0),
            "vol_5m": float((a.get("volume_usd") or {}).get("m5", 0) or 0),
            "vol_1h": float((a.get("volume_usd") or {}).get("h1", 0) or 0),
            "vol_6h": float((a.get("volume_usd") or {}).get("h6", 0) or 0),
            "vol_24h": float((a.get("volume_usd") or {}).get("h24", 0) or 0),
            "pc_5m": float((a.get("price_change_percentage") or {}).get("m5", 0) or 0),
            "pc_1h": float((a.get("price_change_percentage") or {}).get("h1", 0) or 0),
            "fdv": float(a.get("fdv_usd", 0) or 0),
            "mcap": float(a.get("market_cap_usd", 0) or 0) or float(a.get("fdv_usd", 0) or 0),
            "liquidity": float(a.get("reserve_in_usd", 0) or 0),
        }
    except (TypeError, ValueError):
        return None


# ─── Signal Evaluation ────────────────────────────────────────────────────────

def evaluate_candles(candles: list, filters: dict) -> dict | None:
    """
    Evaluate 15m OHLCV candles for volume spike.
    candles = [[ts, open, high, low, close, volume], ...]  (chronological)
    """
    if not candles or len(candles) < 5:
        return None

    volumes = [c[5] for c in candles]
    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]

    cur_vol = volumes[-1]
    cur_price = closes[-1]

    # Filter 1: min absolute volume
    if cur_vol < filters.get("min_15m_volume_usd", 20000):
        return None

    # Filter 2: volume ratio vs MA (exclude current candle)
    hist_vols = volumes[:-1]
    ma_n = min(20, len(hist_vols))
    if ma_n < 3:
        return None
    vol_ma = sum(hist_vols[-ma_n:]) / ma_n
    vol_ratio = (cur_vol / vol_ma) if vol_ma > 0 else 999.0
    if vol_ratio < filters.get("volume_ratio_min", 10.0):
        return None

    # Filter 3: price change vs MA
    hist_prices = closes[:-1]
    price_ma_n = min(20, len(hist_prices))
    if price_ma_n < 3:
        return None
    price_ma = sum(hist_prices[-price_ma_n:]) / price_ma_n
    if price_ma <= 0:
        return None
    price_change = ((cur_price - price_ma) / price_ma) * 100
    if abs(price_change) < filters.get("price_change_pct_min", 50.0):
        return None

    # Filter 4: ATR (optional)
    atr_pct = None
    if filters.get("atr_4h_below_median", True):
        lookback = min(filters.get("atr_lookback_candles", 16), len(highs) - 1)
        if lookback >= 4:
            trs = [highs[i] - lows[i] for i in range(-(lookback + 1), -1)]
            atr_val = sum(trs) / len(trs)
            if price_ma > 0 and atr_val > 0:
                atr_pct = (atr_val / price_ma) * 100
                if atr_pct > 10:
                    return None

    return {
        "volume_15m": cur_vol,
        "volume_ratio": round(vol_ratio, 1),
        "price_change_from_ma": round(price_change, 1),
        "price_usd": cur_price,
        "atr_pct": round(atr_pct, 2) if atr_pct else None,
        "candle_count": len(candles),
    }


def prefilter(p: dict, filters: dict) -> bool:
    """Quick check on pool summary data before expensive OHLCV fetch."""
    min_vol = filters.get("min_15m_volume_usd", 20000)
    est_15m = max(p["vol_5m"] * 3, p["vol_1h"] / 4)
    if est_15m < min_vol * 0.5:
        return False
    price_min = filters.get("price_change_pct_min", 50.0)
    if max(abs(p["pc_5m"]) * 3, abs(p["pc_1h"])) < price_min * 0.3:
        return False
    return True


# ─── Telegram ─────────────────────────────────────────────────────────────────

def format_signal(sig: dict) -> str:
    chain_emoji = {"solana": "◎", "ethereum": "⟠", "base": "🔵", "bsc": "🟡"}
    icon = chain_emoji.get(sig["chain"], "🔗")
    arrow = "🟢" if sig["price_change_from_ma"] > 0 else "🔴"

    def fu(v):
        return f"${v/1e6:.1f}M" if v >= 1e6 else f"${v/1e3:.1f}K" if v >= 1e3 else f"${v:.0f}"

    def fp(v):
        if v >= 1: return f"${v:.2f}"
        if v >= 0.001: return f"${v:.4f}"
        if v >= 1e-7: return f"${v:.8f}"
        return f"${v:.12f}"

    net = CHAIN_TO_NETWORK.get(sig["chain"], sig["chain"])
    gt = f"https://www.geckoterminal.com/{net}/pools/{sig['pool_address']}"
    ds = f"https://dexscreener.com/{sig['chain']}/{sig['pool_address']}"

    msg = (
        f"{arrow} <b>VOL SPIKE</b> {icon} <b>{sig['token_symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Price: <code>{fp(sig['price_usd'])}</code>\n"
        f"📊 Vol 15m: <code>{fu(sig['volume_15m'])}</code> "
        f"(×{sig['volume_ratio']} vs avg)\n"
        f"📈 Price Δ from MA: <code>{sig['price_change_from_ma']:+.1f}%</code>\n"
        f"⚡ 5m: <code>{sig['pc_5m']:+.1f}%</code> | "
        f"1h: <code>{sig['pc_1h']:+.1f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 MCap: <code>{fu(sig['mcap'])}</code> | "
        f"Liq: <code>{fu(sig['liquidity'])}</code>\n"
    )
    if sig.get("atr_pct") is not None:
        msg += f"📉 Prior ATR: <code>{sig['atr_pct']:.1f}%</code> (quiet)\n"
    msg += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <a href=\"{gt}\">GeckoTerminal</a> | "
        f"<a href=\"{ds}\">DexScreener</a>\n"
        f"⏰ {sig['timestamp'][:19]}Z"
    )
    return msg


async def send_alert(bot: Bot, chat_id: str, sig: dict):
    try:
        await bot.send_message(
            chat_id=chat_id, text=format_signal(sig),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
        log.info(f"📤 Alert: {sig['token_symbol']} on {sig['chain']}")
    except Exception as e:
        log.error(f"TG send failed: {e}")


# ─── Main Loop ─────────────────────────────────────────────────────────────────

async def run_scanner():
    cfg = load_config()
    if not cfg["telegram_bot_token"] or not cfg["telegram_chat_id"]:
        log.error("Set telegram_bot_token and telegram_chat_id in config.json!")
        sys.exit(1)

    bot = Bot(token=cfg["telegram_bot_token"])
    chat_id = cfg["telegram_chat_id"]
    cooldowns: dict[str, float] = {}

    log.info("=" * 50)
    log.info("VOL-SNIPER started (GeckoTerminal)")
    log.info(f"Chains: {cfg['chains']}")
    log.info(f"Filters: {json.dumps(cfg['filters'], indent=2)}")
    log.info("=" * 50)

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "🔫 <b>VOL-SNIPER online</b> (GeckoTerminal)\n\n"
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
        log.error(f"Startup msg failed: {e}")

    async with httpx.AsyncClient(
        headers={"Accept": "application/json", "User-Agent": "vol-sniper/2.0"},
        follow_redirects=True,
    ) as client:
        while True:
            try:
                cfg = load_config()
                filters = cfg["filters"]
                cooldown_sec = cfg.get("cooldown_minutes", 60) * 60
                pages = cfg.get("pages_per_chain", 3)

                for chain in cfg["chains"]:
                    log.info(f"Scanning {chain}...")
                    pools = await fetch_pools_for_chain(client, chain, pages)
                    log.info(f"  [{chain}] {len(pools)} pools discovered")

                    candidates = 0
                    signals = 0

                    for pool in pools:
                        p = parse_pool(pool)
                        if not p:
                            continue

                        addr = p["address"]
                        now = time.time()
                        if addr in cooldowns and now - cooldowns[addr] < cooldown_sec:
                            continue
                        if not prefilter(p, filters):
                            continue

                        candidates += 1

                        candles = await fetch_ohlcv_15m(client, chain, addr, limit=25)
                        await asyncio.sleep(2.1)
                        if not candles:
                            continue

                        result = evaluate_candles(candles, filters)
                        if result:
                            sig = {
                                "token_symbol": p["symbol"],
                                "chain": chain,
                                "pool_address": addr,
                                "price_usd": result["price_usd"],
                                "volume_15m": result["volume_15m"],
                                "volume_ratio": result["volume_ratio"],
                                "price_change_from_ma": result["price_change_from_ma"],
                                "pc_5m": p["pc_5m"],
                                "pc_1h": p["pc_1h"],
                                "fdv": p["fdv"],
                                "mcap": p["mcap"],
                                "liquidity": p["liquidity"],
                                "atr_pct": result.get("atr_pct"),
                                "candle_count": result["candle_count"],
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                            await send_alert(bot, chat_id, sig)
                            cooldowns[addr] = now
                            signals += 1

                    log.info(f"  [{chain}] {candidates} candidates, {signals} signals")

                # Cleanup
                now = time.time()
                for k in [k for k, v in cooldowns.items() if now - v > cooldown_sec * 3]:
                    del cooldowns[k]

                log.info(f"Cycle done. Sleeping {cfg['scan_interval_seconds']}s...")

            except Exception as e:
                log.error(f"Scanner error: {e}", exc_info=True)

            await asyncio.sleep(cfg.get("scan_interval_seconds", 60))


if __name__ == "__main__":
    asyncio.run(run_scanner())
