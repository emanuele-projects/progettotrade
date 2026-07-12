"""Strategy: build prompt, get decisions from Claude, validate them.

Two decision sources (CFG.DECISION_SOURCE):
- "api":  direct Anthropic API call with forced tool use + prompt caching
- "file": file-based exchange with an external decider (a Claude Code session
          running /loop reads data/decision_request.json and writes
          data/decision_response.json) — no API credits needed
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from typing import Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from config import CFG
from data import Features
import journal


_ALLOWED_LEVERAGES = (5, 10, 15, 20)


def _coerce_decision(raw: dict) -> dict | None:
    """Repair small model deviations so one bad field doesn't sink the batch.

    Returns a cleaned decision dict, or None if unrecoverable (no symbol/action).
    Fixes seen in the wild: take_profit given negative (sign flip), stop_loss
    given positive, values just outside range, non-standard leverage."""
    symbol = raw.get("symbol")
    action = raw.get("action")
    if not symbol or action not in ("long", "short", "flat", "close"):
        return None

    out = {
        "symbol": symbol,
        "action": action,
        "confidence": min(max(float(raw.get("confidence", 0.5)), 0.0), 1.0),
        "reasoning": str(raw.get("reasoning", ""))[:2000],
    }
    if action in ("long", "short"):
        sl = raw.get("stop_loss_pct")
        tp = raw.get("take_profit_pct")
        lev = raw.get("leverage")
        # SL must be negative, TP positive — correct obvious sign flips, then clamp.
        if sl is not None:
            out["stop_loss_pct"] = min(max(-abs(float(sl)), -0.50), -0.05)
        if tp is not None:
            out["take_profit_pct"] = min(max(abs(float(tp)), 0.05), 0.50)
        if lev is not None:
            out["leverage"] = min(_ALLOWED_LEVERAGES, key=lambda a: abs(a - int(lev)))
    return out


def parse_decisions(raw_input: dict) -> Decision:
    """Coerce + validate a raw tool/file payload into a Decision.

    Per-decision tolerance: recoverable rows are repaired, unrecoverable ones
    are dropped with a logged warning — a single malformed row never aborts the
    cycle (which would waste the paid Claude call and discard the good rows)."""
    market_view = raw_input.get("market_view", "")
    cleaned: list[dict] = []
    dropped = 0
    for raw in raw_input.get("decisions", []):
        coerced = _coerce_decision(raw) if isinstance(raw, dict) else None
        if coerced is None:
            dropped += 1
            continue
        try:
            AssetDecision.model_validate(coerced)  # per-row guard
            cleaned.append(coerced)
        except ValidationError:
            dropped += 1
    if dropped:
        journal.log_event("WARN", f"{dropped} malformed decision(s) dropped/repaired this cycle")
    return Decision.model_validate({"market_view": market_view, "decisions": cleaned})


class AssetDecision(BaseModel):
    symbol: str
    action: Literal["long", "short", "flat", "close"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    # All three required when action is an entry (long/short); ignored otherwise.
    # Stops are fraction of COLLATERAL (margin), NOT of asset price.
    # Sign convention is direction-agnostic: SL always negative, TP always positive.
    stop_loss_pct: float | None = Field(default=None, ge=-0.50, le=-0.05)
    take_profit_pct: float | None = Field(default=None, ge=0.05, le=0.50)
    leverage: Literal[5, 10, 15, 20] | None = Field(default=None)


class Decision(BaseModel):
    market_view: str
    decisions: list[AssetDecision]


SYSTEM_PROMPT = """You are an autonomous crypto trading analyst running on Binance Futures testnet.

You evaluate a curated shortlist of perpetual contracts — a mix of large-cap anchors (BTC/ETH/SOL/BNB/XRP) and dynamically-selected mid-cap altcoins — and decide which to LONG, which to SHORT, leave FLAT, or CLOSE.

STRATEGY (operator-defined, fixed):
- Long AND short futures, isolated margin, per-trade leverage 5x / 10x / 15x / 20x
- 50% of capital deployed initially across up to 10 positions ($10k total → $500 margin per entry)
- 50% reserved for averaging-down (martingale) on losers — handled by a real-time risk engine, not by you; only active on positions at ≤10x leverage
- Protective stops are enforced tick-by-tick by the risk engine, plus a pre-liquidation guard that force-closes at 75% of the distance to liquidation
- You decide ENTRY signals (direction included), CLOSE signals, per-position protective stops, AND per-position leverage
- One position per symbol: to flip direction, CLOSE first — you may open the opposite direction in a later cycle once flat.

