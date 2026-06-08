from __future__ import annotations

import asyncio
import logging
import sys

import aiohttp

from alerter import Alerter
from config import (
    HELIUS_API_KEY,
    PAPER_TRADE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WALLET_PRIVATE_KEY,
)
from gmgn_client import GMGNClient
from risk import RiskManager
from trader import Trader
from wallet_copier import WalletCopier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("gmgn-bot.main")


def _check_config() -> None:
    missing = []
    if not WALLET_PRIVATE_KEY or WALLET_PRIVATE_KEY.startswith("your_"):
        if not PAPER_TRADE:
            missing.append("WALLET_PRIVATE_KEY")
    if not HELIUS_API_KEY or HELIUS_API_KEY.startswith("your_"):
        missing.append("HELIUS_API_KEY (needed for wallet copy tracking)")
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("your_"):
        logger.warning("TELEGRAM_BOT_TOKEN not set — alerts disabled")
    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID not set — alerts disabled")
    if missing:
        logger.warning("Missing env vars: %s", ", ".join(missing))


async def main() -> None:
    _check_config()
    mode = "PAPER TRADE" if PAPER_TRADE else "LIVE TRADE"
    logger.info("=" * 55)
    logger.info("  GMGN Bot starting — %s", mode)
    logger.info("  Sources: trending | smart money | new tokens | wallet copy")
    logger.info("=" * 55)

    # Force Google DNS — avoids Railway DNS issues with gmgn.ai / jup.ag
    connector = aiohttp.TCPConnector(
        resolver=aiohttp.AsyncResolver(nameservers=["8.8.8.8", "1.1.1.1"]),
        ttl_dns_cache=300,
        limit=20,
    )
    async with aiohttp.ClientSession(connector=connector) as session:
        alerter      = Alerter(session)
        trader       = Trader(session)
        risk_manager = RiskManager(trader, alerter)
        await risk_manager.initialize()   # connects to PostgreSQL, loads open positions
        gmgn_client  = GMGNClient(session, trader, risk_manager, alerter)
        wallet_copier = WalletCopier(session, trader, risk_manager, alerter, gmgn_client)

        await alerter.send_message(
            f"🚀 <b>GMGN Bot started — {mode}</b>\n"
            f"Wallet: <code>{trader.public_key[:20]}...</code>\n"
            f"Scanning: trending · smart money · new tokens · wallet copy"
        )

        logger.info("Starting all modules...")
        await asyncio.gather(
            gmgn_client.run(),
            wallet_copier.run(),
            risk_manager.run(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("GMGN Bot stopped by user")
