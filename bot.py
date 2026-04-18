"""
Telegram bot handler for VOL-SNIPER.
Provides /start, /settings, /status, /set commands.
Runs alongside the scanner.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

log = logging.getLogger("vol-sniper-bot")
CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ─── Command Handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    await update.message.reply_text(
        "🔫 <b>VOL-SNIPER Bot</b>\n\n"
        "Commands:\n"
        "/status — current config & state\n"
        "/settings — interactive settings menu\n"
        "/set &lt;param&gt; &lt;value&gt; — quick set a parameter\n"
        "/chains — manage chains\n"
        "/help — show all commands\n\n"
        f"Your chat ID: <code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    f = cfg["filters"]
    await update.message.reply_text(
        "📊 <b>Current Configuration</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Chains: <code>{', '.join(cfg['chains'])}</code>\n"
        f"⏱ Scan interval: <code>{cfg['scan_interval_seconds']}s</code>\n"
        f"🔇 Cooldown: <code>{cfg['cooldown_minutes']} min</code>\n"
        f"📋 Pairs/chain: <code>{cfg.get('top_pairs_per_chain', 50)}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Filters:</b>\n"
        f"  📊 Vol ratio: <code>×{f['volume_ratio_min']}</code>\n"
        f"  📈 Price Δ min: <code>{f['price_change_pct_min']}%</code>\n"
        f"  💵 Min 15m vol: <code>${f['min_15m_volume_usd']:,}</code>\n"
        f"  📉 ATR filter: <code>{'ON' if f['atr_4h_below_median'] else 'OFF'}</code>\n"
        f"  🕐 ATR lookback: <code>{f['atr_lookback_candles']} candles</code>\n",
        parse_mode=ParseMode.HTML,
    )


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📊 Vol Ratio", callback_data="edit_volume_ratio_min"),
            InlineKeyboardButton("📈 Price Δ%", callback_data="edit_price_change_pct_min"),
        ],
        [
            InlineKeyboardButton("💵 Min Volume", callback_data="edit_min_15m_volume_usd"),
            InlineKeyboardButton("📉 ATR Filter", callback_data="toggle_atr"),
        ],
        [
            InlineKeyboardButton("⏱ Scan Interval", callback_data="edit_scan_interval"),
            InlineKeyboardButton("🔇 Cooldown", callback_data="edit_cooldown"),
        ],
        [
            InlineKeyboardButton("📡 Chains", callback_data="show_chains"),
        ],
    ]
    await update.message.reply_text(
        "⚙️ <b>Settings</b> — tap to adjust:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    cfg = load_config()

    if data == "toggle_atr":
        cfg["filters"]["atr_4h_below_median"] = not cfg["filters"]["atr_4h_below_median"]
        save_config(cfg)
        state = "ON" if cfg["filters"]["atr_4h_below_median"] else "OFF"
        await query.edit_message_text(
            f"📉 ATR filter is now <b>{state}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "show_chains":
        chain_options = ["solana", "ethereum", "base", "bsc"]
        keyboard = []
        for chain in chain_options:
            active = "✅" if chain in cfg["chains"] else "⬜"
            keyboard.append([
                InlineKeyboardButton(
                    f"{active} {chain}",
                    callback_data=f"chain_toggle_{chain}",
                )
            ])
        keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="back_settings")])
        await query.edit_message_text(
            "📡 <b>Chains</b> — tap to toggle:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("chain_toggle_"):
        chain = data.replace("chain_toggle_", "")
        if chain in cfg["chains"]:
            if len(cfg["chains"]) > 1:
                cfg["chains"].remove(chain)
            else:
                await query.answer("Need at least one chain!", show_alert=True)
                return
        else:
            cfg["chains"].append(chain)
        save_config(cfg)
        # Re-render chains menu
        chain_options = ["solana", "ethereum", "base", "bsc"]
        keyboard = []
        for c in chain_options:
            active = "✅" if c in cfg["chains"] else "⬜"
            keyboard.append([
                InlineKeyboardButton(
                    f"{active} {c}",
                    callback_data=f"chain_toggle_{c}",
                )
            ])
        keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="back_settings")])
        await query.edit_message_text(
            "📡 <b>Chains</b> — tap to toggle:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "back_settings":
        keyboard = [
            [
                InlineKeyboardButton("📊 Vol Ratio", callback_data="edit_volume_ratio_min"),
                InlineKeyboardButton("📈 Price Δ%", callback_data="edit_price_change_pct_min"),
            ],
            [
                InlineKeyboardButton("💵 Min Volume", callback_data="edit_min_15m_volume_usd"),
                InlineKeyboardButton("📉 ATR Filter", callback_data="toggle_atr"),
            ],
            [
                InlineKeyboardButton("⏱ Scan Interval", callback_data="edit_scan_interval"),
                InlineKeyboardButton("🔇 Cooldown", callback_data="edit_cooldown"),
            ],
            [
                InlineKeyboardButton("📡 Chains", callback_data="show_chains"),
            ],
        ]
        await query.edit_message_text(
            "⚙️ <b>Settings</b> — tap to adjust:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
        )
        return

    # For editable fields — prompt user to send a value
    field_labels = {
        "edit_volume_ratio_min": ("volume_ratio_min", "Vol Ratio multiplier (e.g. 10)"),
        "edit_price_change_pct_min": ("price_change_pct_min", "Price change % (e.g. 50)"),
        "edit_min_15m_volume_usd": ("min_15m_volume_usd", "Min 15m volume in USD (e.g. 20000)"),
        "edit_scan_interval": ("scan_interval_seconds", "Scan interval in seconds (e.g. 60)"),
        "edit_cooldown": ("cooldown_minutes", "Cooldown in minutes (e.g. 60)"),
    }

    if data in field_labels:
        field, label = field_labels[data]
        ctx.user_data["awaiting_setting"] = field
        current = cfg["filters"].get(field) or cfg.get(field)
        await query.edit_message_text(
            f"✏️ <b>{label}</b>\n"
            f"Current value: <code>{current}</code>\n\n"
            f"Send the new value:",
            parse_mode=ParseMode.HTML,
        )


async def handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle text messages — used for setting values after /settings prompt."""
    if "awaiting_setting" not in ctx.user_data:
        return

    field = ctx.user_data.pop("awaiting_setting")
    raw = update.message.text.strip()

    try:
        value = float(raw)
        if field in ("min_15m_volume_usd", "scan_interval_seconds", "cooldown_minutes"):
            value = int(value)
    except ValueError:
        await update.message.reply_text("❌ Invalid number. Try again.")
        ctx.user_data["awaiting_setting"] = field
        return

    cfg = load_config()
    if field in cfg["filters"]:
        cfg["filters"][field] = value
    else:
        cfg[field] = value
    save_config(cfg)

    await update.message.reply_text(
        f"✅ <b>{field}</b> set to <code>{value}</code>\n"
        f"Changes take effect on next scan cycle.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Quick set: /set volume_ratio_min 15"""
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: /set &lt;param&gt; &lt;value&gt;\n\n"
            "Params:\n"
            "<code>volume_ratio_min</code> — e.g. 10\n"
            "<code>price_change_pct_min</code> — e.g. 50\n"
            "<code>min_15m_volume_usd</code> — e.g. 20000\n"
            "<code>atr_4h_below_median</code> — true/false\n"
            "<code>atr_lookback_candles</code> — e.g. 16\n"
            "<code>scan_interval_seconds</code> — e.g. 60\n"
            "<code>cooldown_minutes</code> — e.g. 60\n"
            "<code>top_pairs_per_chain</code> — e.g. 50\n",
            parse_mode=ParseMode.HTML,
        )
        return

    param = args[1]
    raw_val = args[2]

    cfg = load_config()

    # Determine where the param lives
    filter_params = {
        "volume_ratio_min", "price_change_pct_min",
        "min_15m_volume_usd", "atr_4h_below_median", "atr_lookback_candles",
    }

    try:
        if param == "atr_4h_below_median":
            value = raw_val.lower() in ("true", "1", "yes", "on")
        else:
            value = float(raw_val)
            if param in ("min_15m_volume_usd", "scan_interval_seconds",
                         "cooldown_minutes", "top_pairs_per_chain", "atr_lookback_candles"):
                value = int(value)
    except ValueError:
        await update.message.reply_text("❌ Invalid value.")
        return

    if param in filter_params:
        cfg["filters"][param] = value
    elif param in cfg:
        cfg[param] = value
    else:
        await update.message.reply_text(f"❌ Unknown parameter: {param}")
        return

    save_config(cfg)
    await update.message.reply_text(
        f"✅ <code>{param}</code> → <code>{value}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔫 <b>VOL-SNIPER Commands</b>\n\n"
        "/start — welcome message\n"
        "/status — show current config\n"
        "/settings — interactive settings menu\n"
        "/set &lt;param&gt; &lt;value&gt; — quick-set a parameter\n"
        "/chains — manage chains\n"
        "/help — this message\n",
        parse_mode=ParseMode.HTML,
    )


async def cmd_chains(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    chain_options = ["solana", "ethereum", "base", "bsc"]
    keyboard = []
    for chain in chain_options:
        active = "✅" if chain in cfg["chains"] else "⬜"
        keyboard.append([
            InlineKeyboardButton(
                f"{active} {chain}",
                callback_data=f"chain_toggle_{chain}",
            )
        ])
    await update.message.reply_text(
        "📡 <b>Chains</b> — tap to toggle:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


# ─── Main ──────────────────────────────────────────────────────────────────────

def run_bot():
    cfg = load_config()
    token = cfg["telegram_bot_token"]
    if not token:
        log.error("Set telegram_bot_token in config.json!")
        sys.exit(1)

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("chains", cmd_chains))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    log.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    run_bot()
