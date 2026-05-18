from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from ..polymarket.data import TradeHistory, TraderProfile

log = logging.getLogger(__name__)


@dataclass
class BotSignals:
    address: str
    is_bot_likely: bool = False
    bot_confidence: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)


class BotDetector:
    BOT_CONFIDENCE_THRESHOLD = 0.6

    def analyze(
        self,
        address: str,
        trades: list[TradeHistory],
        profile: TraderProfile | None = None,
    ) -> BotSignals:
        result = BotSignals(address=address)
        if not trades:
            return result

        confidence = 0.0

        # 1. High trade frequency (>10/day)
        freq = self._trades_per_day(trades)
        if freq > 10:
            score = min((freq - 10) / 40.0, 1.0) * 0.20
            confidence += score
            result.signals["high_frequency"] = round(score, 3)

        # 2. Consistent position sizing
        sizes = [t.size for t in trades if t.size > 0]
        if len(sizes) >= 5:
            cv = statistics.stdev(sizes) / statistics.mean(sizes) if statistics.mean(sizes) > 0 else 1.0
            if cv < 0.15:
                score = (0.15 - cv) / 0.15 * 0.20
                confidence += score
                result.signals["consistent_sizing"] = round(score, 3)

        # 3. Rapid entry/exit (median position hold < 2h)
        hold_times = self._hold_times_hours(trades)
        if hold_times:
            median_hold = statistics.median(hold_times)
            if median_hold < 2.0:
                score = (2.0 - median_hold) / 2.0 * 0.15
                confidence += score
                result.signals["rapid_exits"] = round(score, 3)

        # 4. Market breadth (>20 distinct markets in 30d)
        markets = {t.market_id for t in trades if t.market_id}
        if len(markets) > 20:
            score = min((len(markets) - 20) / 30.0, 1.0) * 0.15
            confidence += score
            result.signals["market_breadth"] = round(score, 3)

        # 5. Round-the-clock activity (entropy of hour distribution)
        entropy_score = self._hour_entropy(trades)
        if entropy_score > 0.8:
            score = (entropy_score - 0.8) / 0.2 * 0.15
            confidence += score
            result.signals["24h_activity"] = round(score, 3)

        # 6. Near-mid-price entries (low slippage tolerance)
        near_mid = self._near_mid_fraction(trades)
        if near_mid > 0.7:
            score = (near_mid - 0.7) / 0.3 * 0.15
            confidence += score
            result.signals["low_slippage"] = round(score, 3)

        result.bot_confidence = round(min(confidence, 1.0), 3)
        result.is_bot_likely = result.bot_confidence >= self.BOT_CONFIDENCE_THRESHOLD
        log.debug(
            "%s bot_confidence=%.2f is_bot=%s",
            address[:10],
            result.bot_confidence,
            result.is_bot_likely,
        )
        return result

    def _trades_per_day(self, trades: list[TradeHistory]) -> float:
        timestamps = []
        for t in trades:
            try:
                ts = datetime.fromisoformat(t.timestamp.replace("Z", "+00:00"))
                timestamps.append(ts)
            except Exception:
                pass
        if len(timestamps) < 2:
            return 0.0
        timestamps.sort()
        days = (timestamps[-1] - timestamps[0]).total_seconds() / 86400
        return len(trades) / days if days > 0 else 0.0

    def _hold_times_hours(self, trades: list[TradeHistory]) -> list[float]:
        # Pair BUY → SELL on same token_id by timestamp
        buys: dict[str, list[datetime]] = {}
        holds = []
        sorted_trades = sorted(
            trades,
            key=lambda t: t.timestamp,
        )
        for t in sorted_trades:
            try:
                ts = datetime.fromisoformat(t.timestamp.replace("Z", "+00:00"))
            except Exception:
                continue
            key = t.token_id
            if t.side.upper() in ("BUY", "YES"):
                buys.setdefault(key, []).append(ts)
            elif buys.get(key):
                buy_ts = buys[key].pop(0)
                delta_h = (ts - buy_ts).total_seconds() / 3600
                if 0 < delta_h < 720:  # ignore > 30d
                    holds.append(delta_h)
        return holds

    def _hour_entropy(self, trades: list[TradeHistory]) -> float:
        hour_counts = [0] * 24
        for t in trades:
            try:
                ts = datetime.fromisoformat(t.timestamp.replace("Z", "+00:00"))
                hour_counts[ts.hour] += 1
            except Exception:
                pass
        total = sum(hour_counts)
        if total == 0:
            return 0.0
        probs = [c / total for c in hour_counts if c > 0]
        entropy = -sum(p * math.log2(p) for p in probs)
        max_entropy = math.log2(24)  # perfectly uniform
        return entropy / max_entropy

    def _near_mid_fraction(self, trades: list[TradeHistory]) -> float:
        # Assume mid-price is ~0.5 for binary markets; "near" = within 5% of round number
        near = sum(
            1
            for t in trades
            if t.price > 0 and (abs(t.price - round(t.price * 20) / 20) < 0.03)
        )
        return near / len(trades) if trades else 0.0
