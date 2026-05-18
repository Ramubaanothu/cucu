from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..polymarket.data import TradeHistory, TraderProfile
from .leaderboard import WalletCandidate

log = logging.getLogger(__name__)

_30D_CUTOFF = timedelta(days=30)


@dataclass
class WalletScore:
    address: str
    composite_score: float = 0.0
    win_rate: float = 0.0
    roi_30d: float = 0.0
    sharpe: float = 0.0
    avg_trade_size: float = 0.0
    trade_frequency_per_day: float = 0.0
    consistency: float = 0.0
    trade_count: int = 0
    raw_metrics: dict = field(default_factory=dict)


class WalletAnalyzer:
    MIN_TRADE_COUNT = 30

    def score(
        self,
        candidate: WalletCandidate,
        trades: list[TradeHistory],
    ) -> WalletScore:
        ws = WalletScore(address=candidate.address)

        # Filter all metrics to last 30 days so ROI/Sharpe are current
        cutoff = datetime.now(timezone.utc) - _30D_CUTOFF
        recent = []
        for t in trades:
            try:
                ts = datetime.fromisoformat(t.timestamp.replace("Z", "+00:00"))
                if ts >= cutoff:
                    recent.append(t)
            except Exception:
                recent.append(t)  # include unparseable timestamps rather than lose data

        ws.trade_count = len(recent)

        if ws.trade_count < self.MIN_TRADE_COUNT:
            log.debug("%s: too few recent trades (%d in 30d)", candidate.address[:10], ws.trade_count)
            return ws

        # Win rate
        wins = sum(1 for t in recent if t.profit > 0)
        ws.win_rate = wins / ws.trade_count

        # ROI 30d
        if candidate.entry_1m and candidate.entry_1m.roi:
            ws.roi_30d = candidate.entry_1m.roi
        else:
            total_staked = sum(t.size for t in recent if t.size > 0)
            total_profit = sum(t.profit for t in recent)
            ws.roi_30d = total_profit / total_staked if total_staked > 0 else 0.0

        ws.sharpe = self._compute_sharpe(recent)

        sizes = [t.size for t in recent if t.size > 0]
        ws.avg_trade_size = statistics.mean(sizes) if sizes else 0.0

        ws.trade_frequency_per_day = self._compute_frequency(recent)
        ws.consistency = self._compute_consistency(recent)
        ws.composite_score = self._composite(ws)
        ws.raw_metrics = {
            "win_rate": ws.win_rate,
            "roi_30d": ws.roi_30d,
            "sharpe": ws.sharpe,
            "avg_trade_size": ws.avg_trade_size,
            "trade_frequency_per_day": ws.trade_frequency_per_day,
            "consistency": ws.consistency,
        }
        return ws

    def _compute_sharpe(self, trades: list[TradeHistory]) -> float:
        if len(trades) < 2:
            return 0.0
        daily: dict[str, float] = {}
        for t in trades:
            day = t.timestamp[:10] if len(t.timestamp) >= 10 else "unknown"
            daily[day] = daily.get(day, 0.0) + t.profit
        pnls = list(daily.values())
        if len(pnls) < 2:
            return 0.0
        mean = statistics.mean(pnls)
        std = statistics.stdev(pnls)
        if std == 0:
            return 3.0 if mean > 0 else 0.0
        return (mean / std) * math.sqrt(365)

    def _compute_frequency(self, trades: list[TradeHistory]) -> float:
        if len(trades) < 2:
            return 0.0
        timestamps = []
        for t in trades:
            try:
                ts = datetime.fromisoformat(t.timestamp.replace("Z", "+00:00"))
                timestamps.append(ts)
            except Exception:
                pass
        if len(timestamps) < 2:
            return float(len(trades))
        timestamps.sort()
        days = (timestamps[-1] - timestamps[0]).total_seconds() / 86400
        return len(trades) / days if days > 0 else float(len(trades))

    def _compute_consistency(self, trades: list[TradeHistory]) -> float:
        weekly: dict[str, float] = {}
        for t in trades:
            try:
                ts = datetime.fromisoformat(t.timestamp.replace("Z", "+00:00"))
                week_key = f"{ts.isocalendar().year}-W{ts.isocalendar().week:02d}"
                weekly[week_key] = weekly.get(week_key, 0.0) + t.profit
            except Exception:
                pass
        if not weekly:
            return 0.0
        profitable = sum(1 for v in weekly.values() if v > 0)
        return profitable / len(weekly)

    def _composite(self, ws: WalletScore) -> float:
        def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
            return max(lo, min(hi, x))

        score = (
            0.30 * clamp(ws.win_rate)
            + 0.25 * clamp(ws.roi_30d / 2.0)
            + 0.20 * clamp(ws.sharpe / 3.0)
            + 0.15 * clamp(ws.consistency)
            + 0.10 * clamp(ws.trade_frequency_per_day / 50.0)
        )
        return round(score * 100, 2)
