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
    # All three required when action == "long"; ignored otherwise.
    # Stops are fraction of COLLATERAL (margin), NOT of asset price.
    stop_loss_pct: float | None = Field(default=None, ge=-0.50, le=-0.05)
    take_profit_pct: float | None = Field(default=None, ge=0.05, le=0.50)
    leverage: Literal[5, 10] | None = Field(default=None)


class Decision(BaseModel):
    market_view: str
    decisions: list[AssetDecision]


SYSTEM_PROMPT = """You are an autonomous crypto trading analyst running on Binance Futures testnet.

You evaluate a curated shortlist of perpetual contracts — a mix of large-cap anchors (BTC/ETH/SOL/BNB/XRP) and dynamically-selected mid-cap altcoins — and decide which to LONG, leave FLAT, or CLOSE.

STRATEGY (operator-defined, fixed):
- Long-only futures, isolated margin
- 50% of capital deployed initially across up to 10 positions ($10k total → $500 margin per entry)
- 50% reserved for averaging-down (martingale) on losers — handled by execution layer, not by you
- You decide ENTRY signals, CLOSE signals, per-position protective stops, AND per-position leverage (5x or 10x)

DECISION HEURISTICS — TECHNICALS (1h, 4h, daily):
- Trust trends that align across timeframes: above_EMA50 on 1h AND 4h AND daily = strongest. Single-timeframe agreement is weaker signal.
- Prefer: positive momentum (price > EMA50 on multiple TFs), RSI 50-70 on the 4h, strong recent volume, supportive macro (BTC above EMA50, F&G > 40), positive/neutral news.
- Avoid: parabolic RSI > 80 on 1h or 4h (overheated), broken EMA50 on the 4h or daily, heavy negative news.
- Use `dist_from_high_30d` and `dist_from_low_30d` to gauge mean-reversion risk: price near 30d high (dist≈0%) on a frothy run = risky long; price recovering from 30d low with confirmed flow = high R/R.
- `atr_pct_24h` is the asset's recent volatility. Higher ATR ⇒ wider stops needed (use leverage=5 + larger SL). Lower ATR ⇒ tighter stops viable (leverage=10 + smaller SL).

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

LEVERAGE CHOICE (mandatory for every long — pick 5 or 10):
- Leverage amplifies P&L on the margin. With leverage L, an asset move of X% becomes L·X% on collateral.
- 10x: high-conviction setup with a TIGHT technical invalidation level (e.g. price holds above clear support, momentum confirmed). Reaches SL/TP faster — good for clean trends, dangerous in chop.
- 5x: setup is good but the asset is volatile / has wide ATR / SL needs room to breathe. Same collateral SL of -20% triggers at -4% price (not -2%), giving the trade room before getting stopped on noise.
- Default to 5x when in doubt; reserve 10x for the cleanest, tightest setups.

PROTECTIVE STOPS (mandatory for every long — set stop_loss_pct AND take_profit_pct):
- Both percentages are on the COLLATERAL (margin), not on the asset price.
  Translation depends on the leverage you chose:
    leverage 10x: SL=-20% on collateral ⇔ -2% adverse move on price.
    leverage 5x:  SL=-20% on collateral ⇔ -4% adverse move on price.
- Allowed range: stop_loss_pct ∈ [-0.50, -0.05], take_profit_pct ∈ [+0.05, +0.50].
- Minimum risk/reward: aim for TP/|SL| ≥ 1.5 (e.g. SL=-20% paired with TP=+30%+).
- Calibrate stops to the SETUP and chosen leverage:
  - Low-vol large-cap, leverage 10x, confirmed trend → SL≈-15% to -20%, TP≈+20% to +30%
  - Mid-cap with strong momentum, leverage 5x → SL≈-25% to -35%, TP≈+40% to +60% (price has room to breathe)
  - High-conviction tight technical, leverage 10x → SL≈-15%, TP≈+25%
  - Marginal setup → wider SL won't save you; if you can't justify good R/R, return flat
- Reasoning must briefly justify the chosen leverage AND stops (e.g. "5x because wide ATR; SL at recent swing low translation; TP at structural resistance").

ROTATION (encouraged):
- Up to 10 longs concurrent. If at capacity AND a clearly stronger setup appears in candidates, you may CLOSE the weakest current position to free a slot.
- "Weakest" = stagnant P&L + deteriorating flow (OI down, funding turning extreme, top-trader-long collapsing) or thesis broken (lost EMA50).
- Don't close winners just to chase: rotation is justified by relative setup quality, not by recent price movement.
- For each existing position, briefly state in reasoning whether thesis is intact ('hold') or weakening ('close').

PORTFOLIO CONSTRUCTION:
- Mix: 2-3 large-cap anchors (risk_tier=large_cap) for stability + the rest mid-cap (risk_tier=mid_cap) for upside.
- Max 10 concurrent longs total. Quality > quantity — flat is always a valid answer.

OPERATOR NOTES (manual context — TREAT AS HIGH-PRIORITY):
- The user surfaces relevant context (rumors, regulatory news, scheduled macro events, asset-specific catalysts) under a section labeled OPERATOR NOTES in the prompt.
- These are HIGH-priority signal: they reflect information not visible in price/flow data and may explain or override technical signals.
- Note format: [timestamp, symbol or "global"] note text.
- When a note targets a specific symbol, prioritize it for that symbol's decision. When global, factor it into the overall macro stance.
- Operator notes do not override hard rules (max positions, SL/TP ranges) — but they should shift conviction and choice of which symbols to long.

CRITICAL CONSTRAINTS:
- Output decisions only via the `submit_decisions` tool.
- For each candidate, return exactly one decision (long or flat).
- For each existing position, return exactly one decision (close or flat=hold).
- For action=long, ALWAYS include leverage (5 or 10), stop_loss_pct AND take_profit_pct.
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
                        "stop_loss_pct": {
                            "type": "number", "minimum": -0.50, "maximum": -0.05,
                            "description": "REQUIRED when action=long. Negative fraction on collateral, e.g. -0.20 = close at -20% of margin. Calibrate to setup volatility."
                        },
                        "take_profit_pct": {
                            "type": "number", "minimum": 0.05, "maximum": 0.50,
                            "description": "REQUIRED when action=long. Positive fraction on collateral, e.g. 0.30 = close at +30% of margin. Aim for TP/|SL| ≥ 1.5."
                        },
                        "leverage": {
                            "type": "integer", "enum": [5, 10],
                            "description": "REQUIRED when action=long. 5 for high-vol mid-cap or wider technical stop (price needs to move ~2x further to hit SL); 10 for high-conviction tight setups or low-vol large-cap."
                        },
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
    operator_notes: list[dict] | None = None,
) -> str:
    parts: list[str] = []
    parts.append("=== MACRO ===")
    parts.append(
        f"BTC last={btc_features.last_price:.2f}, ret_24h={btc_features.ret_24h:+.2%}, "
        f"ret_7d={btc_features.ret_7d:+.2%}, RSI_1h={btc_features.rsi_14:.1f}, RSI_4h={btc_features.rsi_4h:.1f}, "
        f"above_EMA50_1h={btc_features.above_ema50}, above_EMA50_4h={btc_features.above_ema50_4h}, "
        f"above_EMA50_1d={btc_features.above_ema50_1d}, ATR%24h={btc_features.atr_pct_24h:.2%}"
    )
    parts.append(f"Fear & Greed: {fear_greed['value']} ({fear_greed['classification']})")

    if operator_notes:
        parts.append("\n=== OPERATOR NOTES (manually-curated, HIGH-PRIORITY) ===")
        for n in operator_notes:
            target = n.get("symbol") or "global"
            parts.append(f"[{n['ts'][:16]}, {target}] {n['note']}")

    parts.append("\n=== CANDIDATES (decide long or flat for each) ===")
    parts.append(
        "Format: SYMBOL [tier] | 1h technicals | 4h/daily trend | volatility/range | futures-flow"
    )
    for f in candidates:
        parts.append(
            f"{f.symbol} [{f.risk_tier}] | "
            f"price={f.last_price:.4f} ret_1h={f.ret_1h:+.2%} 24h={f.ret_24h:+.2%} 7d={f.ret_7d:+.2%} "
            f"RSI_1h={f.rsi_14:.1f} above_EMA50_1h={f.above_ema50} vol24h_usd={f.volume_24h_usd:,.0f} "
            f"| ret_4h={f.ret_4h:+.2%} ret_1d={f.ret_1d:+.2%} "
            f"RSI_4h={f.rsi_4h:.1f} above_EMA50_4h={f.above_ema50_4h} above_EMA50_1d={f.above_ema50_1d} "
            f"| ATR%24h={f.atr_pct_24h:.2%} dist_from_30d_high={f.dist_from_high_30d:+.2%} "
            f"dist_from_30d_low={f.dist_from_low_30d:+.2%} "
            f"| funding_8h={f.funding_rate_8h:+.4%} OI_24h={f.open_interest_change_24h:+.2%} "
            f"top_trader_long={f.top_trader_long_pct:.0%}"
        )

    if open_positions:
        parts.append("\n=== EXISTING POSITIONS (decide close or flat=hold for each) ===")
        for p in open_positions:
            sl = p.get("sl_pct", 0)
            tp = p.get("tp_pct", 0)
            lev = p.get("leverage", 0)
            parts.append(
                f"{p['symbol']}: side={p['side']} qty={p['qty']} entry={p['entry_price']:.4f} "
                f"mark={p['mark_price']:.4f} unrealized_pnl_pct={p['unrealized_pnl_pct']:+.2%} "
                f"leverage={lev}x martingale_levels_used={p['martingale_levels']} "
                f"target_SL={sl:+.0%} target_TP={tp:+.0%}"
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
    operator_notes: list[dict] | None = None,
) -> Decision:
    if not CFG.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY missing — set it in .env")

    client = anthropic.Anthropic(api_key=CFG.ANTHROPIC_API_KEY)
    user_prompt = build_user_prompt(
        candidates, open_positions, fear_greed, btc_features, news,
        operator_notes=operator_notes,
    )

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
