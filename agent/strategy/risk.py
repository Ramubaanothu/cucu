from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select

from ..config import Settings
from ..db.models import Position
from ..executor.base import TradeRequest

log = logging.getLogger(__name__)


@dataclass
class RiskResult:
    allowed: bool
    reason: str
    adjusted_size: float  # may be reduced from original


@dataclass
class ExposureSnapshot:
    total_usdc: float
    per_market: dict[str, float]
    max_total: float
    max_per_market: float


class RiskManager:
    def __init__(self, settings: Settings, session_factory) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._market_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def check_trade(self, req: TradeRequest) -> RiskResult:
        async with self._market_locks[req.market_id]:
            return await self._check(req)

    async def _check(self, req: TradeRequest) -> RiskResult:
        snap = await self.get_exposure_snapshot()
        remaining_total = self._settings.max_total_exposure - snap.total_usdc
        remaining_market = self._settings.max_exposure_per_market - snap.per_market.get(req.market_id, 0.0)

        # Rule 1: total portfolio cap
        if remaining_total <= 0:
            return RiskResult(allowed=False, reason="Total portfolio exposure cap reached", adjusted_size=0)

        # Rule 2: per-market cap (scale down instead of reject)
        adjusted_size = min(req.size_usdc, remaining_total, remaining_market)

        if adjusted_size < 1.0:
            return RiskResult(allowed=False, reason="Adjusted size below $1 minimum", adjusted_size=0)

        # Rule 3: signal confidence gate
        if req.signal_confidence < 0.3:
            return RiskResult(allowed=False, reason=f"Signal confidence too low: {req.signal_confidence:.2f}", adjusted_size=0)

        reason = ""
        if adjusted_size < req.size_usdc:
            reason = f"Size reduced from ${req.size_usdc:.2f} to ${adjusted_size:.2f} (exposure cap)"

        return RiskResult(allowed=True, reason=reason, adjusted_size=adjusted_size)

    async def get_exposure_snapshot(self) -> ExposureSnapshot:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Position.market_id, func.sum(Position.size_usdc))
                .where(Position.status == "open")
                .group_by(Position.market_id)
            )
            rows = result.all()

        per_market = {row[0]: float(row[1]) for row in rows}
        total = sum(per_market.values())

        return ExposureSnapshot(
            total_usdc=total,
            per_market=per_market,
            max_total=self._settings.max_total_exposure,
            max_per_market=self._settings.max_exposure_per_market,
        )

    async def get_market_exposure(self, market_id: str) -> float:
        snap = await self.get_exposure_snapshot()
        return snap.per_market.get(market_id, 0.0)
