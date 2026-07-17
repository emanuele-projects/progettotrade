"""Self-correction: summarize the bot's OWN recent realized P&L into a compact
text block that gets injected into the baseline prompt. Claude reads its own
track record and adapts (be more selective when the win-rate is poor, etc.).

Source of truth is Binance's realized-P&L income history (authoritative, and it
survives a journal reset), not the local journal. Cheap: one REST call, only on
the (infrequent) baseline cycle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import CFG


def build_performance_review(client, lookback_days: int = 7,
                             max_events: int = 60) -> str | None:
    """Return a prompt block reviewing recent closed-trade performance, or None
    if there is no realized-P&L history yet (fresh account)."""
    try:
        rows = client.futures_income_history(incomeType="REALIZED_PNL", limit=1000)
    except Exception:
        return None
    if not rows:
        return None

    # Floor the window at the clean-run reset: pre-reset trades were contaminated
    # by the double-instance bug, so learning from them would mislead. Until the
    # reset is >lookback_days old this floor is what actually bounds the window.
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp() * 1000
    cutoff_ms = max(cutoff_ms, float(CFG.RESET_TS_MS))
    events = [(float(r["income"]), r.get("symbol", ""), int(r["time"]))
              for r in rows if int(r.get("time", 0)) >= cutoff_ms]
    if not events:
        return None
    events.sort(key=lambda x: x[2])
    events = events[-max_events:]

    vals = [p for p, _, _ in events]
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    n = len(vals)
    wr = len(wins) / n * 100 if n else 0.0
    net = sum(vals)
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    best = max(events, key=lambda x: x[0])
    worst = min(events, key=lambda x: x[0])
    recent = vals[-10:]
    recent_wr = (len([v for v in recent if v > 0]) / len(recent) * 100) if recent else 0.0

    if wr < 25:
        guidance = (
            "Recent win-rate is POOR — most entries have been wrong. Keep the book invested (the mandate "
            "stands) but de-risk it HARD: drop the whole book to 5x-10x, widen stops so intraday noise "
            "can't tag them, balance longs and shorts toward market-neutral, and reserve 15x-20x for "
            "nothing. Rotate toward whichever side/setups HAVE been working; if one side keeps failing "
            "in this regime, tilt the balance the other way. Change the mix — not the investment level."
        )
    elif wr < 45:
        guidance = (
            "Below breakeven. Keep the book at the mandate but favor lower leverage on marginal picks, "
            "confirmatory flow on every entry, and do not re-open a name you were just stopped out of "
            "unless it genuinely re-qualifies. Prefer rotating into fresher setups over forcing re-entries."
        )
    else:
        guidance = (
            "Win-rate is acceptable — keep the discipline that is working: size stops for the mover's "
            "volatility, keep the book balanced and topped up, and let winners run toward the take-profit "
            "instead of closing early."
        )

    return "\n".join([
        f"=== YOUR RECENT TRADING PERFORMANCE (last {lookback_days}d, {n} closed trades) — LEARN FROM THIS ===",
        f"win-rate {wr:.0f}% ({len(wins)} win / {len(losses)} loss) | net realized {net:+.2f} USDT | "
        f"avg win {avg_w:+.2f} | avg loss {avg_l:+.2f}",
        f"last-10 win-rate {recent_wr:.0f}% | best {best[1]} {best[0]:+.2f} | worst {worst[1]} {worst[0]:+.2f}",
        f"SELF-CORRECTION GUIDANCE: {guidance}",
    ])
