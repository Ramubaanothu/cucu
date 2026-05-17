from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx


BASE_URL = "https://data-api.polymarket.com"
_TIMEOUT = 30.0
_MAX_RETRIES = 3


@dataclass
class LeaderboardEntry:
    address: str
    pnl: float
    volume: float
    num_trades: int
    win_rate: float
    roi: float
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LeaderboardEntry":
        return cls(
            address=(d.get("user") or d.get("address") or "").lower(),
            pnl=float(d.get("pnl") or d.get("profit") or 0),
            volume=float(d.get("volume") or 0),
            num_trades=int(d.get("numTrades") or d.get("num_trades") or 0),
            win_rate=float(d.get("winRate") or d.get("win_rate") or 0),
            roi=float(d.get("roi") or 0),
            raw=d,
        )


@dataclass
class TraderProfile:
    address: str
    pnl_all_time: float
    pnl_30d: float
    pnl_7d: float
    num_trades: int
    markets_traded: int
    positions_value: float
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TraderProfile":
        return cls(
            address=(d.get("user") or d.get("address") or "").lower(),
            pnl_all_time=float(d.get("pnlAllTime") or d.get("pnl") or 0),
            pnl_30d=float(d.get("pnl1m") or d.get("pnl30d") or 0),
            pnl_7d=float(d.get("pnl1w") or d.get("pnl7d") or 0),
            num_trades=int(d.get("numTrades") or d.get("num_trades") or 0),
            markets_traded=int(d.get("marketsTraded") or d.get("markets_traded") or 0),
            positions_value=float(d.get("positionsValue") or 0),
            raw=d,
        )


@dataclass
class TradeHistory:
    id: str
    address: str
    market_id: str
    token_id: str
    side: str
    size: float
    price: float
    outcome: str  # YES | NO
    timestamp: str
    profit: float
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TradeHistory":
        return cls(
            id=str(d.get("id") or d.get("tradeId") or ""),
            address=(d.get("user") or d.get("proxyWallet") or "").lower(),
            market_id=str(d.get("conditionId") or d.get("market_id") or ""),
            token_id=str(d.get("asset") or d.get("token_id") or ""),
            side=str(d.get("side") or "BUY"),
            size=float(d.get("size") or d.get("usdcSize") or 0),
            price=float(d.get("price") or 0),
            outcome=str(d.get("outcome") or ""),
            timestamp=str(d.get("timestamp") or d.get("createdAt") or ""),
            profit=float(d.get("profit") or 0),
            raw=d,
        )


class DataClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=_TIMEOUT,
            headers={"Accept": "application/json"},
        )

    async def _get(self, path: str, params: dict | None = None) -> Any:
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.get(path, params=params)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (429,) or exc.response.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError(f"Data API failed after {_MAX_RETRIES} retries: {path}")

    async def get_leaderboard(
        self, window: str = "1m", limit: int = 100
    ) -> list[LeaderboardEntry]:
        try:
            data = await self._get(
                "/leaderboard",
                params={"window": window, "limit": limit},
            )
            items = data if isinstance(data, list) else data.get("data", [])
            return [LeaderboardEntry.from_dict(e) for e in items if e.get("user") or e.get("address")]
        except Exception:
            return []

    async def get_trader_profile(self, address: str) -> TraderProfile | None:
        try:
            data = await self._get(f"/profile", params={"user": address})
            if isinstance(data, list):
                data = data[0] if data else {}
            return TraderProfile.from_dict(data)
        except Exception:
            return None

    async def get_trader_trades(
        self, address: str, limit: int = 200
    ) -> list[TradeHistory]:
        try:
            data = await self._get(
                "/activity",
                params={"user": address, "limit": limit},
            )
            items = data if isinstance(data, list) else data.get("data", [])
            return [TradeHistory.from_dict(t) for t in items]
        except Exception:
            return []

    async def get_trader_positions(
        self, address: str, limit: int = 50
    ) -> list[dict]:
        try:
            data = await self._get(
                "/positions",
                params={"user": address, "limit": limit},
            )
            return data if isinstance(data, list) else data.get("data", [])
        except Exception:
            return []

    async def aclose(self) -> None:
        await self._client.aclose()
