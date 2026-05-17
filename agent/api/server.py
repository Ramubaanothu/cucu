from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..db.models import Position, TradeLog, Wallet
from ..strategy.copier import CopySignal, CopyTrader
from ..strategy.risk import RiskManager

log = logging.getLogger(__name__)

# In-memory signal deduplication cache: hash -> monotonic timestamp
_recent_signals: dict[str, float] = {}
_SIGNAL_TTL = 60.0  # seconds


def _is_duplicate(source_wallet: str, market_id: str, side: str, detected_at: str) -> bool:
    key = hashlib.md5(
        f"{source_wallet.lower()}:{market_id}:{side}:{detected_at}".encode()
    ).hexdigest()
    now = time.monotonic()
    # Expire old entries
    expired = [k for k, ts in _recent_signals.items() if now - ts > _SIGNAL_TTL]
    for k in expired:
        del _recent_signals[k]
    if key in _recent_signals:
        return True
    _recent_signals[key] = now
    return False


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
        async with copier._wallets_lock:
            wallets = list(copier.active_wallets)
        return {
            "active_wallets": wallets,
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
        async with copier._wallets_lock:
            if wallet.is_active:
                copier.active_wallets.add(address)
            else:
                copier.active_wallets.discard(address)
        return {"address": address, "is_active": wallet.is_active}

    @app.get("/positions")
    async def list_positions(mode: str = "paper", status: str = "open") -> list[dict]:
        async with session_factory() as session:
            q = (
                select(Position)
                .where(Position.mode == mode, Position.status == status)
                .order_by(Position.opened_at.desc())
            )
            result = await session.execute(q)
            positions = result.scalars().all()
        return [
            {
                "id": p.id,
                "market_id": p.market_id,
                "side": p.side,
                "size_usdc": p.size_usdc,
                "entry_price": p.entry_price,
                "current_price": p.current_price,
                "pnl": p.pnl,
                "source_wallet": p.source_wallet,
                "opened_at": str(p.opened_at),
                "closed_at": str(p.closed_at) if p.closed_at else None,
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
        if _is_duplicate(
            payload.source_wallet,
            payload.market_id,
            payload.side,
            payload.detected_at,
        ):
            log.debug("Duplicate signal from %s ignored", payload.source_wallet[:10])
            return {"status": "duplicate_ignored"}

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
        asyncio.create_task(copier.handle_signal(signal))
        return {"status": "queued"}

    @app.post("/rescan")
    async def trigger_rescan() -> dict:
        return {"status": "rescan_requested"}

    return app
