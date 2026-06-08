from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from alerter import TradeAlert
from config import (
    RISK_POLL_INTERVAL_SECONDS,
    STOP_LOSS_PCT,
    TIME_STOP_MIN_MULTIPLIER,
    TIME_STOP_MINUTES,
    TP1_MULTIPLIER,
    TP1_SELL_PCT,
    TP2_MULTIPLIER,
    TP2_SELL_PCT,
    TP3_MULTIPLIER,
    TP3_SELL_PCT,
    TRAILING_ACTIVATION_MULTIPLIER,
    TRAILING_STOP_PCT,
)

if TYPE_CHECKING:
    from alerter import Alerter
    from trader import BuyResult, Trader

logger = logging.getLogger("gmgn-bot.risk")
POSITIONS_FILE = "gmgn_positions.json"


@dataclass
class Position:
    position_id: str
    mint: str
    symbol: str
    source: str
    entry_price_usd: float
    entry_time: datetime
    initial_tokens: float
    remaining_tokens: float
    initial_sol: float
    reason: str
    decimals: int = 6
    peak_multiplier: float = 1.0
    trailing_active: bool = False
    trailing_peak: float = 1.0
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    closed: bool = False
    total_sol_out: float = 0.0
    exits: list[dict[str, Any]] = field(default_factory=list)


def _save(positions: dict[str, Position]) -> None:
    try:
        data = {}
        for pid, p in positions.items():
            if p.closed:
                continue
            data[pid] = {
                "position_id": p.position_id, "mint": p.mint,
                "symbol": p.symbol, "source": p.source,
                "entry_price_usd": p.entry_price_usd,
                "entry_time": p.entry_time.isoformat(),
                "initial_tokens": p.initial_tokens,
                "remaining_tokens": p.remaining_tokens,
                "initial_sol": p.initial_sol,
                "reason": p.reason, "decimals": p.decimals,
                "peak_multiplier": p.peak_multiplier,
                "trailing_active": p.trailing_active,
                "trailing_peak": p.trailing_peak,
                "tp1_hit": p.tp1_hit, "tp2_hit": p.tp2_hit, "tp3_hit": p.tp3_hit,
                "total_sol_out": p.total_sol_out,
                "exits": [{k: str(v) if isinstance(v, datetime) else v
                           for k, v in e.items()} for e in p.exits],
            }
        with open(POSITIONS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        logger.warning("Save positions failed: %s", exc)


def _load() -> dict[str, Position]:
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE) as f:
            data = json.load(f)
        positions = {}
        for pid, d in data.items():
            positions[pid] = Position(
                position_id=d["position_id"], mint=d["mint"],
                symbol=d["symbol"], source=d.get("source", "GMGN"),
                entry_price_usd=d["entry_price_usd"],
                entry_time=datetime.fromisoformat(d["entry_time"]),
                initial_tokens=d["initial_tokens"],
                remaining_tokens=d["remaining_tokens"],
                initial_sol=d["initial_sol"],
                reason=d["reason"],
                decimals=d.get("decimals", 6),
                peak_multiplier=d.get("peak_multiplier", 1.0),
                trailing_active=d.get("trailing_active", False),
                trailing_peak=d.get("trailing_peak", 1.0),
                tp1_hit=d.get("tp1_hit", False),
                tp2_hit=d.get("tp2_hit", False),
                tp3_hit=d.get("tp3_hit", False),
                total_sol_out=d.get("total_sol_out", 0.0),
            )
        logger.info("Loaded %d open position(s) from disk", len(positions))
        return positions
    except Exception as exc:
        logger.warning("Load positions failed: %s", exc)
        return {}


