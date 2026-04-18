"""
Main runner: launches scanner + Telegram bot concurrently.
"""

import asyncio
import logging
import sys
import multiprocessing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("vol_sniper.log"),
    ],
)


def run_bot_process():
    from bot import run_bot
    run_bot()


def run_scanner_process():
    from scanner import run_scanner
    asyncio.run(run_scanner())


if __name__ == "__main__":
    print("=" * 50)
    print("  VOL-SNIPER — starting scanner + bot")
    print("=" * 50)

    bot_proc = multiprocessing.Process(target=run_bot_process, name="tg-bot")
    scanner_proc = multiprocessing.Process(target=run_scanner_process, name="scanner")

    bot_proc.start()
    scanner_proc.start()

    try:
        bot_proc.join()
        scanner_proc.join()
    except KeyboardInterrupt:
        print("\nShutting down...")
        bot_proc.terminate()
        scanner_proc.terminate()
        bot_proc.join()
        scanner_proc.join()
        print("Done.")
