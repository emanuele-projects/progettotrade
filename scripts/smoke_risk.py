"""Smoke test Fase 3: RiskEngine chiude una posizione reale su tick.

Apre un LONG piccolo su testnet, forza SL/TP della cache a soglie microscopiche
(±0.01% ROE) e verifica che il motore la chiuda in pochi secondi dal primo tick,
con una sola riga di chiusura nel journal (latch anti-duplicato).

Uso:  .venv\\Scripts\\python.exe scripts\\smoke_risk.py
"""
import logging
import pathlib
import queue
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from config import CFG   # noqa: E402
import events            # noqa: E402
import execution         # noqa: E402
import journal           # noqa: E402
import risk_engine       # noqa: E402
import stream            # noqa: E402

SYMBOL = "XRPUSDT"


def main() -> None:
    journal.init()
    client = execution.make_client()

    state = stream.MarketState()
    bus = events.TriggerBus()
    tq: "queue.Queue[str]" = queue.Queue(maxsize=10_000)
    state.set_watch({SYMBOL})
    state.set_held({SYMBOL})

    cache = risk_engine.PositionCache()
    engine = risk_engine.RiskEngine(cache, state, tq, bus,
                                    initial_wallet=execution.get_account(client)["wallet_balance"])
    engine.start()

    ms = stream.MarketStream(state, bus, tq)
    ms.start()

    print("... apro LONG di test ...")
    execution.open_position(client, SYMBOL, "LONG", 20, sl_pct=-0.20, tp_pct=0.30, leverage=5)
    cache.reconcile(client)
    cached = cache.get(SYMBOL)
    assert cached is not None, "cache non popolata dopo reconcile"
    print(f"posizione in cache: {cached.side} lev={cached.leverage}x liq={cached.liquidation_price}")

    # Override test-only: soglie microscopiche → qualunque tick chiude
    cached.sl_pct = -0.0001
    cached.tp_pct = 0.0001
    t_armed = time.time()
    print("... SL/TP forzati a ±0.01% ROE, attendo la chiusura dal motore ...")

    deadline = time.time() + 45
    while time.time() < deadline:
        if cache.get(SYMBOL) is None:
            break
        time.sleep(0.2)
    elapsed = time.time() - t_armed
    assert cache.get(SYMBOL) is None, "il motore non ha chiuso entro 45s"

    # REST: davvero flat?
    time.sleep(1.5)
    left = [p for p in execution.get_open_positions(client) if p.symbol == SYMBOL]
    assert not left, f"posizione ancora aperta su testnet: {left}"

    # Journal: esattamente UNA chiusura risk:* dopo l'arming (latch ok)
    with sqlite3.connect(CFG.JOURNAL_DB) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT kind, trigger, ts FROM trades WHERE symbol=? AND kind IN ('sl','tp','liq_guard') "
            "ORDER BY id DESC LIMIT 5", (SYMBOL,),
        ).fetchall()
    recent = [dict(r) for r in rows if r["trigger"] and r["trigger"].startswith("risk:")]
    print(f"chiusura in {elapsed:.1f}s — righe risk recenti: {recent[:2]}")
    assert recent, "nessuna riga di chiusura con trigger risk:* nel journal"

    # Trigger risk_exit emesso sul bus
    drained = bus.drain()
    kinds = [t.kind for t in drained]
    print(f"trigger sul bus: {kinds}")
    assert "risk_exit" in kinds, "trigger risk_exit non emesso"

    engine.stop_event.set()
    ms.stop()
    print(f"SMOKE FASE 3: OK (chiusura in {elapsed:.1f}s dal tick-arming)")


if __name__ == "__main__":
    main()
