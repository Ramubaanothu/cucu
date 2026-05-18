"""
DomainScorer — per-category win rate analysis for each watched wallet.

A wallet averaging 55% overall may be 85% accurate on crypto markets and 40% on
politics. Knowing this lets us:
  1. Copy the wallet ONLY when the signal is in their strong domain (higher precision)
  2. Adjust copy_scale up for domain matches, down (or skip) for domain mismatches
  3. Surface "hidden experts" that the composite score undervalues

domain_scores format: {"crypto": 0.82, "politics": 0.48, "sports": 0.71}
Categories come from Gamma API MarketMeta.category.

Copy-scale adjustment (applied in CopyTrader.handle_signal):
  strong domain  (win_rate ≥ 0.70)  → scale × 1.5
  neutral        (win_rate 0.45–0.70) → scale × 1.0 (unchanged)
  weak domain    (win_rate < 0.45)   → scale × 0.5
  unknown                             → scale × 0.8
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from ..polymarket.data import TradeHistory
from ..polymarket.gamma import GammaClient

log = logging.getLogger(__name__)

_MIN_CATEGORY_TRADES    = 5     # need at least this many trades to trust a category score
_STRONG_DOMAIN_WIN_RATE = 0.70  # wallet has a real edge in this category
_WEAK_DOMAIN_WIN_RATE   = 0.45  # wallet is a net loser in this category

# Copy-scale multipliers based on domain match
DOMAIN_SCALE_STRONG  = 1.5
DOMAIN_SCALE_NEUTRAL = 1.0
DOMAIN_SCALE_WEAK    = 0.5
DOMAIN_SCALE_UNKNOWN = 0.8


class DomainScorer:
    """Computes per-category win rates using an in-process Gamma API cache."""

    def __init__(self, gamma: GammaClient) -> None:
        self._gamma = gamma
        self._cache: dict[str, str] = {}  # condition_id → category

    async def score(self, trades: list[TradeHistory]) -> dict[str, float]:
        """
        Returns {category: win_rate} for categories with >= MIN_CATEGORY_TRADES.
        """
        market_ids = {t.market_id for t in trades if t.market_id}
        await self._warm_cache(market_ids)

        by_cat: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            cat = self._cache.get(t.market_id, "unknown") or "unknown"
            by_cat[cat].append(t.profit)

        return {
            cat: round(sum(1 for p in profits if p > 0) / len(profits), 3)
            for cat, profits in by_cat.items()
            if len(profits) >= _MIN_CATEGORY_TRADES
        }

    def copy_scale_multiplier(
        self,
        domain_scores: dict[str, float],
        market_category: str,
    ) -> float:
        """
        Return the copy_scale multiplier for a given signal market category.
        Called from CopyTrader.handle_signal() at trade time.
        """
        cat = (market_category or "").lower().strip()
        if not cat or cat not in domain_scores:
            return DOMAIN_SCALE_UNKNOWN
        wr = domain_scores[cat]
        if wr >= _STRONG_DOMAIN_WIN_RATE:
            return DOMAIN_SCALE_STRONG
        if wr >= _WEAK_DOMAIN_WIN_RATE:
            return DOMAIN_SCALE_NEUTRAL
        return DOMAIN_SCALE_WEAK

    def strong_domains(self, domain_scores: dict[str, float]) -> list[str]:
        return [c for c, wr in domain_scores.items() if wr >= _STRONG_DOMAIN_WIN_RATE]

    # ── Cache warmup ─────────────────────────────────────────────────────────────

    async def _warm_cache(self, market_ids: set[str]) -> None:
        uncached = [mid for mid in market_ids if mid not in self._cache]
        if not uncached:
            return

        sem = asyncio.Semaphore(10)

        async def fetch(mid: str) -> None:
            async with sem:
                try:
                    meta = await self._gamma.get_market(mid)
                    self._cache[mid] = meta.normalized_category if meta else "unknown"
                except Exception:
                    self._cache[mid] = "unknown"

        await asyncio.gather(*[fetch(mid) for mid in uncached])
