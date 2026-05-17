from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Wallet(Base):
    __tablename__ = "wallet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(100), default="")
    composite_score: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    roi_30d: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe: Mapped[float] = mapped_column(Float, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_trade_size: Mapped[float] = mapped_column(Float, default=0.0)
    trade_frequency_per_day: Mapped[float] = mapped_column(Float, default=0.0)
    is_bot_likely: Mapped[bool] = mapped_column(default=False)
    bot_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(default=True)
    domain_scores: Mapped[str] = mapped_column(Text, default="{}")   # JSON {category: win_rate}
    discovery_source: Mapped[str] = mapped_column(String(20), default="leaderboard")  # leaderboard | onchain | resolution | copycat
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Position(Base):
    __tablename__ = "position"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    token_id: Mapped[str] = mapped_column(String(200), nullable=False)
    condition_id: Mapped[str] = mapped_column(String(200), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # YES | NO
    size_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | closed | cancelled
    mode: Mapped[str] = mapped_column(String(10), nullable=False)  # paper | live
    source_wallet: Mapped[str] = mapped_column(String(42), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TradeLog(Base):
    __tablename__ = "trade_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # open | close | skip
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    market_id: Mapped[str] = mapped_column(String(200), nullable=False)
    token_id: Mapped[str] = mapped_column(String(200), default="")
    side: Mapped[str] = mapped_column(String(4), default="")
    requested_size: Mapped[float] = mapped_column(Float, default=0.0)
    executed_size: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    source_wallet: Mapped[str] = mapped_column(String(42), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    raw_response: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ScanRun(Base):
    __tablename__ = "scan_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    wallets_found: Mapped[int] = mapped_column(Integer, default=0)
    wallets_selected: Mapped[int] = mapped_column(Integer, default=0)
    top_wallet_addresses: Mapped[str] = mapped_column(Text, default="[]")  # JSON
    notes: Mapped[str] = mapped_column(Text, default="")

    def set_addresses(self, addrs: list[str]) -> None:
        self.top_wallet_addresses = json.dumps(addrs)

    def get_addresses(self) -> list[str]:
        return json.loads(self.top_wallet_addresses or "[]")
