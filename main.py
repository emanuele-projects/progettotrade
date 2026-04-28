"""Trading bot main loop.

Run:
  python main.py           # loop forever (sleep 30 min between cycles)
  python main.py --once    # single cycle (smoke test, ignores KILL_SWITCH)

Stops gracefully when KILL_SWITCH file appears (or on Ctrl+C).
"""
from __future__ import annotations
import logging
import sys
import time
import traceback
from datetime import datetime, timezone

from binance.client import Client

from config import CFG, LARGE_CAP_ANCHORS, STRATEGY_ALLOCATIONS
import data
import execution
import journal
import shadow
import strategy


_SHADOWS_ENABLED = any(
    STRATEGY_ALLOCATIONS.get(k, 0) > 0 for k in ("hodl", "dca", "conservative_2x")
)
_LARGE_CAP_SET = set(LARGE_CAP_ANCHORS)


def setup_logging() -> logging.Logger:
    CFG.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Force stdout to UTF-8 so logging non-ASCII symbol names (e.g. CJK meme coins
    # listed on Binance Futures) doesn't crash on Windows cp1252 consoles.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO, format=fmt, force=True,
        handlers=[
            logging.FileHandler(CFG.LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("bot")


def kill_switch_active() -> bool:
    return CFG.KILL_SWITCH.exists()


def equity_floor_breached(equity: float) -> bool:
    return equity < CFG.INITIAL_CAPITAL_USDT * CFG.EQUITY_FLOOR_PCT


def manage_existing_positions(client: Client, log: logging.Logger) -> list[dict]:
    """Apply TP / hard SL / martingale to live positions. Return survivors for Claude."""
    positions = execution.get_open_positions(client)
    serialized: list[dict] = []
    for p in positions:
        p.martingale_levels = execution.count_martingale_levels(p.symbol)
        sl_pct, tp_pct = journal.get_position_targets(p.symbol)

        if p.unrealized_pnl_pct <= sl_pct:
            log.warning(f"HARD_STOP {p.symbol} pnl={p.unrealized_pnl_pct:+.2%} (target SL {sl_pct:+.2%})")
            execution.close_position(client, p, reason="sl")
            continue

        if p.unrealized_pnl_pct >= tp_pct:
            log.info(f"TAKE_PROFIT {p.symbol} pnl={p.unrealized_pnl_pct:+.2%} (target TP {tp_pct:+.2%})")
            execution.close_position(client, p, reason="tp")
            continue

        if (p.unrealized_pnl_pct <= CFG.MARTINGALE_TRIGGER_DRAWDOWN_PCT
                and p.martingale_levels < CFG.MARTINGALE_MAX_LEVELS):
            log.info(f"MARTINGALE add {p.symbol} level={p.martingale_levels + 1} "
                     f"pnl={p.unrealized_pnl_pct:+.2%}")
            execution.add_martingale(client, p)
            continue  # skip Claude this cycle for this position

        # Idempotent guard: ensure server-side SL/TP exist for this position
        execution.ensure_protective_orders(client, p)

        serialized.append({
            "symbol": p.symbol, "side": p.side, "qty": p.qty,
            "entry_price": p.entry_price, "mark_price": p.mark_price,
            "unrealized_pnl_pct": p.unrealized_pnl_pct,
            "martingale_levels": p.martingale_levels,
            "sl_pct": sl_pct, "tp_pct": tp_pct, "leverage": p.leverage,
        })
    return serialized


def execute_decisions(
    client: Client, decision: strategy.Decision, account: dict, log: logging.Logger
) -> None:
    open_syms = {p.symbol for p in execution.get_open_positions(client)}
    margin_per_entry = CFG.INITIAL_CAPITAL_USDT * CFG.POSITION_MARGIN_PCT

    for d in decision.decisions:
        if d.action == "long":
            if d.symbol in open_syms:
                continue
            if len(open_syms) >= CFG.MAX_CONCURRENT_POSITIONS:
                log.info(f"skip long {d.symbol}: max positions reached")
                continue
            if account["available_balance"] < margin_per_entry:
                log.info(f"skip long {d.symbol}: avail {account['available_balance']:.2f} "
                         f"< margin {margin_per_entry:.2f}")
                continue
            sl = d.stop_loss_pct if d.stop_loss_pct is not None else CFG.HARD_STOP_LOSS_PCT
            tp = d.take_profit_pct if d.take_profit_pct is not None else CFG.TAKE_PROFIT_PCT
            lev = d.leverage if d.leverage is not None else CFG.LEVERAGE
            log.info(f"OPEN LONG {d.symbol} margin={margin_per_entry:.2f} "
                     f"lev={lev}x conf={d.confidence:.2f} SL={sl:+.0%} TP={tp:+.0%} "
                     f":: {d.reasoning[:120]}")
            execution.open_long(client, d.symbol, margin_per_entry,
                                sl_pct=sl, tp_pct=tp, leverage=lev)
            open_syms.add(d.symbol)

        elif d.action == "close":
            position = next(
                (p for p in execution.get_open_positions(client) if p.symbol == d.symbol),
                None,
            )
            if position:
                log.info(f"CLOSE {d.symbol} conf={d.confidence:.2f} :: {d.reasoning[:120]}")
                execution.close_position(client, position, reason="manual_close")


def one_cycle(client: Client, log: logging.Logger) -> None:
    log.info("=" * 60)
    log.info(f"cycle start @ {datetime.now(timezone.utc).isoformat()}")

    account = execution.get_account(client)
    journal.log_equity(
        wallet=account["wallet_balance"],
        unrealized=account["unrealized_pnl"],
        equity=account["total_equity"],
        open_positions=len(execution.get_open_positions(client)),
        source="live",
    )
    log.info(
        f"equity={account['total_equity']:.2f} wallet={account['wallet_balance']:.2f} "
        f"unrealized={account['unrealized_pnl']:+.2f} avail={account['available_balance']:.2f}"
    )

    if equity_floor_breached(account["total_equity"]):
        floor = CFG.INITIAL_CAPITAL_USDT * CFG.EQUITY_FLOOR_PCT
        log.error(f"EQUITY FLOOR BREACHED — equity={account['total_equity']:.2f} < {floor:.2f}. Halting.")
        journal.log_event("HALT", "equity floor breached")
        CFG.KILL_SWITCH.write_text("auto-halt: equity floor breached\n", encoding="utf-8")
        return

    open_serialized = manage_existing_positions(client, log)

    universe = data.filter_universe()
    if not universe:
        log.warning("empty universe — skipping decision")
        if _SHADOWS_ENABLED:
            try:
                shadow.update_shadows()
            except Exception as e:
                log.warning(f"shadow update failed: {e}")
        return
    log.info(f"universe ({len(universe)}): {','.join(universe)}")

    btc_features = data.compute_features("BTCUSDT", risk_tier="large_cap")
    candidate_features = []
    for sym in universe:
        tier = "large_cap" if sym in _LARGE_CAP_SET else "mid_cap"
        try:
            candidate_features.append(data.compute_features(sym, risk_tier=tier))
        except Exception as e:
            log.warning(f"features failed for {sym}: {e}")

    fg = data.get_fear_greed()
    news = data.get_news_headlines(universe)
    operator_notes = journal.get_active_operator_notes()
    if operator_notes:
        log.info(f"operator notes active: {len(operator_notes)}")

    log.info("calling Claude for decisions…")
    decision = strategy.decide(
        candidate_features, open_serialized, fg, btc_features, news,
        operator_notes=operator_notes,
    )
    journal.log_decision(
        market_view=decision.market_view,
        decisions=[d.model_dump() for d in decision.decisions],
    )
    log.info(f"market_view: {decision.market_view}")
    for d in decision.decisions:
        log.info(f"  {d.symbol} -> {d.action} (conf={d.confidence:.2f})")

    execute_decisions(client, decision, account, log)

    if _SHADOWS_ENABLED:
        try:
            shadow.update_shadows()
        except Exception as e:
            log.warning(f"shadow update failed: {e}")

    log.info("cycle end")


def main() -> None:
    log = setup_logging()
    journal.init()
    once = "--once" in sys.argv

    log.info(f"trading-bot start — testnet={CFG.USE_TESTNET} dry_run={CFG.DRY_RUN} once={once}")

    if kill_switch_active() and not once:
        log.error("KILL_SWITCH present — refusing to start. Delete the file to enable trading.")
        return

    client = execution.make_client()
    try:
        client.futures_ping()
        log.info("binance futures ping ok")
    except Exception as e:
        log.error(f"can't reach Binance Futures: {e}")
        return

    if once:
        try:
            one_cycle(client, log)
        except Exception as e:
            log.error(f"cycle error: {e}\n{traceback.format_exc()}")
        return

    while True:
        if kill_switch_active():
            log.warning("KILL_SWITCH detected — exiting loop.")
            break
        try:
            one_cycle(client, log)
        except KeyboardInterrupt:
            log.warning("interrupted by user")
            break
        except Exception as e:
            log.error(f"cycle error: {e}\n{traceback.format_exc()}")
            journal.log_event("ERROR", str(e))
        log.info(f"sleeping {CFG.LOOP_INTERVAL_SECONDS}s…")
        time.sleep(CFG.LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
