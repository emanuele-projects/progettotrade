"""Trading bot main loop.

Run:
  python main.py           # loop forever (sleep 30 min between cycles)
  python main.py --once    # single cycle (smoke test, ignores KILL_SWITCH)

Stops gracefully when KILL_SWITCH file appears (or on Ctrl+C).
"""
from __future__ import annotations
import logging
import queue
import sys
import time
import traceback
from datetime import datetime, timezone

from binance.client import Client

from config import CFG, LARGE_CAP_ANCHORS, STRATEGY_ALLOCATIONS
import data
import events
import execution
import journal
import risk_engine
import scanner
import shadow
import strategy
import stream


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


def kill_mode() -> str | None:
    """None = no kill switch. 'soft' = stop trading/Claude but keep the risk
    engine protecting open positions. 'hard' (file contains the word 'hard') =
    full shutdown — at high leverage this leaves positions unprotected, use
    deliberately."""
    if not CFG.KILL_SWITCH.exists():
        return None
    try:
        content = CFG.KILL_SWITCH.read_text(encoding="utf-8")
    except Exception:
        content = ""
    return "hard" if "hard" in content.lower() else "soft"


def equity_floor_breached(equity: float) -> bool:
    return equity < CFG.INITIAL_CAPITAL_USDT * CFG.EQUITY_FLOOR_PCT


def manage_existing_positions(client: Client, log: logging.Logger,
                              risk_active: bool = False) -> list[dict]:
    """Serialize live positions for Claude; enforce SL/TP as cycle-level backstop.

    With the risk engine running (`risk_active`), tick-level enforcement should
    have already fired long before the cycle sees a breach — a hit here is
    logged as RISK_BACKSTOP (cheap insurance, should never happen). Martingale
    is owned by the risk engine in that mode."""
    positions = execution.get_open_positions(client)
    serialized: list[dict] = []
    for p in positions:
        p.martingale_levels = execution.count_martingale_levels(p.symbol)
        sl_pct, tp_pct = journal.get_position_targets(p.symbol)

        if p.unrealized_pnl_pct <= sl_pct:
            if risk_active:
                log.error(f"RISK_BACKSTOP sl {p.symbol} — the risk engine missed this")
                journal.log_event("RISK_BACKSTOP", f"sl {p.symbol} roe={p.unrealized_pnl_pct:+.2%}")
            log.warning(f"HARD_STOP {p.symbol} pnl={p.unrealized_pnl_pct:+.2%} (target SL {sl_pct:+.2%})")
            execution.close_position(client, p, reason="sl", trigger="cycle:backstop" if risk_active else "cycle")
            continue

        if p.unrealized_pnl_pct >= tp_pct:
            if risk_active:
                log.error(f"RISK_BACKSTOP tp {p.symbol} — the risk engine missed this")
                journal.log_event("RISK_BACKSTOP", f"tp {p.symbol} roe={p.unrealized_pnl_pct:+.2%}")
            log.info(f"TAKE_PROFIT {p.symbol} pnl={p.unrealized_pnl_pct:+.2%} (target TP {tp_pct:+.2%})")
            execution.close_position(client, p, reason="tp", trigger="cycle:backstop" if risk_active else "cycle")
            continue

        if (not risk_active
                and p.unrealized_pnl_pct <= CFG.MARTINGALE_TRIGGER_DRAWDOWN_PCT
                and p.martingale_levels < CFG.MARTINGALE_MAX_LEVELS):
            # Legacy path (--once / engine down). add_martingale enforces the
            # leverage cap internally.
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
    client: Client, decision: strategy.Decision, account: dict, log: logging.Logger,
    trigger: str | None = None,
) -> None:
    open_positions = execution.get_open_positions(client)
    open_syms = {p.symbol for p in open_positions}
    total_notional = sum(p.qty * p.mark_price for p in open_positions)
    margin_per_entry = CFG.INITIAL_CAPITAL_USDT * CFG.POSITION_MARGIN_PCT

    for d in decision.decisions:
        try:
            if d.action in ("long", "short"):
                side = "LONG" if d.action == "long" else "SHORT"
                if d.symbol in open_syms:
                    # One position per symbol; flipping requires an explicit close first.
                    continue
                if len(open_syms) >= CFG.MAX_CONCURRENT_POSITIONS:
                    log.info(f"skip {d.action} {d.symbol}: max positions reached")
                    continue
                if account["available_balance"] < margin_per_entry:
                    log.info(f"skip {d.action} {d.symbol}: avail {account['available_balance']:.2f} "
                             f"< margin {margin_per_entry:.2f}")
                    continue
                sl = d.stop_loss_pct if d.stop_loss_pct is not None else CFG.HARD_STOP_LOSS_PCT
                tp = d.take_profit_pct if d.take_profit_pct is not None else CFG.TAKE_PROFIT_PCT
                lev = d.leverage if d.leverage is not None else CFG.LEVERAGE
                new_notional = margin_per_entry * lev
                if total_notional + new_notional > CFG.MAX_TOTAL_NOTIONAL_USDT:
                    log.info(f"skip {d.action} {d.symbol}: notional cap "
                             f"({total_notional:,.0f} + {new_notional:,.0f} > {CFG.MAX_TOTAL_NOTIONAL_USDT:,.0f})")
                    journal.log_event("NOTIONAL_CAP", f"skip {d.action} {d.symbol} lev={lev}x")
                    continue
                log.info(f"OPEN {side} {d.symbol} margin={margin_per_entry:.2f} "
                         f"lev={lev}x conf={d.confidence:.2f} SL={sl:+.0%} TP={tp:+.0%} "
                         f":: {d.reasoning[:120]}")
                order = execution.open_position(client, d.symbol, side, margin_per_entry,
                                                sl_pct=sl, tp_pct=tp, leverage=lev, trigger=trigger)
                if order is not None:
                    open_syms.add(d.symbol)
                    total_notional += new_notional

            elif d.action == "close":
                position = next(
                    (p for p in execution.get_open_positions(client) if p.symbol == d.symbol),
                    None,
                )
                if position:
                    log.info(f"CLOSE {d.symbol} conf={d.confidence:.2f} :: {d.reasoning[:120]}")
                    execution.close_position(client, position, reason="manual_close", trigger=trigger)

        except Exception as e:
            # One bad symbol (e.g. listed on mainnet but not on testnet) must
            # never abort the rest of the batch or the post-execution reconcile.
            log.error(f"execution failed for {d.action} {d.symbol}: {e}")
            journal.log_event("ERROR", f"execute {d.action} {d.symbol}: {e}")