DECISION HEURISTICS — TECHNICALS (1h, 4h, daily):
- Trust trends that align across timeframes: above_EMA50 on 1h AND 4h AND daily = strongest bullish; below EMA50 on all three = strongest bearish. Single-timeframe agreement is weaker signal.
- Prefer LONG: positive momentum (price > EMA50 on multiple TFs), RSI 50-70 on the 4h, strong recent volume, supportive macro (BTC above EMA50, F&G > 40), positive/neutral news.
- Prefer SHORT: confirmed breakdown (price < EMA50 on 4h AND daily), RSI 30-50 and falling on the 4h, lower highs into the 30d range, negative macro (BTC below EMA50, F&G < 30), negative news/catalysts.
- Avoid entries: parabolic RSI > 80 on 1h or 4h for longs (overheated), capitulative RSI < 20 for shorts (bounce risk), heavy contradicting news.
- NEVER short a strong multi-timeframe uptrend purely because RSI is "overbought" — overbought can stay overbought. Shorts need broken structure, not just stretched momentum.
- Use `dist_from_high_30d` and `dist_from_low_30d` to gauge mean-reversion risk: price near 30d high (dist≈0%) on a frothy run = risky long / potential short on rejection; price near 30d low = risky short (bounce zone) / potential contrarian long with confirmed flow.
- `atr_pct_24h` is the asset's recent volatility. Higher ATR ⇒ wider stops needed (use leverage=5 + larger SL). Lower ATR ⇒ tighter stops viable (leverage=10 + smaller SL).

DECISION HEURISTICS — FUTURES FLOW (real-time):
- funding_8h: signed 8h funding rate paid by longs to shorts.
  - Mildly positive (0% to +0.03%) = healthy bullish leverage, OK to long.
  - Very high (>+0.05%) = market overcrowded long → reduce long conviction; with rejection at resistance and deteriorating flow this is a SHORT setup (crowded-long unwind), and shorts get PAID funding while holding.
  - Negative (<−0.02%) = bear sentiment; contrarian long if technicals confirm, but sustained negative funding with broken structure confirms shorts (mind the fee you pay to hold them).
- OI_24h: open-interest change vs 24h ago (fraction).
  - OI up + price up = real momentum (longs adding) → high long conviction.
  - OI up + price down = bears in control (shorts adding) → valid short confirmation, avoid longs.
  - OI down + price up = short covering, fragile rally → low conviction either way.
  - OI down + price down = longs capitulating, trend may be exhausting → late to short.
- top_trader_long: share of top traders net-long (0..1).
  - 0.55–0.70 with bullish technicals = healthy long confirmation.
  - >0.80 = excessive optimism; with rejection/broken structure this strengthens the short case.
  - <0.40 with bullish technicals = contrarian long opportunity; <0.40 with bearish technicals = smart money already short, confirmation.

LEVERAGE CHOICE (mandatory for every entry — pick 5, 10, 15 or 20):
- Leverage amplifies P&L on the margin. With leverage L, an asset move of X% becomes L·X% on collateral.
- LIQUIDATION comes first at high leverage. Approximate adverse PRICE move that liquidates an isolated position:
    5x ≈ 19%   |   10x ≈ 9.5%   |   15x ≈ 6.2%   |   20x ≈ 4.5%
- NEVER exceed the per-candidate `max_lev` shown in its data line.
- 5x: volatile asset / wide ATR / stop needs room to breathe. Default when in doubt.
- 10x: high-conviction setup with a TIGHT technical invalidation level. Good for clean trends, dangerous in chop.
- 15x / 20x: ONLY when ALL of these hold — (a) invalidation level is very tight and unambiguous, (b) low volatility: liquidation distance must exceed ~6× the asset's atr_pct_24h (e.g. 20x needs ATR ≤ ~0.7%), (c) you accept there is NO averaging: martingale is disabled above 10x, the position lives or dies on its initial stop.
- The risk engine force-closes any position at 75% of its distance to liquidation — a stop set too wide at high leverage will be cut earlier than you asked.

PROTECTIVE STOPS (mandatory for every entry — set stop_loss_pct AND take_profit_pct):
- Both percentages are on the COLLATERAL (margin), not on the asset price. Sign convention is the SAME for longs and shorts: stop_loss_pct is always negative (losing trade), take_profit_pct always positive (winning trade). For a LONG the adverse move is price DOWN; for a SHORT it is price UP — the execution layer handles direction.
  Translation to PRICE move: price% = ROE% / leverage. E.g. SL=-30% on collateral triggers at:
    5x → 6% adverse price   |   10x → 3%   |   15x → 2%   |   20x → 1.5%
