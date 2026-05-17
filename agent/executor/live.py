from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from ..db.models import Position, TradeLog
from ..polymarket.clob import CLOBCredentials, ClobClient
from .base import BaseExecutor, TradeRequest, TradeResult

log = logging.getLogger(__name__)


class LiveExecutor(BaseExecutor):
    def __init__(
        self,
        clob: ClobClient,
        credentials: CLOBCredentials,
        session_factory,
    ) -> None:
        self._clob = clob
        self._creds = credentials
        self._session_factory = session_factory

    async def open_position(self, req: TradeRequest) -> TradeResult:
        if req.size_usdc < 1.0:
            return TradeResult(
                success=False, position_id=None,
                executed_size=0, executed_price=0,
                order_id=None, error="Order size below minimum ($1 USDC)",
                mode="live",
            )

        shares = req.size_usdc / req.price if req.price > 0 else 0
        if shares < 1:
            return TradeResult(
                success=False, position_id=None,
                executed_size=0, executed_price=0,
                order_id=None, error="Calculated shares < 1",
                mode="live",
            )

        try:
            order_resp = await self._build_and_place(req, shares)
        except Exception as exc:
            log.error("Live order failed: %s", exc)
            return TradeResult(
                success=False, position_id=None,
                executed_size=0, executed_price=0,
                order_id=None, error=str(exc),
                mode="live",
            )

        order_id = order_resp.get("orderID") or order_resp.get("id") or ""
        filled_size = float(order_resp.get("filledAmount") or req.size_usdc)
        filled_price = float(order_resp.get("fillPrice") or req.price)

        async with self._session_factory() as session:
            pos = Position(
                market_id=req.market_id,
                token_id=req.token_id,
                condition_id=req.condition_id,
                side=req.side,
                size_usdc=filled_size,
                entry_price=filled_price,
                current_price=filled_price,
                pnl=0.0,
                status="open",
                mode="live",
                source_wallet=req.source_wallet,
            )
            session.add(pos)
            await session.flush()

            tl = TradeLog(
                position_id=pos.id,
                action="open",
                mode="live",
                market_id=req.market_id,
                token_id=req.token_id,
                side=req.side,
                requested_size=req.size_usdc,
                executed_size=filled_size,
                price=filled_price,
                source_wallet=req.source_wallet,
                reason=req.reason,
                raw_response=str(order_resp),
            )
            session.add(tl)
            await session.commit()

        log.info(
            "LIVE OPEN: %s $%.2f @ %.3f order_id=%s pos #%d",
            req.side, filled_size, filled_price, order_id, pos.id,
        )
        return TradeResult(
            success=True, position_id=pos.id,
            executed_size=filled_size, executed_price=filled_price,
            order_id=order_id, error=None, mode="live",
        )

    async def _build_and_place(self, req: TradeRequest, shares: float) -> dict:
        """
        Build and submit a FOK limit order via the py-clob-client SDK.
        Wraps the sync SDK in asyncio.to_thread.
        """
        import asyncio

        def _sync_place():
            try:
                from py_clob_client.client import ClobClient as SDK
                from py_clob_client.clob_types import OrderArgs, OrderType
                from py_clob_client.constants import POLYGON

                sdk = SDK(
                    host="https://clob.polymarket.com",
                    key=self._creds.private_key,
                    chain_id=POLYGON,
                    api_key=self._creds.api_key,
                    api_secret=self._creds.api_secret,
                    api_passphrase=self._creds.api_passphrase,
                )
                order_args = OrderArgs(
                    token_id=req.token_id,
                    price=req.price,
                    size=round(shares, 2),
                    side="BUY" if req.side == "YES" else "SELL",
                )
                signed = sdk.create_order(order_args)
                return sdk.post_order(signed, OrderType.FOK)
            except ImportError:
                raise RuntimeError(
                    "py-clob-client not installed. Run: pip install py-clob-client"
                )

        return await asyncio.to_thread(_sync_place)

    async def close_position(self, position_id: int, reason: str) -> TradeResult:
        async with self._session_factory() as session:
            pos = await session.get(Position, position_id)
            if not pos or pos.status != "open":
                return TradeResult(
                    success=False, position_id=position_id,
                    executed_size=0, executed_price=0,
                    order_id=None, error="Position not found or already closed",
                    mode="live",
                )
            # Place opposing FOK order to close
            close_req = TradeRequest(
                market_id=pos.market_id,
                token_id=pos.token_id,
                condition_id=pos.condition_id,
                side="NO" if pos.side == "YES" else "YES",
                size_usdc=pos.size_usdc,
                price=pos.current_price,
                source_wallet=pos.source_wallet,
                reason=reason,
            )
            shares = pos.size_usdc / pos.current_price if pos.current_price > 0 else 0
            try:
                order_resp = await self._build_and_place(close_req, shares)
            except Exception as exc:
                log.error("Live close failed: %s", exc)
                return TradeResult(
                    success=False, position_id=position_id,
                    executed_size=0, executed_price=0,
                    order_id=None, error=str(exc), mode="live",
                )

            pos.status = "closed"
            pos.closed_at = datetime.utcnow()
            tl = TradeLog(
                position_id=pos.id, action="close", mode="live",
                market_id=pos.market_id, token_id=pos.token_id,
                side=close_req.side, requested_size=pos.size_usdc,
                executed_size=pos.size_usdc, price=pos.current_price,
                source_wallet=pos.source_wallet, reason=reason,
                raw_response=str(order_resp),
            )
            session.add(tl)
            await session.commit()

        return TradeResult(
            success=True, position_id=position_id,
            executed_size=pos.size_usdc, executed_price=pos.current_price,
            order_id=None, error=None, mode="live",
        )

    async def get_open_positions(self) -> list[Position]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Position).where(Position.status == "open", Position.mode == "live")
            )
            return list(result.scalars().all())
