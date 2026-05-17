from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..db.models import Position, TradeLog, Wallet
from ..strategy.copier import CopySignal, CopyTrader
from ..strategy.risk import RiskManager

log = logging.getLogger(__name__)


class CopySignalPayload(BaseModel):
    source_wallet: str
    market_id: str
    token_id: str
    side: str
    order_size_usdc: float
    price: float
    detected_at: str = ""
    detection_method: str = "poll"


def build_app(
    copier: CopyTrader,
    risk: RiskManager,
    session_factory,
) -> FastAPI:
    app = FastAPI(title="Polymarket Copy-Trading Agent", version="0.1.0")

    @app.get("/status")
    async def status() -> dict:
        snap = await risk.get_exposure_snapshot()
        return {
            "active_wallets": list(copier.active_wallets),
            "exposure": {
                "total_usdc": round(snap.total_usdc, 2),
                "max_total": snap.max_total,
                "per_market": {k: round(v, 2) for k, v in snap.per_market.items()},
            },
        }

    @app.get("/wallets")
    async def list_wallets() -> list[dict]:
        async with session_factory() as session:
            result = await session.execute(
                select(Wallet).order_by(Wallet.composite_score.desc())
            )
            wallets = result.scalars().all()
        return [
            {
                "address": w.address,
                "score": w.composite_score,
                "win_rate": w.win_rate,
                "roi_30d": w.roi_30d,
                "sharpe": w.sharpe,
                "is_active": w.is_active,
                "is_bot_likely": w.is_bot_likely,
                "trade_count": w.trade_count,
            }
            for w in wallets
        ]

    @app.post("/wallets/{address}/toggle")
    async def toggle_wallet(address: str) -> dict:
        address = address.lower()
        async with session_factory() as session:
            result = await session.execute(
                select(Wallet).where(Wallet.address == address)
            )
            wallet = result.scalar_one_or_none()
            if not wallet:
                raise HTTPException(status_code=404, detail="Wallet not found")
            wallet.is_active = not wallet.is_active
            await session.commit()
            if wallet.is_active:
                copier.active_wallets.add(address)
            else:
                copier.active_wallets.discard(address)
        return {"address": address, "is_active": wallet.is_active}

    @app.get("/positions")
    async def list_positions(mode: str = "paper") -> list[dict]:
        async with session_factory() as session:
            result = await session.execute(
                select(Position)
                .where(Position.status == "open", Position.mode == mode)
                .order_by(Position.opened_at.desc())
            )
            positions = result.scalars().all()
        return [
            {
                "id": p.id,
                "market_id": p.market_id,
                "side": p.side,
                "size_usdc": p.size_usdc,
                "entry_price": p.entry_price,
                "pnl": p.pnl,
                "source_wallet": p.source_wallet,
                "opened_at": str(p.opened_at),
            }
            for p in positions
        ]

    @app.get("/trades")
    async def list_trades(limit: int = 50, mode: str | None = None) -> list[dict]:
        async with session_factory() as session:
            q = select(TradeLog).order_by(TradeLog.created_at.desc()).limit(limit)
            if mode:
                q = q.where(TradeLog.mode == mode)
            result = await session.execute(q)
            trades = result.scalars().all()
        return [
            {
                "id": t.id,
                "action": t.action,
                "mode": t.mode,
                "market_id": t.market_id,
                "side": t.side,
                "executed_size": t.executed_size,
                "price": t.price,
                "reason": t.reason,
                "created_at": str(t.created_at),
            }
            for t in trades
        ]

    @app.post("/copy-signal")
    async def receive_copy_signal(payload: CopySignalPayload) -> dict:
        signal = CopySignal(
            source_wallet=payload.source_wallet,
            market_id=payload.market_id,
            token_id=payload.token_id,
            side=payload.side,
            order_size_usdc=payload.order_size_usdc,
            price=payload.price,
            detected_at=payload.detected_at,
            detection_method=payload.detection_method,
        )
        # Fire and forget — don't block the HTTP response
        import asyncio
        asyncio.create_task(copier.handle_signal(signal))
        return {"status": "queued"}

    @app.post("/rescan")
    async def trigger_rescan() -> dict:
        """Signal the scanner to run immediately (handled by main loop)."""
        return {"status": "rescan_requested"}

    return app
