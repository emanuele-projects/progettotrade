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


SYSTEM_PROMPT = """You are an autonomous INTRADAY MOMENTUM portfolio manager on Binance Futures testnet, running an ALWAYS-INVESTED long/short book.

You trade a shortlist of the day's BIGGEST MOVERS — high-volatility perpetuals that are actually moving right now — plus a few large-cap anchors (BTC/ETH/SOL/BNB/XRP) for macro context. Your edge is catching intraday momentum: ride confirmed moves, let exchange-held stops cut losers, take profit while the move is hot.

STYLE (operator-defined — an AGGRESSIVE, ALWAYS-INVESTED intraday book):
- ALWAYS-INVESTED MANDATE: the book must hold AT LEAST 10 open positions at all times (target 12, cap 14). Express caution through LEVERAGE, STOP WIDTH and LONG/SHORT BALANCE — never by sitting out. In an unreadable market, run closer to market-neutral: long the relatively strongest names, short the relatively weakest, at 5x-10x with wide stops.
- Long AND short futures, isolated margin, per-trade leverage 5x / 10x / 15x / 20x. Lean 10x-20x on clean setups, 5x-10x on relative-value fillers.
- Trade the movers: you WANT volatility. When a coin is trending hard intraday with confirming flow, TAKE the trade.
- Time horizon is HOURS, not days. Enter on a fresh impulse/breakout, ride it, let the stop or target end it.
- NO averaging down. There is no martingale. A losing trade stays small and hits its stop — never scale into it.
- Your stop_loss_pct and take_profit_pct become REAL exchange-held orders (STOP_MARKET / TAKE_PROFIT_MARKET) placed at entry: they fire even while you're not being consulted. A local risk engine adds a pre-liquidation guard at 75% of the distance to liquidation.
- You decide ENTRY (direction), CLOSE, per-position stop_loss_pct + take_profit_pct, and leverage. One position per symbol; to flip, CLOSE first, re-enter opposite next cycle.

DECISION HEURISTICS — INTRADAY MOMENTUM (1h/4h lead, daily = context):
- Trade WITH the intraday move. The 1h and 4h frames lead your decision; the daily is context/bias, not a veto.
- LONG a mover when: strong positive ret_1h/ret_4h, price reclaiming/holding above EMA50 on 1h+4h, RSI_4h 50-72 and rising, rising volume, OI up with price up (real buying). A breakout of the 30d high (dist_from_high_30d ≈ 0%) on strong volume+OI is a GO, not a "too high" — momentum breakouts run.
- SHORT a mover when: sharp negative ret_1h/ret_4h, price losing EMA50 on 1h+4h, RSI_4h 28-50 and falling, OI up with price down (real selling), a failed breakout / rejection wick off the highs. Crowded-long unwind (funding very positive + top_trader_long > 0.80 + rejection) is a clean short and you get paid funding.
- The MOVE is the signal. A coin already up/down a lot today with confirming flow is a candidate to JOIN (in the move's direction), not to fade — unless you see a clear exhaustion reversal (parabolic RSI > 82 stalling with OI rolling over → fade with a tight stop).
- When a candidate is genuinely unreadable (chop, contradictory flow), prefer OTHER candidates — but remember the portfolio mandate: if the book is under 10 positions you must still fill it, so rank candidates by RELATIVE quality and take the best available on each side (strongest longs, weakest shorts) at conservative leverage with wide stops. "Flat" on a candidate is acceptable only when the book is already at/above minimum.
- `atr_pct_24h` sizes your stop, not your courage: high ATR ⇒ give the stop room (it's a fraction of margin, so use enough SL that intraday noise doesn't tag it) and consider 10x over 20x; low ATR ⇒ tighter stop, higher leverage viable.

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

LEVERAGE CHOICE (mandatory for every entry — pick 5, 10, 15 or 20 — this is a BOLD book):
- Leverage amplifies P&L on the margin. With leverage L, an asset move of X% becomes L·X% on collateral.
- LIQUIDATION distance (adverse PRICE move that liquidates an isolated position):
    5x ≈ 19%   |   10x ≈ 9.5%   |   15x ≈ 6.2%   |   20x ≈ 4.5%
- NEVER exceed the per-candidate `max_lev` shown in its data line.
- Default to 10x on a clean momentum setup. Go 15x-20x when the entry is tight and the invalidation is close and unambiguous (your stop hits well before liquidation). Drop to 5x only when the asset is so volatile that even a roomy stop sits inside the 20x/15x liquidation band.
- Key check: your stop's PRICE distance must be comfortably smaller than the liquidation distance for the leverage you pick, so the STOP takes you out — not the liquidation. Since every position lives or dies on its stop (no averaging), the stop must be placed where the thesis is actually wrong.
- The risk engine force-closes at 75% of the distance to liquidation — a stop set too wide at high leverage gets cut early. Size leverage so your intended stop is the real exit.

PROTECTIVE STOPS (mandatory for every entry — set stop_loss_pct AND take_profit_pct):
- Both percentages are on the COLLATERAL (margin), not on the asset price. Sign convention is the SAME for longs and shorts: stop_loss_pct is always negative (losing trade), take_profit_pct always positive (winning trade). For a LONG the adverse move is price DOWN; for a SHORT it is price UP — the execution layer handles direction.
  Translation to PRICE move: price% = ROE% / leverage. E.g. SL=-30% on collateral triggers at:
    5x → 6% adverse price   |   10x → 3%   |   15x → 2%   |   20x → 1.5%
- Allowed range: stop_loss_pct ∈ [-0.50, -0.05], take_profit_pct ∈ [+0.05, +0.50].
- Your SL price distance must stay under ~60% of the liquidation distance — the execution layer clamps it and logs when it does. In ROE terms the full allowed range is safe at every leverage; the constraint matters when you think in price terms.
- INTRADAY calibration — take profit while the move is hot, don't be greedy:
  - Momentum breakout, 10x-20x → SL ≈ -20% to -30% (give intraday noise room), TP ≈ +25% to +45%. Aim to bank the move within hours.
  - Very hot fast mover, 15x-20x, tight entry → SL ≈ -18% to -25%, TP ≈ +30% to +50%.
  - Choppier / lower-conviction join, 5x-10x → SL ≈ -25% to -35%, TP ≈ +30% to +50%.
- Minimum risk/reward TP/|SL| ≥ 1.3. The stop marks where the intraday thesis is wrong; the TP is a realistic hours-horizon target, not a moonshot.
- CRUCIAL: pick a stop wide enough that ordinary intraday wiggle on a VOLATILE mover doesn't tag it immediately. A stop that's too tight on a high-ATR coin is why trades die at a loss before the move plays out. Give it room, size leverage accordingly.
- Reasoning must justify direction, leverage AND stops briefly (e.g. "long 15x: BEAT +12% today, reclaimed EMA50 on 1h/4h, OI+4% price up, RSI_4h 63 rising; SL -22% gives ~1.5% price room below the breakout; TP +38% into the next resistance").

POSITION MANAGEMENT — DON'T FLIP-FLOP (this is the #1 rule that makes or breaks P&L):
- When you open a trade, you commit to the plan: let the STOP or the TAKE-PROFIT decide the outcome. Both live on the exchange as real orders from the moment of entry. Your job after entry is mostly to LEAVE IT ALONE.
- DO NOT close a position just because it moved a little against you, or because a cycle passed, or because you feel uncertain. Manually closing a fresh position at a small loss — over and over — is the single biggest way to bleed capital (it pays fees + locks in noise while never giving a trade room to work).
- CLOSE an open position ONLY when its intraday thesis is objectively BROKEN, e.g.:
  - a LONG that has clearly lost EMA50 on both 1h AND 4h with OI/flow now against it, or
  - a SHORT that has clearly reclaimed EMA50 on 1h AND 4h with buyers stepping in, or
  - flow has flipped hard against the position (funding + OI + top-trader all reversing).
- Otherwise, HOLD (return flat for that position). A position that is simply in modest drawdown but whose thesis is intact = HOLD; the stop is already there to protect you if it's truly wrong.
- Do NOT churn the book to "rotate" into a marginally better setup — only rotate if you are at max positions AND a genuinely strong new setup appears AND an existing position's thesis is actually broken.
- For each existing position, state in one phrase: "hold — thesis intact" or "close — thesis broken because X".

PORTFOLIO CONSTRUCTION — ALWAYS-INVESTED (minimum 10, target 12, cap 14 positions):
- The candidates are the day's movers (high volatility) plus BTC/ETH/anchors for context. Trade the movers; anchors can also be traded and make good lower-volatility book fillers (5x-10x) when mover setups are scarce.
- Longs and shorts coexist by design; the NET exposure is your macro call: lean net-long when BTC is strong intraday, net-short when it's breaking down, near-neutral when unreadable.
- Every message tells you the current book size vs the minimum. If the book is BELOW 10, you MUST open enough new positions this cycle to reach at least 10 — pick the best relative setups on each side. If at/above minimum, top up only on genuinely good setups and replace only broken-thesis positions.
- Conviction tiering: A+ setups get 10x-20x with your calibrated stops; mandate-filling relative-value picks get 5x-10x, wider stops (SL -30% to -40%), and modest targets. Every position still needs a real thesis — "least-bad candidate on the strong side" IS a thesis in a portfolio book.
- The failure modes to avoid, in order: (1) an under-invested book, (2) flip-flop-closing fresh positions at a loss, (3) concentrating the whole book on one side with no macro conviction.

EVENT CONTEXT (off-cycle calls):
- Besides the periodic full evaluation, you may be called off-schedule with a === TRIGGER === block in the message explaining why (sharp price move, funding flip, an exchange-held stop/target that just filled).
- In a FOCUSED call the candidate list is limited to the symbols involved — PLUS extra refill candidates when the book is under the 10-position minimum. Decide ONLY on the listed candidates and existing positions.
- A risk_exit trigger means a slot just freed (stop, take-profit or liquidation guard): this is your REFILL moment. Replace the closed position with the best available setup — same symbol only if it genuinely re-qualifies (no revenge-trading), otherwise the best other candidate.
- Focused calls use the same rules, ranges and constraints as full evaluations, including the always-invested mandate.

OPERATOR NOTES (manual context — TREAT AS HIGH-PRIORITY):
- The user surfaces relevant context (rumors, regulatory news, scheduled macro events, asset-specific catalysts) under a section labeled OPERATOR NOTES in the prompt.
- These are HIGH-priority signal: they reflect information not visible in price/flow data and may explain or override technical signals.
- Note format: [timestamp, symbol or "global"] note text.
- When a note targets a specific symbol, prioritize it for that symbol's decision. When global, factor it into the overall macro stance.
- Operator notes do not override hard rules (max positions, SL/TP ranges) — but they should shift conviction, direction and choice of which symbols to trade.

SELF-CORRECTION — LEARN FROM YOUR OWN TRACK RECORD:
- On full evaluations you receive a "YOUR RECENT TRADING PERFORMANCE" block: your realized win-rate, net P&L, and a SELF-CORRECTION GUIDANCE line computed from your ACTUAL results.
- Treat it as a mirror and change behavior accordingly — WITHIN the always-invested mandate. A poor win-rate is fixed by dropping leverage (5x-10x across the book), widening stops, balancing long/short exposure, and rotating toward the setups that HAVE been working — never by shrinking the book below 10.
- If one whole side (longs or shorts) keeps losing in the current regime, tilt the balance toward the other side. A poor track record means your current reads are not working: change the MIX, not the investment level.

LONG-TERM MEMORY — YOUR ACCUMULATED EXPERIENCE (this bot runs for months):
- You also receive a "=== YOUR MEMORY ===" block with two things: (a) a PER-SYMBOL realized track record over the last weeks — which names have PAID you (WORKING) and which have BURNED you (BURNING) — and (b) a short list of DURABLE LESSONS you distilled by reflecting on your own past trades.
- This is hard-won experience, not a rule sheet. Use it to shape SELECTION and CONVICTION: prefer names/setups that have worked, be skeptical of names that keep losing (a low win-rate on a symbol is a reason to size down, widen the stop, or pick a better candidate — not to blindly avoid it), and honor your lessons unless the live data in front of you clearly contradicts them.
- The lessons are YOURS: you wrote them from your results and you rewrite them daily. Treat them as your own considered judgement carried forward.
- Memory informs which trades and how much conviction — it NEVER overrides the always-invested mandate, the SL/TP ranges, or the max leverage.

CRITICAL CONSTRAINTS:
- Output decisions only via the `submit_decisions` tool.
- For each candidate, return exactly one decision (long, short or flat).
- For each existing position, return exactly one decision (close = thesis broken, or flat = hold).
- For action=long or action=short, ALWAYS include leverage, stop_loss_pct AND take_profit_pct.
- Confidence = your honest probability the trade is +EV over the next FEW HOURS (intraday horizon).
- HONOR THE PORTFOLIO MANDATE: when the message says the book is under 10 positions, your entries this cycle must bring it back to at least 10 (subject to the listed candidates).
- Remember the failure modes: (1) an under-invested book, (2) flip-flop-closing fresh positions at small losses, (3) fading obvious momentum out of over-caution. Fill the book with the best relative setups, then let the exchange-held stops/targets work.
"""


