from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from ..polymarket.data import DataClient, LeaderboardEntry, TraderProfile

log = logging.getLogger(__name__)


@dataclass
class WalletCandidate:
    address: str
    entry_1m: LeaderboardEntry | None
    entry_1w: LeaderboardEntry | None
    profile: TraderProfile | None

    @property
    def pnl(self) -> float:
        if self.entry_1m:
            return self.entry_1m.pnl
        if self.entry_1w:
            return self.entry_1w.pnl
        return 0.0

    @property
    def num_trades(self) -> int:
        if self.profile:
            return self.profile.num_trades
        if self.entry_1m:
            return self.entry_1m.num_trades
        return 0


class LeaderboardFetcher:
    def __init__(self, data_client: DataClient) -> None:
        self._data = data_client

    async def fetch_candidates(self, top_n: int = 200) -> list[WalletCandidate]:
        log.info("Fetching leaderboard candidates (top_n=%d)…", top_n)

        entries_1m, entries_1w = await asyncio.gather(
            self._data.get_leaderboard(window="1m", limit=top_n),
            self._data.get_leaderboard(window="1w", limit=top_n),
        )

        # Merge by address
        by_addr: dict[str, dict[str, Any]] = {}
        for e in entries_1m:
            if e.address:
                by_addr.setdefault(e.address, {})["1m"] = e
        for e in entries_1w:
            if e.address:
                by_addr.setdefault(e.address, {})["1w"] = e

        log.info("Unique addresses found: %d", len(by_addr))

        # Fetch profiles in parallel (max 10 concurrent)
        sem = asyncio.Semaphore(10)

        async def fetch_profile(addr: str) -> tuple[str, TraderProfile | None]:
            async with sem:
                return addr, await self._data.get_trader_profile(addr)

        profiles = await asyncio.gather(*[fetch_profile(a) for a in by_addr])
        profile_map = {addr: prof for addr, prof in profiles}

        candidates = [
            WalletCandidate(
                address=addr,
                entry_1m=slots.get("1m"),
                entry_1w=slots.get("1w"),
                profile=profile_map.get(addr),
            )
            for addr, slots in by_addr.items()
        ]

        # Sort by 1-month PnL descending
        candidates.sort(key=lambda c: c.pnl, reverse=True)
        log.info("Candidates prepared: %d", len(candidates))
        return candidates
