from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx


BASE_URL = "https://gamma-api.polymarket.com"
_TIMEOUT = 30.0
_MAX_RETRIES = 3


@dataclass
class MarketMeta:
    condition_id: str
    question: str
    description: str
    category: str
    end_date: str
    active: bool
    closed: bool
    tokens: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MarketMeta":
        return cls(
            condition_id=d.get("conditionId", d.get("condition_id", "")),
            question=d.get("question", ""),
            description=d.get("description", ""),
            category=d.get("category", ""),
            end_date=d.get("endDate", d.get("end_date_iso", "")),
            active=d.get("active", False),
            closed=d.get("closed", False),
            tokens=d.get("tokens", []),
            raw=d,
        )

    @property
    def winning_token_id(self) -> str | None:
        """Return the token ID of the winning outcome, or None if not yet resolved."""
        for t in self.tokens:
            if t.get("winner") is True or str(t.get("winner", "")).lower() == "true":
                return str(
                    t.get("token_id") or t.get("tokenID") or t.get("tokenId") or ""
                ) or None
        return None

    @property
    def normalized_category(self) -> str:
        return (self.category or "unknown").lower().strip() or "unknown"


class GammaClient:
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
                if exc.response.status_code == 429 or exc.response.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError(f"Gamma API failed after {_MAX_RETRIES} retries: {path}")

    async def get_markets(
        self,
        active: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MarketMeta]:
        data = await self._get(
            "/markets",
            params={"active": str(active).lower(), "limit": limit, "offset": offset},
        )
        items = data if isinstance(data, list) else data.get("data", data.get("markets", []))
        return [MarketMeta.from_dict(m) for m in items]

    async def get_market(self, condition_id: str) -> MarketMeta | None:
        try:
            data = await self._get(f"/markets/{condition_id}")
            return MarketMeta.from_dict(data)
        except Exception:
            return None

    async def search_markets(self, query: str, limit: int = 20) -> list[MarketMeta]:
        data = await self._get("/markets", params={"search": query, "limit": limit})
        items = data if isinstance(data, list) else data.get("data", [])
        return [MarketMeta.from_dict(m) for m in items]

    async def get_resolved_markets(
        self, since_hours: float = 6.0, limit: int = 50
    ) -> list[MarketMeta]:
        """Fetch markets that closed/resolved within the last since_hours hours."""
        from datetime import datetime, timedelta, timezone
        try:
            data = await self._get(
                "/markets",
                params={"closed": "true", "limit": limit, "order": "end_date_iso", "ascending": "false"},
            )
            items = data if isinstance(data, list) else data.get("data", data.get("markets", []))
            markets = [MarketMeta.from_dict(m) for m in items]
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            recent = []
            for m in markets:
                if not m.end_date:
                    continue
                try:
                    end = datetime.fromisoformat(m.end_date.replace("Z", "+00:00"))
                    if end >= cutoff:
                        recent.append(m)
                except Exception:
                    pass
            return recent
        except Exception:
            return []

    async def aclose(self) -> None:
        await self._client.aclose()
