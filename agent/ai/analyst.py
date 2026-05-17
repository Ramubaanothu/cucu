from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import anthropic

from ..scanner.analyzer import WalletScore

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a quantitative trading analyst for a Polymarket copy-trading bot.
Your role:
1. Evaluate which wallets (bots/traders) are worth copying based on performance metrics.
2. Assess individual copy-trade signals before execution, considering market context and risk.
3. Review the portfolio periodically and suggest exits.

Risk limits provided in each call must be respected. Be concise and decisive.
When assessing a trade, return a JSON object in your final message:
{"copy": true/false, "scale": 1.0, "reason": "..."}
- scale: multiplier on the default copy size (0.5 = half size, 1.5 = 50% more, max 2.0)
- reason: one sentence

When ranking wallets, return a JSON array of addresses in priority order:
["0x...", "0x...", ...]
"""

TOOLS = [
    {
        "name": "get_market_details",
        "description": "Get question, end date, and current prices for a Polymarket market.",
        "input_schema": {
            "type": "object",
            "properties": {
                "condition_id": {"type": "string", "description": "Market condition ID"}
            },
            "required": ["condition_id"],
        },
    },
    {
        "name": "get_order_book_snapshot",
        "description": "Get best bid/ask, spread, and top-5 depth for a CLOB token.",
        "input_schema": {
            "type": "object",
            "properties": {
                "token_id": {"type": "string", "description": "CLOB token ID"}
            },
            "required": ["token_id"],
        },
    },
    {
        "name": "check_exposure",
        "description": "Get current total USDC exposure and per-market breakdown.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


@dataclass
class CopyDecision:
    should_copy: bool
    scale: float = 1.0
    reason: str = ""
    confidence: float = 1.0


class AIAnalyst:
    def __init__(self, client: "anthropic.AsyncAnthropic") -> None:
        self._client = client
        self._tool_handlers: dict[str, Any] = {}

    def register_tool_handlers(self, handlers: dict[str, Any]) -> None:
        """Register callables for each tool name."""
        self._tool_handlers = handlers

    async def evaluate_wallets(
        self,
        candidates: list[WalletScore],
        max_select: int = 10,
    ) -> list[str]:
        if not candidates:
            return []

        summary = [
            {
                "address": ws.address,
                "score": ws.composite_score,
                "win_rate": round(ws.win_rate, 3),
                "roi_30d": round(ws.roi_30d, 3),
                "sharpe": round(ws.sharpe, 2),
                "trades": ws.trade_count,
            }
            for ws in candidates[:50]  # cap to avoid huge context
        ]

        prompt = (
            f"Select the top {max_select} wallets to copy from the list below. "
            f"Prioritise: (1) sustained profitability, (2) risk-adjusted returns (Sharpe), "
            f"(3) trade count (reliability of stats). Avoid wallets with <30 trades.\n\n"
            f"Candidates:\n{json.dumps(summary, indent=2)}\n\n"
            f"Return a JSON array of addresses in priority order."
        )

        try:
            resp = await self._client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            # Extract JSON array
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])
        except Exception as exc:
            log.warning("Claude wallet evaluation failed: %s", exc)

        # Fallback: return top by composite score
        return [ws.address for ws in candidates[:max_select]]

    async def assess_copy_trade(
        self,
        source_wallet: str,
        market_id: str,
        token_id: str,
        side: str,
        size_usdc: float,
        price: float,
        current_exposure: float,
        max_exposure: float,
    ) -> CopyDecision:
        prompt = (
            f"Assess this copy-trade signal:\n"
            f"  Source wallet: {source_wallet}\n"
            f"  Market condition_id: {market_id}\n"
            f"  Token: {token_id}\n"
            f"  Side: {side}  Price: {price:.3f}  Size: ${size_usdc:.2f} USDC\n"
            f"  Current portfolio exposure: ${current_exposure:.2f} / ${max_exposure:.2f} USDC\n\n"
            f"Use available tools to check market details and order book. "
            f"Then respond with JSON: {{\"copy\": true/false, \"scale\": 1.0, \"reason\": \"...\"}}"
        )

        messages = [{"role": "user", "content": prompt}]

        try:
            result = await asyncio.wait_for(
                self._run_tool_loop(messages), timeout=8.0
            )
            return result
        except asyncio.TimeoutError:
            log.warning("Claude timed out for trade assessment; using default COPY")
            return CopyDecision(should_copy=True, scale=1.0, reason="Claude timeout — default copy")
        except Exception as exc:
            log.warning("Claude assessment error: %s", exc)
            return CopyDecision(should_copy=True, scale=1.0, reason=f"Claude error: {exc}")

    async def _run_tool_loop(self, messages: list[dict]) -> CopyDecision:
        for _ in range(5):  # max 5 turns
            resp = await self._client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOLS,
                messages=messages,
            )

            if resp.stop_reason == "tool_use":
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        handler = self._tool_handlers.get(block.name)
                        if handler:
                            try:
                                result = await handler(**block.input)
                            except Exception as exc:
                                result = {"error": str(exc)}
                        else:
                            result = {"error": f"No handler for {block.name}"}
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result),
                            }
                        )
                messages = messages + [
                    {"role": "assistant", "content": resp.content},
                    {"role": "user", "content": tool_results},
                ]
                continue

            # End of loop — extract decision
            text = ""
            for block in resp.content:
                if hasattr(block, "text"):
                    text += block.text

            try:
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    d = json.loads(text[start:end])
                    return CopyDecision(
                        should_copy=bool(d.get("copy", True)),
                        scale=float(d.get("scale", 1.0)),
                        reason=str(d.get("reason", "")),
                    )
            except Exception:
                pass

            return CopyDecision(should_copy=True, scale=1.0, reason=text[:200])

        return CopyDecision(should_copy=True, scale=1.0, reason="Max tool loops reached")
