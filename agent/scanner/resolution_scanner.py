"""
ResolutionScanner — identifies wallets that correctly called recently resolved markets.

When a Polymarket market resolves the outcome is on-chain truth. We immediately know
who held the winning token and at what entry price. A wallet that bought YES at 0.12
when the market resolved YES had a real edge — no need to wait weeks on the leaderboard.

Trigger conditions (wallet must meet ALL):
  - Bought the winning token at price ≤ 0.70  (conviction before it was obvious)
  - Trade size ≥ $10 USDC                      (real money, not dust)
  - Wallet passes RisingStarAnalyzer threshold  (some trading history)

Runs every 5 minutes, processing only markets resolved since last check.
"""

from __future__ import annotations

import asyncio
import logging

from ..polymarket.data import DataClient
from ..polymarket.gamma import GammaClient, MarketMeta
from .leaderboard import WalletCandidate
from .onchain_scanner import RisingStarAnalyzer

log = logging.getLogger(__name__)

_MAX_ENTRY_PRICE  = 0.70   # only count buys made when outcome was uncertain
_MIN_WIN_SIZE_USDC = 10.0  # ignore dust trades
_RESOLUTION_MIN_SCORE = 45.0


class ResolutionScanner:
    def __init__(
        self,
        gamma: GammaClient,
        data: DataClient,
        lookback_hours: float = 6.0,
    ) -> None:
        self._gamma = gamma
        self._data = data
        self._lookback_hours = lookback_hours
        self._processed: set[str] = set()  # condition_ids already handled

    async def scan(self, known_addresses: set[str] | None = None) -> list[WalletCandidate]:
        """
        Check recently resolved markets, find wallets that were correctly positioned,
        and return scored WalletCandidate objects for any not already being watched.
        """
        known = {a.lower() for a in (known_addresses or set())}
        resolved = await self._gamma.get_resolved_markets(since_hours=self._lookback_hours)
        new = [m for m in resolved if m.condition_id and m.condition_id not in self._processed]

        if not new:
            return []

        log.info("[ResolutionScanner] Processing %d newly resolved markets", len(new))

        sem = asyncio.Semaphore(5)

        async def handle_market(market: MarketMeta) -> list[str]:
            async with sem:
                return await self._winning_wallets(market)

        results = await asyncio.gather(*[handle_market(m) for m in new], return_exceptions=True)

        winners: set[str] = set()
        for r in results:
            if isinstance(r, list):
                winners.update(r)

        for m in new:
            self._processed.add(m.condition_id)

        new_winners = [w for w in winners if w not in known]
        if not new_winners:
            return []

        log.info(
            "[ResolutionScanner] %d winning wallets found (%d new, not yet watched)",
            len(winners), len(new_winners),
        )
        return await self._to_candidates(new_winners)

    # ── Internal ────────────────────────────────────────────────────────────────

    async def _winning_wallets(self, market: MarketMeta) -> list[str]:
        winning_token = market.winning_token_id
        if not winning_token:
            return []

        trades = await self._data.get_market_trades(market.condition_id)
        winners: set[str] = set()
        for t in trades:
            if (
                t.token_id == winning_token
                and t.side.upper() in ("BUY",)
                and 0 < t.price <= _MAX_ENTRY_PRICE
                and t.size >= _MIN_WIN_SIZE_USDC
                and t.address
            ):
                winners.add(t.address.lower())

        if winners:
            log.debug(
                "[ResolutionScanner] %s resolved → %d wallets on winning side (entry ≤ %.0f%%)",
                market.condition_id[:12], len(winners), _MAX_ENTRY_PRICE * 100,
            )
        return list(winners)

    async def _to_candidates(self, addresses: list[str]) -> list[WalletCandidate]:
        analyzer = RisingStarAnalyzer()
        sem = asyncio.Semaphore(8)
        scored: list[WalletCandidate] = []

        async def evaluate(address: str) -> None:
            async with sem:
                profile = await self._data.get_trader_profile(address)
                trades  = await self._data.get_trader_trades(address, limit=200)

            candidate = WalletCandidate(address=address, entry_1m=None, entry_1w=None, profile=profile)
            score = analyzer.score(candidate, trades)
            score.raw_metrics["source"] = "resolution"

            if score.composite_score >= _RESOLUTION_MIN_SCORE or score.trade_count >= 5:
                scored.append(candidate)

        await asyncio.gather(*[evaluate(a) for a in addresses], return_exceptions=True)
        log.info("[ResolutionScanner] %d/%d winning wallets pass threshold", len(scored), len(addresses))
        return scored
