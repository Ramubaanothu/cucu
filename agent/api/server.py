from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..db.models import Position, TradeLog, Wallet
from ..polymarket.data import DataClient
from ..scanner.detector import BotDetector
from ..scanner.leaderboard import WalletCandidate
from ..scanner.onchain_scanner import HotWalletAnalyzer
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
    data_client: DataClient | None = None,
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

    # ── Hot-wallet real-time intake ──────────────────────────────────────────────

    class HotWalletPayload(BaseModel):
        address: str
        trade_count: int
        window_hours: float
        buy_usdc: float
        sell_usdc: float
        profit_ratio: float
        unique_markets: int
        reason: str

    # Cooldown: skip re-evaluation if we just processed this wallet
    _hot_wallet_seen: dict[str, float] = {}
    _HOT_WALLET_COOLDOWN = 3600.0  # 1 hour

    @app.post("/hot-wallet")
    async def receive_hot_wallet(payload: HotWalletPayload) -> dict:
        address = payload.address.lower()
        now = time.monotonic()

        # Already watching?
        async with copier._wallets_lock:
            if address in copier.active_wallets:
                return {"status": "already_watching", "address": address}

        # Evaluation cooldown — avoid hammering Data API for the same wallet
        last_seen = _hot_wallet_seen.get(address, 0.0)
        if now - last_seen < _HOT_WALLET_COOLDOWN:
            return {"status": "cooldown", "address": address}
        _hot_wallet_seen[address] = now

        log.info(
            "[HotWallet] Alert from monitor: %s — %s (buy=$%.0f sell=$%.0f ratio=%.1fx)",
            address[:10], payload.reason, payload.buy_usdc, payload.sell_usdc, payload.profit_ratio,
        )

        if data_client is None:
            return {"status": "no_data_client"}

        # Fetch trade history and score immediately
        trades, profile = await asyncio.gather(
            data_client.get_trader_trades(address, limit=100),
            data_client.get_trader_profile(address),
        )

        candidate = WalletCandidate(
            address=address,
            entry_1m=None,
            entry_1w=None,
            profile=profile,
        )
        analyzer = HotWalletAnalyzer()
        detector = BotDetector()

        score = analyzer.score(candidate, trades)
        bot   = detector.analyze(address, trades, profile)

        log.info(
            "[HotWallet] %s scored %.1f (win_rate=%.0f%% trades=%d bot=%s)",
            address[:10], score.composite_score,
            score.win_rate * 100, score.trade_count, bot.is_bot_likely,
        )

        # Threshold: lower than normal (40) — on-chain pattern already a strong signal
        HOT_WALLET_MIN_SCORE = 40.0
        if score.composite_score < HOT_WALLET_MIN_SCORE and not bot.is_bot_likely:
            return {
                "status": "rejected",
                "address": address,
                "score": score.composite_score,
                "reason": f"score {score.composite_score:.1f} < {HOT_WALLET_MIN_SCORE} and not bot-like",
            }

        added = await copier.add_wallet(address)

        # Persist to DB so the wallet appears in /wallets and survives restart
        async with session_factory() as session:
            existing = (
                await session.execute(select(Wallet).where(Wallet.address == address))
            ).scalar_one_or_none()
            w = existing or Wallet(address=address)
            if not existing:
                session.add(w)
            w.composite_score = score.composite_score
            w.win_rate        = score.win_rate
            w.roi_30d         = score.roi_30d
            w.sharpe          = score.sharpe
            w.trade_count     = score.trade_count
            w.is_bot_likely   = bot.is_bot_likely
            w.bot_confidence  = bot.bot_confidence
            w.is_active       = True
            w.last_scanned_at = datetime.utcnow()
            await session.commit()

        status = "activated" if added else "already_active"
        log.info("[HotWallet] %s %s (score=%.1f bot=%s)", address[:10], status, score.composite_score, bot.is_bot_likely)
        return {
            "status": status,
            "address": address,
            "score": score.composite_score,
            "win_rate": round(score.win_rate, 3),
            "trade_count": score.trade_count,
            "is_bot_likely": bot.is_bot_likely,
            "trigger_reason": payload.reason,
        }

    @app.post("/rescan")
    async def trigger_rescan() -> dict:
        return {"status": "rescan_requested"}

    return app