def baseline_cycle(client: Client, log: logging.Logger,
                   market_state: "stream.MarketState | None" = None,
                   position_cache: "risk_engine.PositionCache | None" = None,
                   trigger_tag: str = "baseline") -> None:
    """Slow lane: full universe + features + Claude. Runs on the (rare) baseline
    timer and on macro-regime triggers (trigger_tag records which)."""
    log.info("=" * 60)
    log.info(f"baseline cycle @ {datetime.now(timezone.utc).isoformat()}")

    if market_state is not None:
        age = market_state.age_seconds()
        log.info(f"ws market tick age: {age:.1f}s (watch={len(market_state.watch())} symbols)")

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

    open_serialized = manage_existing_positions(client, log,
                                                risk_active=position_cache is not None)
    if position_cache is not None:
        position_cache.reconcile(client)
    if market_state is not None:
        market_state.set_held({p["symbol"] for p in open_serialized})

    universe = data.filter_universe()
    if market_state is not None and universe:
        market_state.set_watch(set(universe) | set(LARGE_CAP_ANCHORS))
    if not universe:
        log.warning("empty universe — skipping decision")
        if _SHADOWS_ENABLED:
            try:
                shadow.update_shadows()
            except Exception as e:
                log.warning(f"shadow update failed: {e}")
        return
    log.info(f"universe ({len(universe)}): {','.join(universe)}")

    btc_features = data.compute_features("BTCUSDT", risk_tier="large_cap",
                                         max_leverage=execution.get_max_leverage(client, "BTCUSDT"))
    candidate_features = []
    for sym in universe:
        tier = "large_cap" if sym in _LARGE_CAP_SET else "mid_cap"
        try:
            candidate_features.append(data.compute_features(
                sym, risk_tier=tier,
                max_leverage=execution.get_max_leverage(client, sym)))
        except Exception as e:
            log.warning(f"features failed for {sym}: {e}")

    fg = data.get_fear_greed()
    news = data.get_news_headlines(universe)
    operator_notes = journal.get_active_operator_notes()
    if operator_notes:
        log.info(f"operator notes active: {len(operator_notes)}")

    log.info("calling Claude for decisions…")
    decision, usage = strategy.decide(
        candidate_features, open_serialized, fg, btc_features, news,
        operator_notes=operator_notes,
    )
    journal.log_decision(
        market_view=decision.market_view,
        decisions=[d.model_dump() for d in decision.decisions],
        trigger=trigger_tag, model=CFG.CLAUDE_MODEL,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
    )
    log.info(f"tokens: in={usage['input_tokens']} out={usage['output_tokens']} "
             f"cache_read={usage['cache_read_input_tokens']} cache_write={usage['cache_creation_input_tokens']}")
    log.info(f"market_view: {decision.market_view}")
    for d in decision.decisions:
        log.info(f"  {d.symbol} -> {d.action} (conf={d.confidence:.2f})")

    execute_decisions(client, decision, account, log, trigger=trigger_tag)
    if position_cache is not None:
        position_cache.reconcile(client)
        if market_state is not None:
            market_state.set_held(position_cache.held_symbols())

    if _SHADOWS_ENABLED:
        try:
            shadow.update_shadows()
        except Exception as e:
            log.warning(f"shadow update failed: {e}")

    log.info("cycle end")


