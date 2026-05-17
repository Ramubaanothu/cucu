from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..db.models import Position, TradeLog
from .base import BaseExecutor, TradeRequest, TradeResult

log = logging.getLogger(__name__)


class PaperExecutor(BaseExecutor):
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def open_position(self, req: TradeRequest) -> TradeResult:
        async with self._session_factory() as session:
            pos = Position(
                market_id=req.market_id,
                token_id=req.token_id,
                condition_id=req.condition_id,
                side=req.side,
                size_usdc=req.size_usdc,
                entry_price=req.price,
                current_price=req.price,
                pnl=0.0,
                status="open",
                mode="paper",
                source_wallet=req.source_wallet,
            )
            session.add(pos)
            await session.flush()

            tl = TradeLog(
                position_id=pos.id,
                action="open",
                mode="paper",
                market_id=req.market_id,
                token_id=req.token_id,
                side=req.side,
                requested_size=req.size_usdc,
                executed_size=req.size_usdc,
                price=req.price,
                source_wallet=req.source_wallet,
                reason=req.reason,
                raw_response="{}",
            )
            session.add(tl)
            await session.commit()

        log.info(
            "PAPER OPEN: %s $%.2f @ %.3f on %s (pos #%d)",
            req.side, req.size_usdc, req.price, req.market_id[:20], pos.id,
        )
        return TradeResult(
            success=True,
            position_id=pos.id,
            executed_size=req.size_usdc,
            executed_price=req.price,
            order_id=None,
            error=None,
            mode="paper",
        )

    async def close_position(self, position_id: int, reason: str) -> TradeResult:
        async with self._session_factory() as session:
            pos = await session.get(Position, position_id)
            if not pos or pos.status != "open":
                return TradeResult(
                    success=False, position_id=position_id,
                    executed_size=0, executed_price=0,
                    order_id=None, error="Position not found or already closed",
                    mode="paper",
                )
            pos.status = "closed"
            pos.closed_at = datetime.utcnow()

            tl = TradeLog(
                position_id=pos.id,
                action="close",
                mode="paper",
                market_id=pos.market_id,
                token_id=pos.token_id,
                side=pos.side,
                requested_size=pos.size_usdc,
                executed_size=pos.size_usdc,
                price=pos.current_price,
                source_wallet=pos.source_wallet,
                reason=reason,
                raw_response="{}",
            )
            session.add(tl)
            await session.commit()

        log.info("PAPER CLOSE: pos #%d — %s", position_id, reason)
        return TradeResult(
            success=True, position_id=position_id,
            executed_size=pos.size_usdc, executed_price=pos.current_price,
            order_id=None, error=None, mode="paper",
        )

    async def get_open_positions(self) -> list[Position]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Position).where(Position.status == "open", Position.mode == "paper")
            )
            return list(result.scalars().all())
