"""Long-term memory for a bot that runs for months.

The Anthropic API is stateless: every decision call starts from zero. Over a
year that means the bot would keep repeating the same mistakes unless we hand it
back its own experience. This module builds that experience in three tiers, all
token-cheap to inject:

  1. PER-SYMBOL TRACK RECORD (free, computed): from Binance realized-P&L income
     history — which names have paid the bot and which have burned it. No model
     call; pure aggregation over the last MEMORY_LOOKBACK_DAYS.

  2. DISTILLED LESSONS (one reflection call/day): once a day `reflect()` shows
     Claude its own recent decisions + their realized outcomes + its previous
     lessons, and asks it to rewrite a short, durable, self-curating set of
     lessons ("stop shorting low-liquidity memecoins", "20x entries keep getting
     wicked out — cap momentum plays at 15x", ...). These are stored and read
     back on every future decision.

  3. INJECTION: `build_memory_block()` folds tiers 1+2 into one compact block
     that goes in the USER message (never the cached system prompt), so the 1h
     prompt cache is preserved and the memory stays bounded in size.

The reflection is the only paid part and runs ~once/day → negligible cost. The
substrate (income history + journal) already persists across restarts and even
across journal resets (income history lives on Binance), so the memory survives
a redeploy of the VM.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import anthropic

from config import CFG
import journal


# ---------------------------------------------------------------------------
# Tier 1 — per-symbol realized track record (free, from income history).
# ---------------------------------------------------------------------------
def symbol_records(client, lookback_days: int | None = None) -> dict[str, dict]:
    """Aggregate realized P&L per symbol over the lookback window.

    Returns {symbol: {"wins": int, "losses": int, "net": float, "n": int}}.
    Source is Binance's authoritative realized-P&L history (survives a journal
    reset). Empty dict on any failure — memory is always best-effort."""
    days = lookback_days if lookback_days is not None else CFG.MEMORY_LOOKBACK_DAYS
    try:
        rows = client.futures_income_history(incomeType="REALIZED_PNL", limit=1000)
    except Exception:
        return {}
    if not rows:
        return {}
    # Floor at the clean-run reset so pre-reset (double-instance-contaminated)
    # trades never enter the bot's long-term memory.
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    cutoff_ms = max(cutoff_ms, float(CFG.RESET_TS_MS))
    agg: dict[str, dict] = {}
    for r in rows:
        if int(r.get("time", 0)) < cutoff_ms:
            continue
        sym = r.get("symbol", "")
        if not sym:
            continue
        pnl = float(r["income"])
        a = agg.setdefault(sym, {"wins": 0, "losses": 0, "net": 0.0, "n": 0})
        a["net"] += pnl
        a["n"] += 1
        if pnl > 0:
            a["wins"] += 1
        elif pnl < 0:
            a["losses"] += 1
    return agg


def _format_symbol_records(agg: dict[str, dict], top_n: int,
                           only: set[str] | None = None) -> list[str]:
    """One compact line per symbol, most-traded first (or the `only` subset)."""
    items = [(s, a) for s, a in agg.items() if a["n"] > 0]
    if only is not None:
        items = [(s, a) for s, a in items if s in only]
    # Rank by trade count then |net| so the names with the most evidence lead.
    items.sort(key=lambda x: (x[1]["n"], abs(x[1]["net"])), reverse=True)
    lines = []
    for sym, a in items[:top_n]:
        wr = a["wins"] / a["n"] * 100 if a["n"] else 0.0
        tag = "WORKING" if a["net"] > 0 else ("BURNING" if a["net"] < 0 else "flat")
        lines.append(
            f"{sym.replace('USDT', '')}: {a['wins']}W/{a['losses']}L "
            f"(wr {wr:.0f}%) net {a['net']:+.1f} USDT [{tag}]"
        )
    return lines


# ---------------------------------------------------------------------------
# Injection — build the compact === YOUR MEMORY === block for the prompt.
# ---------------------------------------------------------------------------
def build_memory_block(client, symbols: set[str] | None = None,
                       lessons_only: bool = False) -> str | None:
    """Fold the per-symbol record + active lessons into one prompt block.

    `symbols` (focused calls) restricts the per-symbol record to the names in
    play. `lessons_only=True` skips the per-symbol table entirely (cheapest, for
    tight focused calls). Returns None when there is nothing to say yet."""
    parts: list[str] = []

    if not lessons_only:
        agg = symbol_records(client)
        recs = _format_symbol_records(agg, CFG.MEMORY_SYMBOL_TOP_N, only=symbols)
        if recs:
            parts.append(
                f"Per-symbol realized track record (last {CFG.MEMORY_LOOKBACK_DAYS}d — "
                f"favor WORKING names, be skeptical of BURNING ones):"
            )
            parts.extend(f"  {line}" for line in recs)

    lessons = journal.get_active_lessons(limit=CFG.MEMORY_MAX_LESSONS)
    if lessons:
        parts.append("Durable lessons you distilled from your own past trades:")
        for l in lessons:
            scope = l["scope"]
            prefix = "" if scope == "global" else f"[{scope.replace('USDT', '')}] "
            parts.append(f"  - {prefix}{l['text']}")

    if not parts:
        return None
    return "=== YOUR MEMORY (accumulated experience — hard-won, use it) ===\n" + "\n".join(parts)


# ---------------------------------------------------------------------------
# Tier 2 — the reflection call: rewrite the durable lesson set (once/day).
# ---------------------------------------------------------------------------
REFLECT_SYSTEM_PROMPT = """You are the REFLECTIVE MEMORY of an autonomous intraday crypto-futures bot (Binance, long/short, 5x-20x, always-invested book of 10-14 positions).

