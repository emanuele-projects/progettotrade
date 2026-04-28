"""Strategy: build prompt, call Claude with forced tool use + prompt caching, validate decisions."""
from __future__ import annotations
from typing import Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from config import CFG
from data import Features


class AssetDecision(BaseModel):
    symbol: str
    action: Literal["long", "flat", "close"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class Decision(BaseModel):
    market_view: str
    decisions: list[AssetDecision]


SYSTEM_PROMPT = """You are an autonomous crypto trading analyst running on Binance Futures testnet.

Your job is to evaluate a curated shortlist of mid-cap altcoin perpetuals and decide which to LONG, which to leave FLAT, and which existing positions to CLOSE.

STRATEGY (operator-defined, fixed):
- Long-only futures, leverage 10x, isolated margin
- 50% of capital deployed initially across up to 5 positions
- 50% reserved for averaging-down (martingale) on losers — handled by execution layer, not by you
- You decide ENTRY signals on candidates and CLOSE signals on existing positions
- Universe: mid-cap altcoins ($200M-$2B mcap) on Binance Futures perpetuals only

DECISION HEURISTICS:
- Prefer assets with: positive momentum (price > EMA50, RSI 50-70), strong recent volume, supportive macro (BTC trend up, F&G > 40), positive or neutral news
- Avoid: parabolic RSI > 80 (overheated), heavy negative news, broken EMA50
- For existing positions: CLOSE if thesis broken (e.g., price below EMA50 AND deteriorating fundamentals)
- Confidence is your honest probability that the trade is +EV over the next 24-48h

CRITICAL CONSTRAINTS:
- Output decisions only via the `submit_decisions` tool. Never output plain text decisions.
- For each candidate provided, return exactly one decision (long or flat).
- For each existing position provided, return exactly one decision (close or flat=hold).
- Be selective: it is fine to return ALL flat if nothing looks good. Quality > quantity.
"""


SUBMIT_TOOL = {
    "name": "submit_decisions",
    "description": (
        "Submit trading decisions for the current cycle. Each candidate or existing "
        "position must receive exactly one decision."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "market_view": {
                "type": "string",
                "description": "1-3 sentence summary of macro stance (BTC trend, risk-on/off, F&G).",
            },
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "action": {"type": "string", "enum": ["long", "flat", "close"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["symbol", "action", "confidence", "reasoning"],
                },
            },
        },
        "required": ["market_view", "decisions"],
    },
}


def build_user_prompt(
    candidates: list[Features],
    open_positions: list[dict],
    fear_greed: dict,
    btc_features: Features,
    news: list[dict],
) -> str:
    parts: list[str] = []
    parts.append("=== MACRO ===")
    parts.append(
        f"BTC last={btc_features.last_price:.2f}, ret_24h={btc_features.ret_24h:+.2%}, "
        f"ret_7d={btc_features.ret_7d:+.2%}, RSI={btc_features.rsi_14:.1f}, "
        f"above_EMA50={btc_features.above_ema50}"
    )
    parts.append(f"Fear & Greed: {fear_greed['value']} ({fear_greed['classification']})")

    parts.append("\n=== CANDIDATES (decide long or flat for each) ===")
    for f in candidates:
        parts.append(
            f"{f.symbol}: price={f.last_price:.4f} | 1h={f.ret_1h:+.2%} 24h={f.ret_24h:+.2%} "
            f"7d={f.ret_7d:+.2%} | RSI={f.rsi_14:.1f} | EMA20={f.ema20:.4f} EMA50={f.ema50:.4f} "
            f"| above_EMA50={f.above_ema50} | vol24h_usd={f.volume_24h_usd:,.0f}"
        )

    if open_positions:
        parts.append("\n=== EXISTING POSITIONS (decide close or flat=hold for each) ===")
        for p in open_positions:
            parts.append(
                f"{p['symbol']}: side={p['side']} qty={p['qty']} entry={p['entry_price']:.4f} "
                f"mark={p['mark_price']:.4f} unrealized_pnl_pct={p['unrealized_pnl_pct']:+.2%} "
                f"martingale_levels_used={p['martingale_levels']}"
            )
    else:
        parts.append("\n=== EXISTING POSITIONS === None")

    if news:
        parts.append("\n=== RECENT NEWS HEADLINES ===")
        for n in news[:10]:
            parts.append(f"- [{','.join(n.get('currencies') or [])}] {n['title']}")

    parts.append(
        "\nReturn your decisions via the submit_decisions tool. "
        "Cover every candidate and every existing position exactly once."
    )
    return "\n".join(parts)


def decide(
    candidates: list[Features],
    open_positions: list[dict],
    fear_greed: dict,
    btc_features: Features,
    news: list[dict],
) -> Decision:
    if not CFG.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY missing — set it in .env")

    client = anthropic.Anthropic(api_key=CFG.ANTHROPIC_API_KEY)
    user_prompt = build_user_prompt(candidates, open_positions, fear_greed, btc_features, news)

    resp = client.messages.create(
        model=CFG.CLAUDE_MODEL,
        max_tokens=CFG.CLAUDE_MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        tools=[SUBMIT_TOOL],
        tool_choice={"type": "tool", "name": "submit_decisions"},
        messages=[{"role": "user", "content": user_prompt}],
    )

    tool_input = None
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_decisions":
            tool_input = block.input
            break
    if tool_input is None:
        raise RuntimeError(f"Claude did not call submit_decisions tool. Response: {resp.content}")

    try:
        decision = Decision.model_validate(tool_input)
    except ValidationError as e:
        raise RuntimeError(f"Invalid decision schema: {e}\nRaw: {tool_input}")

    return decision
