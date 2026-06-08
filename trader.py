from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from config import (
    DEFAULT_SLIPPAGE_BPS,
    HELIUS_RPC_URL,
    JUPITER_PRICE_URL,
    JUPITER_QUOTE_URL,
    JUPITER_SWAP_URL,
    LAMPORTS_PER_SOL,
    MAX_RETRIES,
    PAPER_TRADE,
    RETRY_DELAY_SECONDS,
    SOL_MINT,
    SOLANA_RPC_URL,
    WALLET_PRIVATE_KEY,
)

logger = logging.getLogger("gmgn-bot.trader")


def sol_to_lamports(sol: float) -> int:
    return int(sol * LAMPORTS_PER_SOL)


def lamports_to_sol(lam: int) -> float:
    return lam / LAMPORTS_PER_SOL


async def fetch_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
    label: str = "",
) -> Any:
    for attempt in range(MAX_RETRIES):
        try:
            async with session.request(
                method, url, params=params, json=json_body,
                headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except Exception as exc:
            if attempt == MAX_RETRIES - 1:
                logger.warning("%s failed: %s", label or url[:60], exc)
                return {}
            await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
    return {}


@dataclass
class BuyResult:
    success: bool
    mint: str
    symbol: str
    amount_sol: float
    tokens_received: float
    entry_price_usd: float
    tx_signature: str | None
    source: str
    reason: str
    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class SellResult:
    success: bool
    mint: str
    symbol: str
    amount_tokens: float
    sol_received: float
    exit_price_usd: float
    tx_signature: str | None
    sell_pct: float


class Trader:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.paper_trade = PAPER_TRADE
        self.keypair = self._load_keypair()
        self._sol_price_usd = 150.0
        self._sol_price_ts = 0.0
        self._price_cache: dict[str, tuple[float, float | None]] = {}

    def _load_keypair(self):
        if not WALLET_PRIVATE_KEY or WALLET_PRIVATE_KEY.startswith("your_"):
            if not PAPER_TRADE:
                logger.warning("No WALLET_PRIVATE_KEY — forcing paper trade")
            return None
        try:
            import base58
            from solders.keypair import Keypair
            raw = base58.b58decode(WALLET_PRIVATE_KEY)
            return Keypair.from_bytes(raw)
        except Exception:
            try:
                import json
                from solders.keypair import Keypair
                return Keypair.from_bytes(bytes(json.loads(WALLET_PRIVATE_KEY)))
            except Exception as exc:
                logger.error("Failed to load keypair: %s", exc)
                return None

    @property
    def public_key(self) -> str:
        return str(self.keypair.pubkey()) if self.keypair else "PAPER_WALLET"

    async def get_sol_price(self) -> float:
        now = time.time()
        if now - self._sol_price_ts < 60:
            return self._sol_price_usd
        self._sol_price_ts = now
        try:
            data = await fetch_json(self.session, "GET", JUPITER_PRICE_URL,
                                    params={"ids": SOL_MINT}, label="SOL price")
            price = (data.get("data", {}).get(SOL_MINT, {}).get("price")
                     or data.get(SOL_MINT, {}).get("price"))
            if price:
                self._sol_price_usd = float(price)
        except Exception:
            pass
        return self._sol_price_usd

    async def get_token_price(self, mint: str) -> float | None:
        now = time.time()
        ts, cached = self._price_cache.get(mint, (0, None))
        if now - ts < 30:
            return cached

        result = await self._price_jupiter(mint)
        if not result:
            result = await self._price_dexscreener(mint)
        if not result:
            result = await self._price_gmgn(mint)

        self._price_cache[mint] = (now, result)
        return result

    async def _price_jupiter(self, mint: str) -> float | None:
        try:
            data = await fetch_json(self.session, "GET", JUPITER_PRICE_URL,
                                    params={"ids": mint}, label=f"Jupiter price {mint[:8]}")
            raw = (data.get("data", {}).get(mint, {}).get("price")
                   or data.get(mint, {}).get("price"))
            return float(raw) if raw else None
        except Exception:
            return None

    async def _price_dexscreener(self, mint: str) -> float | None:
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            data = await fetch_json(self.session, "GET", url,
                                    label=f"DexScreener price {mint[:8]}")
            pairs = data.get("pairs") or []
            if not pairs:
                return None
            best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            price_str = best.get("priceUsd")
            return float(price_str) if price_str else None
        except Exception:
            return None

    async def _price_gmgn(self, mint: str) -> float | None:
        try:
            url = f"https://gmgn.ai/defi/quotation/v1/tokens/sol/{mint}"
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gmgn.ai/"}
            data = await fetch_json(self.session, "GET", url, headers=headers,
                                    label=f"GMGN price {mint[:8]}")
            price = (data.get("data", {}).get("price")
                     or data.get("data", {}).get("priceUsd"))
            return float(price) if price else None
        except Exception:
            return None

    async def get_token_balance(self, mint: str) -> tuple[float, int]:
        if self.paper_trade or not self.keypair:
            return 0.0, 6
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                str(self.keypair.pubkey()),
                {"mint": mint},
                {"encoding": "jsonParsed"},
            ],
        }
        data = await fetch_json(self.session, "POST", HELIUS_RPC_URL,
                                json_body=payload, label="token balance")
        accounts = data.get("result", {}).get("value", [])
        if not accounts:
            return 0.0, 6
        info = accounts[0]["account"]["data"]["parsed"]["info"]
        return float(info["tokenAmount"]["uiAmount"] or 0), int(info["tokenAmount"]["decimals"])

    async def _get_quote(self, input_mint: str, output_mint: str, amount: int) -> dict:
        return await fetch_json(
            self.session, "GET", JUPITER_QUOTE_URL,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(DEFAULT_SLIPPAGE_BPS),
            },
            label="Jupiter quote",
        )

    async def _execute_swap(self, quote: dict) -> str | None:
        if self.paper_trade or not self.keypair:
            sig = f"PAPER_{uuid.uuid4().hex[:16]}"
            logger.info("[PAPER] swap would execute — out=%s  sig=%s", quote.get("outAmount"), sig)
            return sig

        from solders.message import to_bytes_versioned
        from solders.transaction import VersionedTransaction

        payload = {
            "quoteResponse": quote,
            "userPublicKey": str(self.keypair.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": 100_000,
        }
        swap_data = await fetch_json(self.session, "POST", JUPITER_SWAP_URL,
                                     json_body=payload, label="Jupiter swap build")
        tx_b64 = swap_data.get("swapTransaction")
        if not tx_b64:
            raise ValueError("Jupiter swap response missing swapTransaction")

        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        sig = self.keypair.sign_message(to_bytes_versioned(raw_tx.message))
        signed = VersionedTransaction.populate(raw_tx.message, [sig])
        encoded = base64.b64encode(bytes(signed)).decode()

        body = {
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [encoded, {"skipPreflight": True, "maxRetries": 3, "encoding": "base64"}],
        }
        for attempt in range(MAX_RETRIES):
            async with self.session.post(SOLANA_RPC_URL, json=body) as resp:
                result = await resp.json()
                if "error" in result:
                    if attempt == MAX_RETRIES - 1:
                        raise RuntimeError(result["error"])
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                tx_sig = result["result"]
                logger.info("Swap submitted: %s", tx_sig)
                return tx_sig
        return None

    async def buy(
        self,
        mint: str,
        amount_sol: float,
        source: str,
        reason: str,
        symbol: str = "UNKNOWN",
    ) -> BuyResult:
        logger.info("BUY [%s] %s (%s) %.4f SOL", source, symbol, mint[:8], amount_sol)
        lamports = sol_to_lamports(amount_sol)
        try:
            quote = await self._get_quote(SOL_MINT, mint, lamports)
            out_amount = int(quote.get("outAmount", 0))
            out_decimals = int(quote.get("outDecimals", 6) or 6)
            tokens_received = out_amount / (10 ** out_decimals)

            token_price = await self.get_token_price(mint)
            if not token_price and tokens_received > 0:
                sol_price = await self.get_sol_price()
                token_price = (amount_sol * sol_price) / tokens_received

            tx_sig = await self._execute_swap(quote)
            logger.info("BUY complete — %s %.4f tokens @ $%.8f (tx: %s)",
                        symbol, tokens_received, token_price or 0, tx_sig)
            return BuyResult(
                success=True, mint=mint, symbol=symbol,
                amount_sol=amount_sol, tokens_received=tokens_received,
                entry_price_usd=token_price or 0.0,
                tx_signature=tx_sig, source=source, reason=reason,
            )
        except Exception as exc:
            logger.error("BUY failed %s: %s", mint[:8], exc)
            return BuyResult(
                success=False, mint=mint, symbol=symbol,
                amount_sol=amount_sol, tokens_received=0,
                entry_price_usd=0, tx_signature=None, source=source, reason=reason,
            )

    async def sell(
        self,
        mint: str,
        amount_tokens: float,
        decimals: int = 6,
        symbol: str = "UNKNOWN",
        sell_pct: float = 100.0,
    ) -> SellResult:
        raw_amount = int(amount_tokens * (10 ** decimals))
        logger.info("SELL %s %.2f%% — %.4f tokens", symbol, sell_pct, amount_tokens)
        try:
            quote = await self._get_quote(mint, SOL_MINT, raw_amount)
            out_lamports = int(quote.get("outAmount", 0))
            sol_received = lamports_to_sol(out_lamports)
            token_price = await self.get_token_price(mint) or 0.0
            tx_sig = await self._execute_swap(quote)
            logger.info("SELL complete — %s %.4f SOL (tx: %s)", symbol, sol_received, tx_sig)
            return SellResult(
                success=True, mint=mint, symbol=symbol,
                amount_tokens=amount_tokens, sol_received=sol_received,
                exit_price_usd=token_price, tx_signature=tx_sig, sell_pct=sell_pct,
            )
        except Exception as exc:
            logger.error("SELL failed %s: %s", mint[:8], exc)
            return SellResult(
                success=False, mint=mint, symbol=symbol,
                amount_tokens=amount_tokens, sol_received=0,
                exit_price_usd=0, tx_signature=None, sell_pct=sell_pct,
            )
