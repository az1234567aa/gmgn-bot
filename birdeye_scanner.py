from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from config import (
    BIRDEYE_API_KEY,
    COPY_BUY_SOL,
    HELIUS_TX_URL,
    MIN_LIQUIDITY_USD,
    RUGCHECK_URL,
    SCAN_INTERVAL_SECONDS,
    SIGNAL_BUY_SOL,
    SOL_MINT,
    TRENDING_BUY_SOL,
)
from trader import Trader, fetch_json

logger = logging.getLogger("gmgn-bot.birdeye")

BIRDEYE_BASE    = "https://public-api.birdeye.so"
BIRDEYE_HEADERS = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}

# How many top Birdeye wallets to copy-track
TOP_WALLET_COUNT = 5

# Min 24h volume to consider a token worth buying
MIN_VOLUME_24H = 50_000

# Reject tokens where top 10 holders own more than this % (concentration = rug risk)
MAX_TOP10_HOLDER_PCT = 0.80


class BirdeyeScanner:
    """
    Replaces GMGN scanning with Birdeye API — works from Railway servers.

    Sources:
    1. Trending tokens (sorted by 24h volume change)
    2. New token listings
    3. Top gaining wallets → copy their recent buys via Helius
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        trader: Trader,
        risk_manager: Any,
        alerter: Any,
    ) -> None:
        self.session      = session
        self.trader       = trader
        self.risk_manager = risk_manager
        self.alerter      = alerter
        self._seen: set[str] = set()
        self._top_wallets: list[str] = []
        self._seen_txs: set[str] = set()
        self._bought_mints: set[str] = set()
        self._running     = False
        self._wallet_refresh_count = 0

    # ── Birdeye fetchers ──────────────────────────────────────────────────────

    async def _fetch_trending(self) -> list[dict]:
        """Top tokens by 24h volume change with minimum liquidity."""
        try:
            data = await fetch_json(
                self.session, "GET",
                f"{BIRDEYE_BASE}/defi/tokenlist",
                params={
                    "sort_by": "v24hChangePercent",
                    "sort_type": "desc",
                    "offset": "0",
                    "limit": "20",
                    "min_liquidity": str(MIN_LIQUIDITY_USD),
                },
                headers=BIRDEYE_HEADERS,
                label="Birdeye trending",
            )
            tokens = data.get("data", {}).get("tokens", []) or []
            logger.info("Birdeye trending: %d tokens", len(tokens))
            return tokens
        except Exception as exc:
            logger.warning("Birdeye trending failed: %s", exc)
            return []

    async def _fetch_new_listings(self) -> list[dict]:
        """Recently listed tokens on Solana."""
        try:
            data = await fetch_json(
                self.session, "GET",
                f"{BIRDEYE_BASE}/defi/v2/tokens/new_listing",
                params={"limit": "20", "meme_platform_enabled": "true"},
                headers=BIRDEYE_HEADERS,
                label="Birdeye new listings",
            )
            tokens = data.get("data", {}).get("items", []) or []
            if tokens:
                logger.info("Birdeye new listings: %d", len(tokens))
            return tokens
        except Exception as exc:
            logger.warning("Birdeye new listings failed: %s", exc)
            return []

    async def _fetch_top_wallets(self) -> list[str]:
        """
        Top gaining wallets — tries Birdeye trader endpoint first,
        falls back to a curated list of known profitable Solana wallets.
        """
        try:
            data = await fetch_json(
                self.session, "GET",
                f"{BIRDEYE_BASE}/trader/gainers-losers",
                params={"type": "1W", "sort_by": "PnL", "sort_type": "desc",
                        "offset": "0", "limit": str(TOP_WALLET_COUNT)},
                headers=BIRDEYE_HEADERS,
                label="Birdeye top wallets",
            )
            items = (data.get("data", {}).get("items", [])
                     or data.get("data", {}).get("result", []) or [])
            wallets = [w.get("address", "") or w.get("wallet", "")
                       for w in items if w.get("address") or w.get("wallet")]
            wallets = [w for w in wallets if w]
            if wallets:
                logger.info("Birdeye top wallets: %d found", len(wallets))
                return wallets[:TOP_WALLET_COUNT]
        except Exception as exc:
            logger.warning("Birdeye top wallets failed: %s — using fallback list", exc)

        # Fallback: known profitable Solana memecoin wallets (same as Solana bot)
        fallback = [
            "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o",  # Cented
            "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9",  # decu
            "FAicXNV5FVqtfbpn4Zccs71XcfGeyxBSGbqLDyDJZjke",  # Radiance
            "Av3xWHJ5EsoLZag6pr7LKbrGgLRTaykXomDD5kBhL9YQ",  # HeyItsYolo
            "99xnE2zEFi8YhmKDaikc1EvH6ELTQJppnqUwMzmpLXrs",  # Colercooks
        ]
        logger.info("Using fallback wallet list (%d wallets)", len(fallback))
        return fallback

    async def _fetch_token_security(self, mint: str) -> dict:
        try:
            data = await fetch_json(
                self.session, "GET",
                f"{BIRDEYE_BASE}/defi/token_security",
                params={"address": mint},
                headers=BIRDEYE_HEADERS,
                label=f"Birdeye security {mint[:8]}",
            )
            return data.get("data", {}) or {}
        except Exception:
            return {}

    async def _rugcheck_ok(self, mint: str) -> bool:
        try:
            url = RUGCHECK_URL.format(mint=mint)
            data = await fetch_json(self.session, "GET", url,
                                    label=f"rugcheck {mint[:8]}")
            score = float(data.get("score", 0) or 0)
            risks = data.get("risks", []) or []
            is_honeypot = any(
                "honeypot" in str(r.get("name", "")).lower()
                or "cannot sell" in str(r.get("description", "")).lower()
                for r in risks
            )
            return not (is_honeypot or data.get("rugged", False) or score > 500)
        except Exception:
            return True

    # ── Safety check ──────────────────────────────────────────────────────────

    async def _is_safe(self, mint: str, symbol: str) -> bool:
        # RugCheck first
        if not await self._rugcheck_ok(mint):
            logger.info("Skip %s — RugCheck flagged", symbol)
            return False

        # Birdeye token security — check concentration
        sec = await self._fetch_token_security(mint)
        top10 = float(sec.get("top10HolderPercent", 0) or 0)
        if top10 > MAX_TOP10_HOLDER_PCT:
            logger.info("Skip %s — top 10 holders own %.0f%% (too concentrated)",
                        symbol, top10 * 100)
            return False

        # Must be priceable
        price = await self.trader.get_token_price(mint)
        if not price or price <= 0:
            logger.info("Skip %s — no price data on Jupiter/DexScreener", symbol)
            return False

        return True

    # ── Buy helper ────────────────────────────────────────────────────────────

    async def _buy(self, mint: str, symbol: str, amount_sol: float,
                   source: str, reason: str) -> None:
        if mint in self._seen or mint in self._bought_mints:
            return
        self._seen.add(mint)

        if not await self._is_safe(mint, symbol):
            return

        await self.alerter.send_buy_signal(symbol, mint, amount_sol, source, reason)
        result = await self.trader.buy(
            mint=mint, amount_sol=amount_sol, source=source,
            reason=reason, symbol=symbol,
        )
        if result.success:
            self._bought_mints.add(mint)
            await self.risk_manager.open_position(result)

    # ── Scan cycles ───────────────────────────────────────────────────────────

    async def _scan_trending(self) -> None:
        tokens = await self._fetch_trending()
        for t in tokens[:10]:
            mint   = t.get("address", "")
            symbol = t.get("symbol", "UNKNOWN")
            liq    = float(t.get("liquidity", 0) or 0)
            vol24  = float(t.get("v24hUSD", 0) or 0)
            chg24  = float(t.get("v24hChangePercent", 0) or 0)

            if not mint or liq < MIN_LIQUIDITY_USD or vol24 < MIN_VOLUME_24H:
                continue

            reason = f"Birdeye trending | vol ${vol24:,.0f} | +{chg24:.1f}% 24h | liq ${liq:,.0f}"
            await self._buy(mint, symbol, TRENDING_BUY_SOL, "Birdeye trending", reason)
            await asyncio.sleep(0.5)

    async def _scan_new_listings(self) -> None:
        tokens = await self._fetch_new_listings()
        for t in tokens[:8]:
            mint   = t.get("address", "")
            symbol = t.get("symbol", "UNKNOWN")
            liq    = float(t.get("liquidity", 0) or 0)

            if not mint or liq < MIN_LIQUIDITY_USD:
                continue

            reason = f"Birdeye new listing | liq ${liq:,.0f}"
            await self._buy(mint, symbol, TRENDING_BUY_SOL, "Birdeye new listing", reason)
            await asyncio.sleep(0.5)

    async def _scan_wallet_copy(self) -> None:
        # Wallet copy disabled — Solana bot handles this via its own Helius key
        # Avoids 401/rate limit conflicts from sharing the same key
        pass

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        logger.info("Birdeye scanner started — trending + new listings + wallet copy every %ds",
                    SCAN_INTERVAL_SECONDS)

        # Load top wallets on startup
        self._top_wallets = await self._fetch_top_wallets()

        while self._running:
            try:
                self._wallet_refresh_count += 1
                if self._wallet_refresh_count >= 10:
                    self._wallet_refresh_count = 0
                    self._top_wallets = await self._fetch_top_wallets()

                await self._scan_trending()
                await self._scan_new_listings()
                await self._scan_wallet_copy()

            except Exception as exc:
                logger.error("Birdeye scan error: %s", exc)

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False