class RiskManager:
    def __init__(self, trader: "Trader", alerter: "Alerter") -> None:
        self.trader = trader
        self.alerter = alerter
        self.positions: dict[str, Position] = _load()
        self._running = False

    async def open_position(self, buy: "BuyResult") -> None:
        if not buy.success or buy.tokens_received <= 0:
            return
        p = Position(
            position_id=buy.position_id, mint=buy.mint, symbol=buy.symbol,
            source=buy.source, entry_price_usd=buy.entry_price_usd,
            entry_time=datetime.now(timezone.utc),
            initial_tokens=buy.tokens_received, remaining_tokens=buy.tokens_received,
            initial_sol=buy.amount_sol, reason=buy.reason,
        )
        self.positions[buy.position_id] = p
        _save(self.positions)
        logger.info("Position opened — %s [%s] %.4f tokens @ $%.8f",
                    buy.symbol, buy.source, buy.tokens_received, buy.entry_price_usd)

    async def _sell_partial(self, p: Position, pct: float, reason: str) -> float:
        tokens = p.remaining_tokens * (pct / 100.0)
        if tokens <= 0:
            return 0.0
        result = await self.trader.sell(
            mint=p.mint, amount_tokens=tokens, decimals=p.decimals,
            symbol=p.symbol, sell_pct=pct,
        )
        if result.success:
            p.remaining_tokens -= tokens
            p.total_sol_out += result.sol_received
            p.exits.append({"reason": reason, "tokens": tokens,
                             "sol": result.sol_received, "time": datetime.now(timezone.utc)})
            _save(self.positions)
            logger.info("Partial sell — %s | %s | %.2f%% | %.4f SOL",
                        p.symbol, reason, pct, result.sol_received)
            return result.sol_received
        return 0.0

    async def _close(self, p: Position, reason: str, price: float) -> None:
        if p.closed:
            return
        if p.remaining_tokens > 0:
            await self._sell_partial(p, 100.0, reason)
        p.closed = True
        _save(self.positions)

        exit_time = datetime.now(timezone.utc)
        pnl_sol = p.total_sol_out - p.initial_sol
        sol_price = await self.trader.get_sol_price()
        await self.alerter.send_trade_alert(TradeAlert(
            token_mint=p.mint, token_symbol=p.symbol,
            source=p.source, reason=p.reason,
            entry_price=p.entry_price_usd, exit_price=price,
            exit_reason=reason, entry_time=p.entry_time, exit_time=exit_time,
            pnl_sol=pnl_sol, pnl_usd=pnl_sol * sol_price,
            peak_multiplier=p.peak_multiplier, sol_price_usd=sol_price,
        ))
        hold = (exit_time - p.entry_time).total_seconds()
        logger.info("Closed %s | %s | hold %dm%ds | PnL %.4f SOL | peak %.2fx",
                    p.symbol, reason, int(hold // 60), int(hold % 60), pnl_sol, p.peak_multiplier)

    async def _evaluate(self, p: Position) -> None:
        if p.closed:
            return
        price = await self.trader.get_token_price(p.mint)
        if not price or price <= 0:
            return

        mult = price / p.entry_price_usd if p.entry_price_usd > 0 else 1.0
        p.peak_multiplier = max(p.peak_multiplier, mult)
        pnl_pct = (mult - 1.0) * 100.0
        hold_min = (datetime.now(timezone.utc) - p.entry_time).total_seconds() / 60.0

        if pnl_pct <= STOP_LOSS_PCT:
            await self._close(p, "SL", price)
            return

        if mult >= TRAILING_ACTIVATION_MULTIPLIER:
            p.trailing_active = True
            p.trailing_peak = max(p.trailing_peak, mult)

        if p.trailing_active:
            drawdown = ((mult - p.trailing_peak) / p.trailing_peak) * 100.0
            if drawdown <= TRAILING_STOP_PCT:
                await self._close(p, "trailing", price)
                return

        if hold_min >= TIME_STOP_MINUTES and mult < TIME_STOP_MIN_MULTIPLIER:
            await self._close(p, "time_stop", price)
            return

        if mult >= TP1_MULTIPLIER and not p.tp1_hit:
            p.tp1_hit = True
            await self._sell_partial(p, TP1_SELL_PCT, "TP1")

        if mult >= TP2_MULTIPLIER and not p.tp2_hit:
            p.tp2_hit = True
            await self._sell_partial(p, TP2_SELL_PCT, "TP2")

        if mult >= TP3_MULTIPLIER and not p.tp3_hit:
            p.tp3_hit = True
            await self._sell_partial(p, TP3_SELL_PCT, "TP3")
            if p.remaining_tokens <= 0:
                await self._close(p, "TP3", price)

    async def run(self) -> None:
        self._running = True
        logger.info("Risk manager started — polling every %ds", RISK_POLL_INTERVAL_SECONDS)
        while self._running:
            try:
                open_pos = [p for p in self.positions.values() if not p.closed]
                if open_pos:
                    await asyncio.gather(*[self._evaluate(p) for p in open_pos],
                                         return_exceptions=True)
            except Exception as exc:
                logger.error("Risk loop error: %s", exc)
            await asyncio.sleep(RISK_POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False
