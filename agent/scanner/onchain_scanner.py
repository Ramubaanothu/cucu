"""
OnChainScanner — discovers new profitable traders directly from Polygon blockchain data.

The Polymarket leaderboard only surfaces consistent winners after 2-4 weeks of activity.
This scanner reads OrderFilled events from the CTF Exchange contract via eth_getLogs,
detecting active traders within hours of their first trade.

Flow:
  1. eth_getLogs  → find all active makers in the lookback window
  2. Exclude wallets already known from the leaderboard
  3. Data API     → fetch profile for each new wallet
  4. Return WalletCandidate objects for scoring via RisingStarAnalyzer
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..polymarket.data import DataClient
from .analyzer import WalletAnalyzer
from .leaderboard import WalletCandidate

log = logging.getLogger(__name__)

# Polymarket CTF Exchange on Polygon mainnet
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# keccak256("OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")
# eth_hash is a transitive dep of eth_account
try:
    from eth_hash.auto import keccak as _keccak  # type: ignore[import]
    ORDER_FILLED_TOPIC = "0x" + _keccak(
        b"OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
    ).hex()
except Exception:
    ORDER_FILLED_TOPIC = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06baf5ed7519cddee4ef0ae96"

COLLATERAL_ID = 0            # USDC collateral token ID in CTF Exchange
POLYGON_BLOCKS_PER_DAY = 43_200   # ~2 s block time
LOG_CHUNK = 2_000            # blocks per eth_getLogs call (public-RPC safe)


@dataclass
class OnChainActivity:
    address: str
    trade_count: int = 0
    buy_usdc: float = 0.0
    sell_usdc: float = 0.0
    unique_tokens: set[str] = field(default_factory=set)


class RisingStarAnalyzer(WalletAnalyzer):
    """Lower minimum-trade threshold for freshly discovered on-chain wallets."""
    MIN_TRADE_COUNT = 10


class OnChainScanner:
    """
    Scans the CTF Exchange for active traders not yet visible on the leaderboard.
    Uses HTTP JSON-RPC (polygon_rpc_url) — distinct from the WebSocket used by
    the TypeScript monitor.
    """

    def __init__(
        self,
        rpc_url: str,
        data_client: DataClient,
        lookback_days: int = 3,
        min_trades: int = 10,
    ) -> None:
        self._rpc_url = rpc_url
        self._data = data_client
        self._lookback_days = lookback_days
        self._min_trades = min_trades
        self._http = httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── Public API ───────────────────────────────────────────────────────────────

    async def scan_new_traders(
        self,
        known_addresses: set[str] | None = None,
        max_results: int = 100,
    ) -> list[WalletCandidate]:
        """
        Discover wallets that have been actively trading on the CTF Exchange
        in the last `lookback_days` days but are absent from known_addresses.

        Returns WalletCandidate objects ready for RisingStarAnalyzer.score().
        """
        known = {a.lower() for a in (known_addresses or set())}
        log.info("[OnChainScanner] Scanning last %d days for new active traders…", self._lookback_days)

        try:
            activity = await self._fetch_activity()
        except Exception as exc:
            log.error("[OnChainScanner] eth_getLogs scan failed: %s", exc)
            return []

        new_traders = [
            a for a in activity.values()
            if a.trade_count >= self._min_trades and a.address not in known
        ]
        new_traders.sort(key=lambda a: a.trade_count, reverse=True)
        new_traders = new_traders[:max_results]

        log.info(
            "[OnChainScanner] %d unique makers found on-chain; %d are new (not on leaderboard)",
            len(activity),
            len(new_traders),
        )

        return await self._to_candidates(new_traders)

    # ── eth_getLogs pipeline ─────────────────────────────────────────────────────

    async def _fetch_activity(self) -> dict[str, OnChainActivity]:
        current_block = await self._get_block_number()
        from_block = max(current_block - self._lookback_days * POLYGON_BLOCKS_PER_DAY, 0)

        chunks = [
            (s, min(s + LOG_CHUNK - 1, current_block))
            for s in range(from_block, current_block + 1, LOG_CHUNK)
        ]
        log.info(
            "[OnChainScanner] Fetching blocks %d → %d in %d chunks…",
            from_block, current_block, len(chunks),
        )

        sem = asyncio.Semaphore(5)  # respect public-RPC rate limits

        async def fetch_chunk(start: int, end: int) -> list[dict]:
            async with sem:
                return await self._get_logs(start, end)

        results = await asyncio.gather(
            *[fetch_chunk(s, e) for s, e in chunks],
            return_exceptions=True,
        )

        activity: dict[str, OnChainActivity] = {}
        errors = 0
        for res in results:
            if isinstance(res, Exception):
                errors += 1
                log.debug("[OnChainScanner] Chunk skipped: %s", res)
                continue
            for entry in res:
                self._aggregate_log(entry, activity)

        if errors:
            log.warning("[OnChainScanner] %d/%d chunks failed (partial data)", errors, len(chunks))
        return activity

    def _aggregate_log(self, log_entry: dict, activity: dict[str, OnChainActivity]) -> None:
        try:
            topics = log_entry.get("topics", [])
            if len(topics) < 4:
                return

            # topics[2] = maker, padded to 32 bytes; last 40 hex chars = address
            maker = ("0x" + topics[2][-40:]).lower()

            # Non-indexed data: 5 × uint256, each 32 bytes = 64 hex chars
            # [makerAssetId | takerAssetId | makerAmountFilled | takerAmountFilled | fee]
            raw = log_entry.get("data", "0x")[2:]
            if len(raw) < 320:
                return

            maker_asset_id = int(raw[0:64], 16)
            taker_asset_id = int(raw[64:128], 16)
            maker_amount   = int(raw[128:192], 16)
            taker_amount   = int(raw[192:256], 16)

            if maker_asset_id == COLLATERAL_ID:
                # Maker gives USDC → buying an outcome token
                token_id  = str(taker_asset_id)
                size_usdc = maker_amount / 1e6
                is_buy    = True
            elif taker_asset_id == COLLATERAL_ID:
                # Maker gives outcome token → selling for USDC
                token_id  = str(maker_asset_id)
                size_usdc = taker_amount / 1e6
                is_buy    = False
            else:
                return  # token-to-token swap

            if size_usdc < 0.5:
                return

            if maker not in activity:
                activity[maker] = OnChainActivity(address=maker)
            a = activity[maker]
            a.trade_count += 1
            a.unique_tokens.add(token_id)
            if is_buy:
                a.buy_usdc += size_usdc
            else:
                a.sell_usdc += size_usdc

        except Exception:
            pass  # ignore malformed logs

    # ── JSON-RPC helpers ─────────────────────────────────────────────────────────

    async def _get_block_number(self) -> int:
        result = await self._rpc_call("eth_blockNumber", [])
        return int(result, 16)

    async def _get_logs(self, from_block: int, to_block: int) -> list[dict]:
        result = await self._rpc_call(
            "eth_getLogs",
            [{
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": CTF_EXCHANGE,
                "topics": [ORDER_FILLED_TOPIC],
            }],
        )
        return result if isinstance(result, list) else []

    async def _rpc_call(self, method: str, params: list, retries: int = 3) -> Any:
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        for attempt in range(retries):
            try:
                resp = await self._http.post(self._rpc_url, json=payload)
                resp.raise_for_status()
                body = resp.json()
                if "error" in body:
                    err = body["error"]
                    raise RuntimeError(
                        f"RPC error {err.get('code')}: {err.get('message', err)}"
                    )
                return body["result"]
            except Exception as exc:
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise RuntimeError(
                        f"{method} failed after {retries} attempts: {exc}"
                    ) from exc

    # ── Data API enrichment ──────────────────────────────────────────────────────

    async def _to_candidates(self, traders: list[OnChainActivity]) -> list[WalletCandidate]:
        sem = asyncio.Semaphore(10)

        async def fetch(a: OnChainActivity) -> WalletCandidate | None:
            async with sem:
                try:
                    profile = await self._data.get_trader_profile(a.address)
                    return WalletCandidate(
                        address=a.address,
                        entry_1m=None,   # not on leaderboard yet
                        entry_1w=None,
                        profile=profile,
                    )
                except Exception:
                    return None

        results = await asyncio.gather(*[fetch(a) for a in traders])
        return [r for r in results if r is not None]