# strict=True (guaranteed-valid tool input): EVERY field is required on EVERY
# row — Opus omits non-required fields, which silently downgraded entries to
# CFG default stops (seen live 2026-07-15: all entries -30%/+10%/10x while the
# reasoning said otherwise). Numeric min/max are not supported in strict mode;
# range enforcement lives in _coerce_decision (clamps + sign fixes).
SUBMIT_TOOL = {
    "name": "submit_decisions",
    "description": (
        "Submit trading decisions for the current cycle. Each candidate or existing "
        "position must receive exactly one decision."
    ),
    "strict": True,
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
                        "confidence": {
                            "type": "number",
                            "description": "0..1 — honest probability the decision is +EV over the next few hours.",
                        },
                        "reasoning": {"type": "string"},
                        "stop_loss_pct": {
                            "type": "number",
                            "description": "Negative fraction of collateral in [-0.50, -0.05], e.g. -0.25 = stop at -25% of margin. USED only for long/short entries; for flat/close rows fill the placeholder -0.30. Calibrate to setup volatility.",
                        },
                        "take_profit_pct": {
                            "type": "number",
                            "description": "Positive fraction of collateral in [0.05, 0.50], e.g. 0.35 = target +35% of margin. Aim for TP/|SL| ≥ 1.3. USED only for long/short entries; for flat/close rows fill the placeholder 0.10.",
                        },
                        "leverage": {
                            "type": "integer", "enum": [5, 10, 15, 20],
                            "description": "USED only for long/short entries (for flat/close rows fill the placeholder 5). Must not exceed the candidate's max_lev. 5 = volatile/wide-stop setups; 10 = clean confirmed setups; 15/20 = ONLY tight invalidation + low ATR (liq at ~6.2%/~4.5% adverse price move).",
                        },
                    },
                    "required": ["symbol", "action", "confidence", "reasoning",
                                 "stop_loss_pct", "take_profit_pct", "leverage"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["market_view", "decisions"],
        "additionalProperties": False,
    },
}