Your job: read the bot's RECENT DECISIONS and their REALIZED OUTCOMES, plus the lessons you wrote last time, and rewrite a short, durable set of LESSONS that will make future entries and exits better. This is how the bot remembers across a year of running.

Rules for good lessons:
- Be SPECIFIC and ACTIONABLE, grounded in the evidence shown. Prefer concrete patterns: a symbol that keeps losing ("stop shorting X, it squeezes"), a leverage that gets wicked out ("20x momentum entries stopped early — cap at 15x on high-ATR movers"), a setup that consistently pays or fails, a recurring timing/exit mistake.
- CARRY FORWARD prior lessons that still hold; DROP ones the recent data contradicts or that were one-offs. You are curating, not just appending.
- Each lesson: one sentence, under ~160 characters. Set scope to the exact SYMBOL (e.g. DOGEUSDT) when it's symbol-specific, else "global".
- Do NOT restate the standing strategy rules (always-invested mandate, SL/TP ranges, no martingale) — those are fixed elsewhere. Only capture what EXPERIENCE has taught that isn't already a rule.
- Quality over quantity: return only lessons you'd genuinely want your future self to read. Fewer, sharper lessons beat a long vague list.

Return the full new lesson set via the submit_lessons tool."""


REFLECT_TOOL = {
    "name": "submit_lessons",
    "description": "Store the distilled, durable trading lessons that replace the current memory set.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "lessons": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "scope": {
                            "type": "string",
                            "description": "'global' or a specific SYMBOL like DOGEUSDT when the lesson is symbol-specific.",
                        },
                        "text": {
                            "type": "string",
                            "description": "One concise, durable, actionable lesson under ~160 chars, grounded in the evidence.",
                        },
                    },
                    "required": ["scope", "text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["lessons"],
        "additionalProperties": False,
    },
}


def _build_reflection_digest(client) -> str:
    """Compact 'what happened' text: prior lessons + per-symbol record + recent
    realized outcomes + recent market views. Kept small on purpose."""
    parts: list[str] = []

    prior = journal.get_active_lessons(limit=CFG.MEMORY_MAX_LESSONS)
    if prior:
        parts.append("=== YOUR CURRENT LESSONS (carry forward the ones still valid) ===")
        for l in prior:
            scope = l["scope"]
            prefix = "" if scope == "global" else f"[{scope.replace('USDT', '')}] "
            parts.append(f"- {prefix}{l['text']}")
        parts.append("")

    agg = symbol_records(client)
    recs = _format_symbol_records(agg, top_n=20)
    if recs:
        parts.append(f"=== PER-SYMBOL REALIZED RESULTS (last {CFG.MEMORY_LOOKBACK_DAYS}d) ===")
        parts.extend(recs)
        parts.append("")

    # Recent realized outcomes (chronological tail) — the raw episodic material.
    try:
        rows = client.futures_income_history(incomeType="REALIZED_PNL", limit=1000)
    except Exception:
        rows = []
    if rows:
        rows = [r for r in rows if int(r.get("time", 0)) >= CFG.RESET_TS_MS]
        rows = sorted(rows, key=lambda r: int(r.get("time", 0)))[-40:]
        parts.append("=== RECENT CLOSED TRADES (chronological, realized P&L in USDT) ===")
        for r in rows:
            ts = datetime.fromtimestamp(int(r["time"]) / 1000, tz=timezone.utc)
            parts.append(f"{ts:%m-%d %H:%M} {r.get('symbol', ''):<12} {float(r['income']):+.2f}")
        parts.append("")

    decs = journal.recent_decisions(limit=CFG.REFLECT_DECISIONS_SAMPLE)
    if decs:
        parts.append("=== YOUR RECENT MARKET VIEWS (newest first) ===")
        for d in decs:
            mv = (d.get("market_view") or "").strip().replace("\n", " ")
            if mv:
                parts.append(f"[{d['ts'][:16]}] {mv[:240]}")
        parts.append("")

    parts.append(
        "Reflect on the above and rewrite your durable lesson set via submit_lessons. "
        "Curate: keep what still holds, drop what the data contradicts, add what's newly clear."
    )
    return "\n".join(parts)


def reflect(client, log=None) -> int:
    """Run one reflection: distill/curate the durable lesson set. Returns the
    number of lessons stored (0 = skipped or nothing produced).

    Cheap and infrequent (once/day). Never raises — memory is best-effort and
    must never take down the trading loop."""
    if not CFG.REFLECT_ENABLED:
        return 0
    # Reflection needs the API. In file-decider mode we can't run it; the
    # per-symbol track record (tier 1) still works without any model call.
    if CFG.DECISION_SOURCE != "api" or not CFG.ANTHROPIC_API_KEY:
        return 0
    try:
        digest = _build_reflection_digest(client)
        anth = anthropic.Anthropic(api_key=CFG.ANTHROPIC_API_KEY)
        resp = anth.messages.create(
            model=CFG.CLAUDE_MODEL,
            max_tokens=CFG.REFLECT_MAX_TOKENS,
            system=REFLECT_SYSTEM_PROMPT,
            tools=[REFLECT_TOOL],
            tool_choice={"type": "tool", "name": "submit_lessons"},
            messages=[{"role": "user", "content": digest}],
        )
        lessons = None
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_lessons":
                lessons = block.input.get("lessons")
                break
        if not lessons:
            return 0
        # Cap the set so memory stays bounded no matter what the model returns.
        lessons = lessons[: CFG.MEMORY_MAX_LESSONS]
        n = journal.replace_lessons(lessons)
        journal.set_meta("last_reflection_ts", datetime.now(timezone.utc).isoformat())
        journal.log_event("REFLECT", f"memory refreshed: {n} lessons distilled")
        if log is not None:
            log.info(f"reflection: stored {n} durable lessons "
                     f"(in={resp.usage.input_tokens} out={resp.usage.output_tokens})")
        return n
    except Exception as e:
        journal.log_event("WARN", f"reflection failed: {e}")
        if log is not None:
            log.warning(f"reflection failed: {e}")
        return 0


def seconds_since_last_reflection() -> float | None:
    """Age of the last reflection in seconds, or None if never run."""
    ts = journal.get_meta("last_reflection_ts")
    if not ts:
        return None
    try:
        last = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - last).total_seconds()
