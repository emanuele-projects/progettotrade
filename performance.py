"""Self-correction: summarize the bot's OWN recent realized P&L into a compact
text block that gets injected into the baseline prompt. Claude reads its own
track record and adapts (be more selective when the win-rate is poor, etc.).

Source of truth is Binance's realized-P&L income history (authoritative, and it
survives a journal reset), not the local journal. Cheap: one REST call, only on
the (infrequent) baseline cycle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


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

    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp() * 1000
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
            "Recent win-rate is POOR — most entries have been wrong. Be FAR more selective: "
            "take only A+ setups where trend, flow and OI all agree, and pass on everything marginal. "
            "Trade FEWER positions, prefer 5x-10x over 15x-20x, and set stops wide enough that ordinary "
            "intraday noise on a volatile mover does not tag them. If one side (long or short) keeps failing "
            "in the current regime, favor the other. When there is no clean read, stay FLAT — a skipped trade "
            "beats a flip-flop loss."
        )
    elif wr < 45:
        guidance = (
            "Below breakeven. Tighten entry criteria — skip low-conviction setups and act only on clear "
            "momentum with confirming flow. Do not force trades, and do not re-open a name you were just "
            "stopped out of unless it genuinely re-qualifies."
        )
    else:
        guidance = (
            "Win-rate is acceptable — keep the discipline that is working: stay selective, size stops for "
            "the mover's volatility, and let winners run toward the take-profit instead of closing early."
        )

    return "\n".join([
        f"=== YOUR RECENT TRADING PERFORMANCE (last {lookback_days}d, {n} closed trades) — LEARN FROM THIS ===",
        f"win-rate {wr:.0f}% ({len(wins)} win / {len(losses)} loss) | net realized {net:+.2f} USDT | "
        f"avg win {avg_w:+.2f} | avg loss {avg_l:+.2f}",
        f"last-10 win-rate {recent_wr:.0f}% | best {best[1]} {best[0]:+.2f} | worst {worst[1]} {worst[0]:+.2f}",
        f"SELF-CORRECTION GUIDANCE: {guidance}",
    ])
