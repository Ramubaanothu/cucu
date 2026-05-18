from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass
class TradeRequest:
    market_id: str
    token_id: str
    condition_id: str
    side: Literal["YES", "NO"]
    size_usdc: float
    price: float
    source_wallet: str
    signal_confidence: float = 1.0
    reason: str = ""


@dataclass
class TradeResult:
    success: bool
    position_id: int | None
    executed_size: float
    executed_price: float
    order_id: str | None
    error: str | None
    mode: Literal["paper", "live"]


class BaseExecutor(ABC):
    @abstractmethod
    async def open_position(self, req: TradeRequest) -> TradeResult: ...

    @abstractmethod
    async def close_position(self, position_id: int, reason: str) -> TradeResult: ...

    @abstractmethod
    async def get_open_positions(self) -> list: ...