def focused_cycle(client: Client, log: logging.Logger, triggers: list["events.Trigger"],
                  market_state: "stream.MarketState",
                  position_cache: "risk_engine.PositionCache") -> None:
    """Fast lane follow-up: Claude re-evaluates only the symbols involved in the
    trigger batch (plus every open position). No universe scan, no news fetch."""
    log.info("=" * 60)
    tags = ", ".join(t.tag() + (f" ({t.detail})" if t.detail else "") for t in triggers[:8])
    log.info(f"FOCUSED cycle @ {datetime.now(timezone.utc).isoformat()} — {tags}")

    account = execution.get_account(client)
    journal.log_equity(
        wallet=account["wallet_balance"], unrealized=account["unrealized_pnl"],
        equity=account["total_equity"],
        open_positions=len(execution.get_open_positions(client)), source="live",
    )
    if equity_floor_breached(account["total_equity"]):
        log.error("EQUITY FLOOR BREACHED (focused) — halting.")
        journal.log_event("HALT", "equity floor breached")
        CFG.KILL_SWITCH.write_text("auto-halt: equity floor breached\n", encoding="utf-8")
        return

    open_serialized = manage_existing_positions(client, log, risk_active=True)
    position_cache.reconcile(client)

    affected = list({t.symbol for t in triggers if t.symbol})[:5]
    candidate_features = []
    for sym in affected:
        tier = "large_cap" if sym in _LARGE_CAP_SET else "mid_cap"
        try:
            candidate_features.append(data.compute_features(
                sym, risk_tier=tier,
                max_leverage=execution.get_max_leverage(client, sym)))
        except Exception as e:
            log.warning(f"features failed for {sym}: {e}")

    btc_features = data.compute_features("BTCUSDT", risk_tier="large_cap",
                                         max_leverage=execution.get_max_leverage(client, "BTCUSDT"))
    fg = data.get_fear_greed()
    operator_notes = journal.get_active_operator_notes()
    trigger_lines = [f"[{t.kind}] {t.symbol or 'global'}: {t.detail or '-'}" for t in triggers[:10]]

    log.info("calling Claude (focused)…")
    decision, usage = strategy.decide(
        candidate_features, open_serialized, fg, btc_features, news=[],
        operator_notes=operator_notes,
        trigger_lines=trigger_lines, focused=True,
    )
    trigger_tag = ",".join(sorted({t.kind for t in triggers}))
    journal.log_decision(
        market_view=decision.market_view,
        decisions=[d.model_dump() for d in decision.decisions],
        trigger=f"event:{trigger_tag}", model=CFG.CLAUDE_MODEL,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
    )
    log.info(f"tokens: in={usage['input_tokens']} out={usage['output_tokens']} "
             f"cache_read={usage['cache_read_input_tokens']} cache_write={usage['cache_creation_input_tokens']}")
    for d in decision.decisions:
        log.info(f"  {d.symbol} -> {d.action} (conf={d.confidence:.2f})")

    execute_decisions(client, decision, account, log, trigger=f"event:{trigger_tag}")
    position_cache.reconcile(client)
    market_state.set_held(position_cache.held_symbols())
    log.info("focused cycle end")