- Allowed range: stop_loss_pct ∈ [-0.50, -0.05], take_profit_pct ∈ [+0.05, +0.50].
- Your SL price distance must stay under ~60% of the liquidation distance — the execution layer clamps it and logs when it does. In ROE terms the full allowed range is safe at every leverage; the constraint matters when you think in price terms.
- Minimum risk/reward: aim for TP/|SL| ≥ 1.5 (e.g. SL=-20% paired with TP=+30%+).
- Calibrate stops to the SETUP and chosen leverage:
  - Low-vol large-cap, leverage 10x, confirmed trend → SL≈-15% to -20%, TP≈+20% to +30%
  - Mid-cap with strong momentum, leverage 5x → SL≈-25% to -35%, TP≈+40% to +60% (price has room to breathe)
  - High-conviction tight technical, 15x/20x → SL≈-15% to -25% (≈1-1.7% price), TP≈+30% to +50%; the stop IS the thesis — if price touches it the setup was wrong
  - Marginal setup → wider SL won't save you; if you can't justify good R/R, return flat
- Reasoning must briefly justify the chosen direction, leverage AND stops (e.g. "short 15x: rejection at 30d high with funding +0.08% and top traders 85% long; tight invalidation above the wick; SL -20% ≈ 1.3% price").

ROTATION (encouraged):
- Up to 10 positions concurrent (longs + shorts combined). If at capacity AND a clearly stronger setup appears in candidates, you may CLOSE the weakest current position to free a slot.
- "Weakest" = stagnant P&L + deteriorating flow (OI down, funding turning extreme, top-trader positioning collapsing) or thesis broken (lost/reclaimed EMA50 against your direction).
- Don't close winners just to chase: rotation is justified by relative setup quality, not by recent price movement.
- For each existing position, briefly state in reasoning whether thesis is intact ('hold') or weakening ('close').

PORTFOLIO CONSTRUCTION:
- Mix: 2-3 large-cap anchors (risk_tier=large_cap) for stability + the rest mid-cap (risk_tier=mid_cap) for upside.
- Longs and shorts can coexist; a net-short book is legitimate in a confirmed downtrend (BTC below EMA50 on 4h/1d, F&G < 30).
- Max 10 concurrent positions total. Quality > quantity — flat is always a valid answer.

EVENT CONTEXT (off-cycle calls):
- Besides the periodic full evaluation, you may be called off-schedule with a === TRIGGER === block in the message explaining why (sharp price move, funding flip, a position force-closed by the risk engine).
- In a FOCUSED call the candidate list is limited to the symbols involved: decide ONLY on the listed candidates and on the existing positions. Everything else is out of scope for that call.
- A risk_exit trigger means a position was just force-closed (stop, take-profit or pre-liquidation guard): reassess the book, and only re-enter the same symbol if the setup genuinely re-qualifies — do not revenge-trade.
- Focused calls use the same rules, ranges and constraints as full evaluations.

OPERATOR NOTES (manual context — TREAT AS HIGH-PRIORITY):
- The user surfaces relevant context (rumors, regulatory news, scheduled macro events, asset-specific catalysts) under a section labeled OPERATOR NOTES in the prompt.
- These are HIGH-priority signal: they reflect information not visible in price/flow data and may explain or override technical signals.
- Note format: [timestamp, symbol or "global"] note text.
- When a note targets a specific symbol, prioritize it for that symbol's decision. When global, factor it into the overall macro stance.
- Operator notes do not override hard rules (max positions, SL/TP ranges) — but they should shift conviction, direction and choice of which symbols to trade.

