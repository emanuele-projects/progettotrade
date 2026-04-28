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

You evaluate a curated shortlist of perpetual contracts — a mix of large-cap anchors (BTC/ETH/SOL/BNB/XRP) and dynamically-selected mid-cap altcoins — and decide which to LONG, leave FLAT, or CLOSE.

STRATEGY (operator-defined, fixed):
- Long-only futures, leverage 10x, isolated margin
- 50% of capital deployed initially across up to 8 positions ($10k total → ~$625 margin per entry)
- 50% reserved for averaging-down (martingale) on losers — handled by execution layer, not by you
- You decide ENTRY signals on candidates and CLOSE signals on existing positions

DECISION HEURISTICS — TECHNICALS:
- Prefer: positive momentum (price > EMA50, RSI 50-70), strong recent volume, supportive macro (BTC above EMA50, F&G > 40), positive/neutral news
- Avoid: parabolic RSI > 80 (overheated), heavy negative news, broken EMA50
- CLOSE existing if thesis broken: price below EMA50 AND deteriorating flow

DECISION HEURISTICS — FUTURES FLOW (real-time):
- funding_8h: signed 8h funding rate paid by longs to shorts.
  - Mildly positive (0% to +0.03%) = healthy bullish leverage, OK to long.
  - Very high (>+0.05%) = market overcrowded long, mean-reversion risk → reduce conviction.
  - Negative (<−0.02%) = bear sentiment; can be a contrarian long if technicals confirm.
- OI_24h: open-interest change vs 24h ago (fraction).
  - OI up + price up = real momentum (smart money adding longs) → high conviction.
  - OI up + price down = bears adding, trap risk → avoid long.
  - OI down + price up = short covering, fragile → low conviction.
- top_trader_long: share of top traders net-long (0..1).
  - 0.55–0.70 with bullish technicals = healthy confirmation.
  - >0.80 = excessive optimism, contra-indicator.
  - <0.40 with bullish technicals = contrarian opportunity.

PORTFOLIO CONSTRUCTION:
- Aim for a mix: 1–2 large-cap anchors (risk_tier=large_cap) for stability + the rest mid-cap (risk_tier=mid_cap) for upside.
- Don't load only mid-caps; don't load only large-caps. Balance.
- Max 8 concurrent longs total. Quality > quantity — flat is always a valid answer.

CRITICAL CONSTRAINTS:
- Output decisions only via the `submit_decisions` tool.
- For each candidate, return exactly one decision (long or flat).
- For each existing position, return exactly one decision (close or flat=hold).
- Confidence = your honest probability the trade is +EV over the next 24-48h.
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
    parts.append(
        "Format: SYMBOL [tier] | technicals | futures-flow"
    )
    for f in candidates:
        parts.append(
            f"{f.symbol} [{f.risk_tier}] | "
            f"price={f.last_price:.4f} 1h={f.ret_1h:+.2%} 24h={f.ret_24h:+.2%} 7d={f.ret_7d:+.2%} "
            f"RSI={f.rsi_14:.1f} above_EMA50={f.above_ema50} vol24h_usd={f.volume_24h_usd:,.0f} "
            f"| funding_8h={f.funding_rate_8h:+.4%} OI_24h={f.open_interest_change_24h:+.2%} "
            f"top_trader_long={f.top_trader_long_pct:.0%}"
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