def main() -> None:
    log = setup_logging()
    journal.init()
    once = "--once" in sys.argv

    log.info(f"trading-bot start — testnet={CFG.USE_TESTNET} dry_run={CFG.DRY_RUN} once={once}")

    if kill_mode() == "hard" and not once:
        log.error("KILL_SWITCH (hard) present — refusing to start. Delete the file to enable trading.")
        return
    # A soft kill switch does NOT block startup: streams + risk engine come up
    # to protect any open positions; trading stays paused until the file is removed.

    client = execution.make_client()
    try:
        client.futures_ping()
        log.info("binance futures ping ok")
    except Exception as e:
        log.error(f"can't reach Binance Futures: {e}")
        return

    if once:
        try:
            baseline_cycle(client, log)
        except Exception as e:
            log.error(f"cycle error: {e}\n{traceback.format_exc()}")
        return

    # ---- real-time plumbing ----
    # Startup order is deliberate: seed the position cache from REST truth,
    # bring the risk engine online, THEN open the streams (protection first).
    market_state = stream.MarketState()
    trigger_bus = events.TriggerBus()
    tick_queue: "queue.Queue[str]" = queue.Queue(maxsize=10_000)

    position_cache = risk_engine.PositionCache()
    position_cache.reconcile(client)
    held = position_cache.held_symbols()
    market_state.set_held(held)
    market_state.set_watch(set(LARGE_CAP_ANCHORS) | held)

    engine = risk_engine.RiskEngine(
        position_cache, market_state, tick_queue, trigger_bus,
        initial_wallet=execution.get_account(client)["wallet_balance"],
    )
    engine.start()

    market_stream = stream.MarketStream(market_state, trigger_bus, tick_queue)
    user_stream = stream.UserStream()

    def _on_account_update(payload: dict) -> None:
        position_cache.apply_account_update(payload)
        engine.apply_wallet_update(payload)

    user_stream.on_account_update = _on_account_update

    # Local signal scanner (free): the gatekeeper that decides WHEN Claude is
    # worth calling, so the baseline can be rare instead of every 30 min.
    signal_scanner = None
    if CFG.SCANNER_ENABLED:
        signal_scanner = scanner.SignalScanner(market_state, position_cache, trigger_bus)
        signal_scanner.start()

    # The watchdog reconciles on its own client (one Client per thread).
    wd_client = execution.make_client()
    watchdog = stream.Watchdog(
        market_stream, user_stream, market_state,
        reconcile=lambda: position_cache.reconcile(wd_client),
        reconcile_asap=position_cache.needs_reconcile,
    )
    try:
        market_stream.start()
        user_stream.start()
        watchdog.start()
    except Exception as e:
        log.error(f"stream startup failed: {e}\n{traceback.format_exc()}")
        journal.log_event("ERROR", f"stream startup failed: {e}")

    # ---- event loop: baseline timer + trigger-driven focused cycles ----
    policy = events.TriggerPolicy()
    next_baseline = time.monotonic()  # first baseline runs immediately
    soft_logged = False
    try:
        while True:
            mode = kill_mode()
            if mode == "hard":
                log.warning("KILL_SWITCH (hard) — shutting down.")
                break
            if mode == "soft":
                if not soft_logged:
                    log.warning("KILL_SWITCH (soft) — trading paused; risk engine keeps "
                                "protecting open positions. Delete the file to resume, "
                                "write 'hard' in it to shut down completely.")
                    journal.log_event("KILL_SOFT", "trading paused, protection active")
                    soft_logged = True
                trigger_bus.drain()  # don't let stale triggers pile up
                time.sleep(5)
                continue
            if soft_logged:
                log.info("KILL_SWITCH removed — trading resumed.")
                journal.log_event("RESUME", "kill switch removed")
                soft_logged = False

            try:
                # Wait for a trigger, capped so the kill switch is re-checked
                # at least once a minute even on a quiet market.
                timeout = max(0.0, next_baseline - time.monotonic())
                trig = trigger_bus.get_or_none(timeout=min(timeout, 60.0))

                if trig is None:
                    if time.monotonic() < next_baseline:
                        continue  # just a kill-check wakeup
                    if policy.baseline_should_skip():
                        log.info("baseline deferred (recent Claude call)")
                        next_baseline = time.monotonic() + CFG.BASELINE_SKIP_IF_CALLED_WITHIN
                        continue
                    try:
                        baseline_cycle(client, log, market_state=market_state,
                                       position_cache=position_cache)
                        policy.record_call(is_event=False)
                        next_baseline = time.monotonic() + CFG.BASELINE_INTERVAL_SECONDS
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        log.error(f"baseline cycle error: {e}\n{traceback.format_exc()}")
                        journal.log_event("ERROR", f"baseline: {e}")
                        next_baseline = time.monotonic() + 120  # retry backoff
                    continue

                # Trigger received: debounce (risk_exit jumps the queue), batch, rate-limit.
                batch = [trig]
                if trig.kind != "risk_exit":
                    deadline = time.monotonic() + CFG.EVENT_DEBOUNCE_SECONDS
                    while time.monotonic() < deadline:
                        extra = trigger_bus.get_or_none(
                            timeout=min(1.0, max(0.0, deadline - time.monotonic())))
                        if extra is not None:
                            batch.append(extra)
                batch.extend(trigger_bus.drain())

                allowed, deny_reason = policy.can_event_call()
                if not allowed:
                    log.info(f"triggers dropped ({deny_reason}): {[t.tag() for t in batch[:6]]}")
                    journal.log_event("TRIGGER_DROPPED",
                                      f"{len(batch)} triggers ({deny_reason}): "
                                      + ", ".join(t.tag() for t in batch[:6]))
                    continue

                # A macro trigger (no symbol) means the whole regime shifted →
                # re-evaluate the entire book, not a focused subset. It also
                # resets the baseline timer since it IS a full review.
                if any(t.symbol is None for t in batch):
                    tag = ",".join(sorted({t.kind for t in batch}))
                    baseline_cycle(client, log, market_state=market_state,
                                   position_cache=position_cache, trigger_tag=f"event:{tag}")
                    next_baseline = time.monotonic() + CFG.BASELINE_INTERVAL_SECONDS
                else:
                    focused_cycle(client, log, batch, market_state, position_cache)
                policy.record_call(is_event=True)

            except KeyboardInterrupt:
                log.warning("interrupted by user")
                break
            except Exception as e:
                log.error(f"cycle error: {e}\n{traceback.format_exc()}")
                journal.log_event("ERROR", str(e))
                time.sleep(5)  # don't spin on a persistent failure
    finally:
        if signal_scanner is not None:
            signal_scanner.stop_event.set()
        engine.stop_event.set()
        watchdog.stop_event.set()
        market_stream.stop()
        user_stream.stop()


if __name__ == "__main__":
    main()