CRITICAL CONSTRAINTS:
- Output decisions only via the `submit_decisions` tool.
- For each candidate, return exactly one decision (long, short or flat).
- For each existing position, return exactly one decision (close or flat=hold).
- For action=long or action=short, ALWAYS include leverage, stop_loss_pct AND take_profit_pct.
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
                        "action": {"type": "string", "enum": ["long", "short", "flat", "close"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reasoning": {"type": "string"},
                        "stop_loss_pct": {
                            "type": "number", "minimum": -0.50, "maximum": -0.05,
                            "description": "REQUIRED when action=long or short. Negative fraction on collateral for both directions, e.g. -0.20 = close at -20% of margin. Calibrate to setup volatility."
                        },
                        "take_profit_pct": {
                            "type": "number", "minimum": 0.05, "maximum": 0.50,
                            "description": "REQUIRED when action=long or short. Positive fraction on collateral for both directions, e.g. 0.30 = close at +30% of margin. Aim for TP/|SL| ≥ 1.5."
                        },
                        "leverage": {
                            "type": "integer", "enum": [5, 10, 15, 20],
                            "description": "REQUIRED when action=long or short. Must not exceed the max_lev shown for the candidate. 5 = volatile/wide-stop setups; 10 = clean confirmed setups; 15/20 = ONLY tight invalidation + low ATR (liq at ~6.2%/~4.5% adverse price move; no martingale above 10x)."
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
    trigger_lines: list[str] | None = None,
    focused: bool = False,
) -> str:
    parts: list[str] = []
    if trigger_lines:
        parts.append("=== TRIGGER (why you are being called now) ===")
        parts.extend(trigger_lines)
        parts.append("")
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

    if focused:
        parts.append("\n=== CANDIDATES (FOCUSED call: only the symbols involved in the trigger — decide long, short or flat for each) ===")
    else:
        parts.append("\n=== CANDIDATES (decide long, short or flat for each) ===")
    parts.append(
        "Format: SYMBOL [tier] | 1h technicals | 4h/daily trend | volatility/range | futures-flow"
    )
    for f in candidates:
        parts.append(
            f"{f.symbol} [{f.risk_tier}] max_lev={f.max_leverage}x | "
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


def _decide_via_file(user_prompt: str, focused: bool) -> tuple[Decision, dict]:
    """File-based decision exchange with an external Claude Code /loop session.

    Writes an atomic, self-contained request (instructions + data + expected
    schema) and polls for a response with the matching request_id. On timeout
    the cycle is skipped upstream — position protection is unaffected (the
    risk engine never depends on the decider)."""
    request_id = f"req-{int(time.time() * 1000)}"
    request = {
        "request_id": request_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "focused" if focused else "baseline",
        "how_to_respond": (
            f"Scrivi la risposta come JSON in {CFG.DECISION_RESPONSE_FILE} "
            "(scrittura atomica: file temporaneo poi rename) con le chiavi: "
            "request_id (copia esatta), market_view (stringa), decisions (array). "
            "Ogni decisione: symbol, action (long|short|flat|close), confidence (0-1), "
            "reasoning, e per long/short anche stop_loss_pct (-0.50..-0.05), "
            "take_profit_pct (0.05..0.50), leverage (5|10|15|20). "
            "Copri ogni candidato e ogni posizione aperta esattamente una volta. "
            "Nessun testo fuori dal JSON."
        ),
        "instructions": SYSTEM_PROMPT,
        "response_schema": SUBMIT_TOOL["input_schema"],
        "user_prompt": user_prompt,
    }
    tmp = CFG.DECISION_REQUEST_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(request, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, CFG.DECISION_REQUEST_FILE)

    deadline = time.monotonic() + CFG.FILE_DECISION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if CFG.DECISION_RESPONSE_FILE.exists():
            try:
                data = json.loads(CFG.DECISION_RESPONSE_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = None  # mid-write or garbage: keep polling
            if data and data.get("request_id") == request_id:
                decision = parse_decisions(data)  # tolerant coerce+validate
                usage = {"input_tokens": 0, "output_tokens": 0,
                         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                         "model": "claude-code-loop"}
                return decision, usage
        time.sleep(2)
    raise RuntimeError(
        f"nessuna risposta dal decisore file entro {CFG.FILE_DECISION_TIMEOUT_SECONDS}s "
        f"(request_id={request_id}) — la sessione Claude Code con /loop è attiva?"
    )


def decide(
    candidates: list[Features],
    open_positions: list[dict],
    fear_greed: dict,
    btc_features: Features,
    news: list[dict],
    operator_notes: list[dict] | None = None,
    trigger_lines: list[str] | None = None,
    focused: bool = False,
) -> tuple[Decision, dict]:
    """Get decisions from the configured source. Returns (decision, usage);
    usage carries token counts (API mode) and the deciding model.

    The TRIGGER context goes in the USER message only: SYSTEM_PROMPT and the
    tool list must stay byte-stable or the 1h prompt cache is invalidated."""
    user_prompt = build_user_prompt(
        candidates, open_positions, fear_greed, btc_features, news,
        operator_notes=operator_notes,
        trigger_lines=trigger_lines, focused=focused,
    )

    if CFG.DECISION_SOURCE == "file":
        return _decide_via_file(user_prompt, focused)

    if not CFG.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY missing — set it in .env")

    client = anthropic.Anthropic(api_key=CFG.ANTHROPIC_API_KEY)

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
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(
            f"risposta troncata a max_tokens={CFG.CLAUDE_MAX_TOKENS} — decisions incompleto; "
            f"alzare CLAUDE_MAX_TOKENS (output usati: {resp.usage.output_tokens})"
        )
    if tool_input is None:
        raise RuntimeError(f"Claude did not call submit_decisions tool. Response: {resp.content}")

    decision = parse_decisions(tool_input)  # tolerant coerce+validate

    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
    }
    return decision, usage
