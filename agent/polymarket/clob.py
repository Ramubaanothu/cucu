from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


BASE_URL = "https://clob.polymarket.com"
_TIMEOUT = 30.0
_MAX_RETRIES = 3


@dataclass
class CLOBCredentials:
    api_key: str
    api_secret: str
    api_passphrase: str
    private_key: str


@dataclass
class OrderBook:
    token_id: str
    bids: list[dict[str, float]] = field(default_factory=list)
    asks: list[dict[str, float]] = field(default_factory=list)
    mid_price: float = 0.0
    spread: float = 0.0

    @classmethod
    def from_dict(cls, token_id: str, d: dict[str, Any]) -> "OrderBook":
        bids = [{"price": float(b["price"]), "size": float(b["size"])} for b in d.get("bids", [])]
        asks = [{"price": float(a["price"]), "size": float(a["size"])} for a in d.get("asks", [])]
        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
        spread = best_ask - best_bid if best_bid and best_ask else 1.0
        return cls(token_id=token_id, bids=bids, asks=asks, mid_price=mid, spread=spread)


@dataclass
class CLOBTrade:
    id: str
    market_id: str
    token_id: str
    side: str
    size: float
    price: float
    maker_address: str
    timestamp: str
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CLOBTrade":
        return cls(
            id=str(d.get("id") or ""),
            market_id=str(d.get("market") or d.get("condition_id") or ""),
            token_id=str(d.get("asset_id") or d.get("token_id") or ""),
            side=str(d.get("side") or "BUY"),
            size=float(d.get("size") or 0),
            price=float(d.get("price") or 0),
            maker_address=str(d.get("maker_address") or "").lower(),
            timestamp=str(d.get("timestamp") or d.get("created_at") or ""),
            raw=d,
        )


class ClobClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        self._semaphore = asyncio.Semaphore(20)  # rate-limit guard

    def _build_auth_headers(
        self, creds: CLOBCredentials, method: str, path: str, body: str = ""
    ) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        msg = ts + method.upper() + path + body
        sig = hmac.new(
            creds.api_secret.encode(),
            msg.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "POLY-API-KEY": creds.api_key,
            "POLY-TIMESTAMP": ts,
            "POLY-SIGNATURE": sig,
            "POLY-PASSPHRASE": creds.api_passphrase,
        }

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with self._semaphore:
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
        raise RuntimeError(f"CLOB GET failed: {path}")

    async def _post(
        self,
        path: str,
        body: dict,
        creds: CLOBCredentials,
    ) -> Any:
        import json as _json

        body_str = _json.dumps(body)
        headers = self._build_auth_headers(creds, "POST", path, body_str)
        async with self._semaphore:
            for attempt in range(_MAX_RETRIES):
                try:
                    resp = await self._client.post(
                        path,
                        content=body_str,
                        headers={**headers, "Content-Type": "application/json"},
                    )
                    resp.raise_for_status()
                    return resp.json()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise
        raise RuntimeError(f"CLOB POST failed: {path}")

    async def get_order_book(self, token_id: str) -> OrderBook:
        data = await self._get(f"/book", params={"token_id": token_id})
        return OrderBook.from_dict(token_id, data)

    async def get_trades(
        self,
        market_id: str | None = None,
        maker_address: str | None = None,
        limit: int = 100,
    ) -> list[CLOBTrade]:
        params: dict = {"limit": limit}
        if market_id:
            params["market"] = market_id
        if maker_address:
            params["maker_address"] = maker_address
        try:
            data = await self._get("/trades", params=params)
            items = data if isinstance(data, list) else data.get("data", [])
            return [CLOBTrade.from_dict(t) for t in items]
        except Exception:
            return []

    async def place_order(
        self,
        order: dict,
        creds: CLOBCredentials,
    ) -> dict:
        """Place a signed order. `order` should be pre-built by py-clob-client SDK."""
        return await self._post("/order", order, creds)

    async def cancel_order(
        self, order_id: str, creds: CLOBCredentials
    ) -> bool:
        try:
            await self._post(f"/order/{order_id}", {"action": "cancel"}, creds)
            return True
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