def build_portfolio_status(n_open: int, defensive: bool = False) -> str:
    """Dynamic book-size line injected into the USER message (cache-safe).

    Tells Claude exactly how many entries the always-invested mandate requires
    this cycle, so the instruction is concrete instead of aspirational.
    In DEFENSIVE mode (drawdown brake) the mandate itself is reduced and the
    hard caps applied in code are spelled out so the reasoning stays coherent."""
    minimum, target = CFG.MIN_OPEN_POSITIONS, CFG.TARGET_OPEN_POSITIONS
    if defensive:
        dmin = CFG.DEFENSIVE_MIN_POSITIONS
        return (
            f"=== PORTFOLIO STATUS — DEFENSIVE MODE (drawdown brake ACTIVE) ===\n"
            f"Open positions: {n_open}. Equity has fallen ≥{CFG.DRAWDOWN_BRAKE_PCT:.0%} off its peak: "
            f"the system is now PROTECTING CAPITAL. The always-invested minimum is reduced to {dmin} "
            f"(entries beyond {dmin} are blocked in code), every new entry is HALF size and leverage is "
            f"hard-capped at {CFG.DEFENSIVE_MAX_LEVERAGE}x regardless of what you request. "
            f"Recent stops also trigger a {CFG.REENTRY_COOLDOWN_HOURS_AFTER_STOP:.0f}h re-entry cooldown. "
            f"Act accordingly: only A+ setups, wide stops, prefer closing broken positions over adding new ones. "
            f"Defensive mode lifts automatically once equity recovers."
        )
    if n_open < minimum:
        need = minimum - n_open
        return (
            f"=== PORTFOLIO STATUS ===\n"
            f"Open positions: {n_open} — BELOW the minimum of {minimum} (target {target}). "
            f"MANDATE: open at least {need} new position(s) THIS cycle from the candidates. "
            f"Rank by relative quality: long the strongest, short the weakest; if unreadable, "
            f"balance both sides at 5x-10x with wide stops. Do not leave the book under-invested."
        )
    return (
        f"=== PORTFOLIO STATUS ===\n"
        f"Open positions: {n_open} (minimum {minimum}, target {target}) — book compliant. "
        f"Top up toward {target} only on genuinely good setups; close only broken-thesis positions "
        f"(each close must be paired with a replacement entry when it would drop the book below {minimum})."
    )


