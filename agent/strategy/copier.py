from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..ai.analyst import AIAnalyst, CopyDecision
from ..config import Settings
from ..db.models import TradeLog
from ..executor.base import BaseExecutor, TradeRequest
from ..polymarket.clob import ClobClient
from ..polymarket.gamma import GammaClient
from .risk import RiskManager

log = logging.getLogger(__name__)


@dataclass
class CopySignal:
    source_wallet: str
    market_id: str
    token_id: str
    side: str  # YES | NO
    order_size_usdc: float
    price: float
    detected_at: str = ""
    detection_method: str = "poll"


class CopyTrader:
    def __init__(
        self,
        settings: Settings,
        risk: RiskManager,
        executor: BaseExecutor,
        analyst: AIAnalyst | None,
        clob: ClobClient,
        gamma: GammaClient,
        session_factory,
        active_wallets: set[str],
    ) -> None:
        self._settings = settings
        self._risk = risk
        self._executor = executor
        self._analyst = analyst
        self._clob = clob
        self._gamma = gamma
        self._session_factory = session_factory
        self.active_wallets = active_wallets
        self._wallets_lock = asyncio.Lock()

    async def update_active_wallets(self, new_wallets: list[str]) -> None:
        """Thread-safe replacement of the active wallet set."""
        async with self._wallets_lock:
            self.active_wallets.clear()
            self.active_wallets.update(new_wallets)

    async def handle_signal(self, signal: CopySignal) -> None:
        wallet = signal.source_wallet.lower()

        async with self._wallets_lock:
            is_target = wallet in self.active_wallets

        if not is_target:
            log.debug("Signal from non-target wallet %s — ignored", wallet[:10])
            return

        log.info(
            "Signal received from %s: %s $%.2f @ %.3f on %s",
            wallet[:10], signal.side, signal.order_size_usdc, signal.price, signal.market_id[:20],
        )

        raw_size = signal.order_size_usdc * self._settings.copy_scale
        scale_override = 1.0
        ai_reason = "no AI"

        if self._analyst and self._settings.has_claude_credentials:
            snap = await self._risk.get_exposure_snapshot()
            decision: CopyDecision = await self._analyst.assess_copy_trade(
                source_wallet=signal.source_wallet,
                market_id=signal.market_id,
                token_id=signal.token_id,
                side=signal.side,
                size_usdc=raw_size,
                price=signal.price,
                current_exposure=snap.total_usdc,
                max_exposure=self._settings.max_total_exposure,
            )
            if not decision.should_copy:
                log.info("Claude vetoed trade: %s", decision.reason)
                await self._log_skip(signal, f"Claude veto: {decision.reason}")
                return
            scale_override = max(0.1, min(2.0, decision.scale))
            ai_reason = decision.reason

        copy_size = raw_size * scale_override

        req = TradeRequest(
            market_id=signal.market_id,
            token_id=signal.token_id,
            condition_id=signal.market_id,
            side="YES" if signal.side.upper() in ("YES", "BUY") else "NO",
            size_usdc=copy_size,
            price=signal.price,
            source_wallet=signal.source_wallet,
            reason=ai_reason,
        )

        risk_result = await self._risk.check_trade(req)
        if not risk_result.allowed:
            log.info("Risk blocked trade: %s", risk_result.reason)
            await self._log_skip(signal, f"Risk: {risk_result.reason}")
            return

        req.size_usdc = risk_result.adjusted_size
        result = await self._executor.open_position(req)

        if result.success:
            log.info(
                "Copied trade: pos #%d %s $%.2f @ %.3f [%s]",
                result.position_id, req.side, result.executed_size,
                result.executed_price, result.mode,
            )
        else:
            log.warning("Executor failed: %s", result.error)

    async def _log_skip(self, signal: CopySignal, reason: str) -> None:
        async with self._session_factory() as session:
            tl = TradeLog(
                action="skip",
                mode=self._settings.trading_mode,
                market_id=signal.market_id,
                token_id=signal.token_id,
                side=signal.side,
                requested_size=signal.order_size_usdc * self._settings.copy_scale,
                executed_size=0,
                price=signal.price,
                source_wallet=signal.source_wallet,
                reason=reason,
                raw_response="{}",
            )
            session.add(tl)
            await session.commit()
