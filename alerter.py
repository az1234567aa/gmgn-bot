from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_SEND_URL

logger = logging.getLogger("gmgn-bot.alerter")


@dataclass
class TradeAlert:
    token_mint: str
    token_symbol: str
    source: str          # e.g. "GMGN trending", "GMGN smart money", "wallet copy"
    reason: str
    entry_price: float
    exit_price: float
    exit_reason: str
    entry_time: datetime
    exit_time: datetime
    pnl_sol: float
    pnl_usd: float
    peak_multiplier: float
    sol_price_usd: float


class Alerter:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
                             and not TELEGRAM_BOT_TOKEN.startswith("your_"))

    async def send_message(self, text: str) -> None:
        if not self._enabled:
            logger.info("[ALERT] %s", text[:120])
            return
        url = TELEGRAM_SEND_URL.format(token=TELEGRAM_BOT_TOKEN)
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with self.session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Telegram error %d: %s", resp.status, body[:200])
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    async def send_buy_signal(
        self,
        symbol: str,
        mint: str,
        amount_sol: float,
        source: str,
        reason: str,
    ) -> None:
        emoji = "🟢" if "smart" in source.lower() or "wallet" in source.lower() else "📡"
        await self.send_message(
            f"{emoji} <b>GMGN BUY — {symbol}</b>\n"
            f"Source: <b>{source}</b>\n"
            f"Amount: {amount_sol} SOL\n"
            f"Reason: {reason}\n"
            f"Mint: <code>{mint[:20]}...</code>"
        )

    async def send_trade_alert(self, alert: TradeAlert) -> None:
        hold_sec = (alert.exit_time - alert.entry_time).total_seconds()
        hold_str = f"{int(hold_sec // 60)}m {int(hold_sec % 60)}s"
        pnl_sign = "+" if alert.pnl_sol >= 0 else ""
        emoji = "✅" if alert.pnl_sol >= 0 else "❌"

        await self.send_message(
            f"{emoji} <b>GMGN TRADE CLOSED — {alert.token_symbol}</b>\n"
            f"Source: {alert.source}\n"
            f"Exit reason: <b>{alert.exit_reason}</b>\n"
            f"Entry: ${alert.entry_price:.8f} → Exit: ${alert.exit_price:.8f}\n"
            f"Peak: {alert.peak_multiplier:.2f}x\n"
            f"PnL: <b>{pnl_sign}{alert.pnl_sol:.4f} SOL (${pnl_sign}{alert.pnl_usd:.2f})</b>\n"
            f"Hold: {hold_str}"
        )