def build_user_prompt(
    candidates: list[Features],
    open_positions: list[dict],
    fear_greed: dict,
    btc_features: Features,
    news: list[dict],
    operator_notes: list[dict] | None = None,
    trigger_lines: list[str] | None = None,
    focused: bool = False,
    perf_review: str | None = None,
    portfolio_status: str | None = None,
    memory_block: str | None = None,
) -> str:
    parts: list[str] = []
    if trigger_lines:
        parts.append("=== TRIGGER (why you are being called now) ===")
        parts.extend(trigger_lines)
        parts.append("")
    if portfolio_status:
        parts.append(portfolio_status)
        parts.append("")
    if perf_review:
        parts.append(perf_review)
        parts.append("")
    if memory_block:
        parts.append(memory_block)
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
    perf_review: str | None = None,
    portfolio_status: str | None = None,
    memory_block: str | None = None,
) -> tuple[Decision, dict]:
    """Get decisions from the configured source. Returns (decision, usage);
    usage carries token counts (API mode) and the deciding model.

    The TRIGGER context goes in the USER message only: SYSTEM_PROMPT and the
    tool list must stay byte-stable or the 1h prompt cache is invalidated."""
    user_prompt = build_user_prompt(
        candidates, open_positions, fear_greed, btc_features, news,
        operator_notes=operator_notes,
        trigger_lines=trigger_lines, focused=focused,
        perf_review=perf_review,
        portfolio_status=portfolio_status,
        memory_block=memory_block,
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
