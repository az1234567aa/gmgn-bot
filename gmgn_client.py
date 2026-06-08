from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from config import (
    COPY_BUY_SOL,
    COPY_WALLET_COUNT,
    GMGN_NEW_TOKEN_URL,
    GMGN_SIGNALS_URL,
    GMGN_TOP_WALLETS_URL,
    GMGN_TRENDING_URL,
    MAX_MARKET_CAP_USD,
    MIN_LIQUIDITY_USD,
    MIN_SWAPS_1H,
    RUGCHECK_URL,
    SCAN_INTERVAL_SECONDS,
    SIGNAL_BUY_SOL,
    TRENDING_BUY_SOL,
)
from trader import Trader, fetch_json

logger = logging.getLogger("gmgn-bot.client")

_GMGN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://gmgn.ai/",
}


class GMGNClient:
    """Fetches signals from GMGN and submits buys via Trader."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        trader: Trader,
        risk_manager: Any,
        alerter: Any,
    ) -> None:
        self.session = session
        self.trader = trader
        self.risk_manager = risk_manager
        self.alerter = alerter
        self._seen: set[str] = set()
        self._running = False

    # ── GMGN API fetchers ─────────────────────────────────────────────────────

    async def _fetch_trending(self) -> list[dict]:
        """Top tokens ranked by 1h swap count."""
        try:
            data = await fetch_json(
                self.session, "GET", GMGN_TRENDING_URL,
                params={"limit": "25", "orderby": "swaps", "direction": "desc",
                        "filters[]": ["renounced", "frozen"]},
                headers=_GMGN_HEADERS, label="GMGN trending",
            )
            tokens = data.get("data", {}).get("rank", []) or []
            logger.info("GMGN trending: %d tokens", len(tokens))
            return tokens
        except Exception as exc:
            logger.warning("GMGN trending failed: %s", exc)
            return []

    async def _fetch_signals(self) -> list[dict]:
        """Smart money buy signals."""
        try:
            data = await fetch_json(
                self.session, "GET", GMGN_SIGNALS_URL,
                headers=_GMGN_HEADERS, label="GMGN signals",
            )
            signals = data.get("data", []) or []
            if signals:
                logger.info("GMGN smart money signals: %d", len(signals))
            return signals
        except Exception as exc:
            logger.warning("GMGN signals failed: %s", exc)
            return []

    async def _fetch_new_tokens(self) -> list[dict]:
        """Fresh token launches on Solana."""
        try:
            data = await fetch_json(
                self.session, "GET", GMGN_NEW_TOKEN_URL,
                params={"limit": "20", "filters[]": ["renounced", "frozen"]},
                headers=_GMGN_HEADERS, label="GMGN new tokens",
            )
            tokens = data.get("data", {}).get("new_token", []) or []
            if tokens:
                logger.info("GMGN new tokens: %d", len(tokens))
            return tokens
        except Exception as exc:
            logger.warning("GMGN new tokens failed: %s", exc)
            return []

    async def fetch_top_wallets(self) -> list[str]:
        """Return addresses of GMGN's top smart money wallets."""
        try:
            data = await fetch_json(
                self.session, "GET", GMGN_TOP_WALLETS_URL,
                params={"limit": str(COPY_WALLET_COUNT), "orderby": "pnl_30d",
                        "direction": "desc"},
                headers=_GMGN_HEADERS, label="GMGN top wallets",
            )
            wallets = data.get("data", []) or []
            addresses = [w.get("wallet_address", "") for w in wallets if w.get("wallet_address")]
            if addresses:
                logger.info("GMGN top wallets fetched: %s", addresses)
            return addresses
        except Exception as exc:
            logger.warning("GMGN top wallets failed: %s", exc)
            return []

    # ── Safety check ─────────────────────────────────────────────────────────

    async def _rugcheck_ok(self, mint: str) -> bool:
        try:
            url = RUGCHECK_URL.format(mint=mint)
            data = await fetch_json(self.session, "GET", url, label=f"rugcheck {mint[:8]}")
            risks = data.get("risks", []) or []
            is_honeypot = any(
                "honeypot" in str(r.get("name", "")).lower()
                or "cannot sell" in str(r.get("description", "")).lower()
                for r in risks
            )
            return not (is_honeypot or data.get("rugged", False))
        except Exception:
            return True  # allow if rugcheck is unreachable

    def _passes_filters(self, token: dict) -> bool:
        liquidity = float(token.get("liquidity", 0) or 0)
        market_cap = float(token.get("market_cap", 0) or token.get("usd_market_cap", 0) or 0)
        swaps_1h = int(token.get("swaps_1h", 0) or token.get("swaps", 0) or 0)
        if liquidity < MIN_LIQUIDITY_USD:
            return False
        if 0 < market_cap > MAX_MARKET_CAP_USD:
            return False
        if swaps_1h < MIN_SWAPS_1H:
            return False
        return True

    # ── Trade execution helper ────────────────────────────────────────────────

    async def _buy(self, mint: str, symbol: str, amount_sol: float, source: str, reason: str) -> None:
        if mint in self._seen:
            return
        self._seen.add(mint)

        safe = await self._rugcheck_ok(mint)
        if not safe:
            logger.info("Skip %s — RugCheck flagged", symbol)
            return

        await self.alerter.send_buy_signal(symbol, mint, amount_sol, source, reason)
        result = await self.trader.buy(
            mint=mint, amount_sol=amount_sol, source=source,
            reason=reason, symbol=symbol,
        )
        if result.success:
            await self.risk_manager.open_position(result)

    # ── Scan cycles ───────────────────────────────────────────────────────────

    async def _scan_trending(self) -> None:
        tokens = await self._fetch_trending()
        for t in tokens[:10]:
            mint = t.get("address", "")
            symbol = t.get("symbol", "UNKNOWN")
            if not mint or not self._passes_filters(t):
                continue
            swaps = int(t.get("swaps_1h", 0) or 0)
            price_change = float(t.get("price_change_1h", 0) or 0)
            reason = f"{swaps} swaps/1h | {price_change:+.1f}% 1h | liq ${float(t.get('liquidity', 0) or 0):,.0f}"
            await self._buy(mint, symbol, TRENDING_BUY_SOL, "GMGN trending", reason)
            await asyncio.sleep(0.5)

    async def _scan_signals(self) -> None:
        signals = await self._fetch_signals()
        for s in signals[:5]:
            mint = s.get("token_address", "")
            symbol = s.get("token_symbol", "UNKNOWN")
            signal_type = s.get("signal_type", "smart_money")
            wallet = s.get("wallet_address", "")[:8]
            if not mint:
                continue
            reason = f"signal_type={signal_type} | wallet={wallet}"
            await self._buy(mint, symbol, SIGNAL_BUY_SOL, "GMGN smart money", reason)
            await asyncio.sleep(0.5)

    async def _scan_new_tokens(self) -> None:
        tokens = await self._fetch_new_tokens()
        for t in tokens[:8]:
            mint = t.get("address", "")
            symbol = t.get("symbol", "UNKNOWN")
            if not mint or not self._passes_filters(t):
                continue
            age_min = float(t.get("open_timestamp", 0) or 0)
            reason = f"New launch | age ~{age_min:.0f}m | liq ${float(t.get('liquidity', 0) or 0):,.0f}"
            await self._buy(mint, symbol, TRENDING_BUY_SOL, "GMGN new launch", reason)
            await asyncio.sleep(0.5)

    async def run(self) -> None:
        self._running = True
        logger.info(
            "GMGN client started — scanning trending + smart money + new tokens every %ds",
            SCAN_INTERVAL_SECONDS,
        )
        while self._running:
            try:
                await self._scan_trending()
                await self._scan_signals()
                await self._scan_new_tokens()
            except Exception as exc:
                logger.error("GMGN scan error: %s", exc)
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False
