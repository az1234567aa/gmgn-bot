from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from config import (
    COPY_BUY_SOL,
    COPY_WALLET_COUNT,
    HELIUS_TX_URL,
    MAX_MARKET_CAP_USD,
    RUGCHECK_URL,
    SOL_MINT,
    WALLET_POLL_INTERVAL_SECONDS,
)
from trader import Trader, fetch_json

logger = logging.getLogger("gmgn-bot.wallet_copier")

# Minimum SOL spent to count a tx as a "real" buy
MIN_SOL_BUY = 0.05


class WalletCopier:
    """
    Polls GMGN's top smart money wallets via Helius and copies their buys.
    Wallets are refreshed from GMGN every 10 minutes.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        trader: Trader,
        risk_manager: Any,
        alerter: Any,
        gmgn_client: Any,
    ) -> None:
        self.session = session
        self.trader = trader
        self.risk_manager = risk_manager
        self.alerter = alerter
        self.gmgn_client = gmgn_client
        self._wallets: list[str] = []
        self._seen_txs: set[str] = set()
        self._bought_mints: set[str] = set()
        self._running = False
        self._wallet_refresh_counter = 0

    async def _refresh_wallets(self) -> None:
        wallets = await self.gmgn_client.fetch_top_wallets()
        if wallets:
            self._wallets = wallets[:COPY_WALLET_COUNT]
            logger.info("Tracking %d GMGN top wallets", len(self._wallets))
        else:
            logger.warning("No GMGN wallets returned — keeping previous list (%d)", len(self._wallets))

    async def _fetch_recent_txs(self, wallet: str) -> list[dict]:
        url = HELIUS_TX_URL.format(address=wallet)
        data = await fetch_json(
            self.session, "GET", url,
            params={"limit": "10", "type": "SWAP"},
            label=f"Helius txs {wallet[:8]}",
        )
        return data if isinstance(data, list) else []

    def _extract_buy(self, tx: dict) -> tuple[str, float, str] | None:
        """
        Return (mint, sol_spent, symbol) if this tx is a token buy, else None.
        """
        if tx.get("type") != "SWAP":
            return None
        transfers = tx.get("tokenTransfers", []) or []
        native_transfers = tx.get("nativeTransfers", []) or []

        sol_out = sum(
            abs(t.get("amount", 0)) / 1e9
            for t in native_transfers
            if t.get("fromUserAccount") and t.get("amount", 0) > 0
        )
        if sol_out < MIN_SOL_BUY:
            return None

        for t in transfers:
            mint = t.get("mint", "")
            if mint and mint != SOL_MINT:
                symbol = tx.get("description", "").split(" ")[-1] if tx.get("description") else "UNKNOWN"
                return mint, sol_out, symbol
        return None

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
            return True

    async def _process_wallet(self, wallet: str) -> None:
        txs = await self._fetch_recent_txs(wallet)
        for tx in txs:
            sig = tx.get("signature", "")
            if not sig or sig in self._seen_txs:
                continue
            self._seen_txs.add(sig)

            buy = self._extract_buy(tx)
            if not buy:
                continue
            mint, sol_spent, symbol = buy

            if mint in self._bought_mints:
                continue

            safe = await self._rugcheck_ok(mint)
            if not safe:
                logger.info("Copy skip %s — RugCheck flagged", mint[:8])
                continue

            self._bought_mints.add(mint)
            reason = f"Copied GMGN wallet {wallet[:8]} | original buy {sol_spent:.3f} SOL"
            logger.info("Copy trade — %s from wallet %s", mint[:8], wallet[:8])

            await self.alerter.send_buy_signal(
                symbol, mint, COPY_BUY_SOL, "GMGN wallet copy", reason
            )
            result = await self.trader.buy(
                mint=mint, amount_sol=COPY_BUY_SOL,
                source="GMGN wallet copy", reason=reason, symbol=symbol,
            )
            if result.success:
                await self.risk_manager.open_position(result)

    async def run(self) -> None:
        self._running = True
        logger.info("Wallet copier started — polling every %ds", WALLET_POLL_INTERVAL_SECONDS)
        await self._refresh_wallets()

        while self._running:
            try:
                self._wallet_refresh_counter += 1
                if self._wallet_refresh_counter >= 20:  # refresh wallets every ~3 min
                    self._wallet_refresh_counter = 0
                    await self._refresh_wallets()

                for wallet in self._wallets:
                    try:
                        await self._process_wallet(wallet)
                    except Exception as exc:
                        logger.error("Error processing wallet %s: %s", wallet[:8], exc)
                    await asyncio.sleep(1.0)

            except Exception as exc:
                logger.error("Wallet copier loop error: %s", exc)

            await asyncio.sleep(WALLET_POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False
