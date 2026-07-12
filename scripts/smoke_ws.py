"""Smoke test Fase 2: stream WebSocket in sola lettura.

Verifica:
1. MarketStream (mainnet !markPrice@arr@1s) → tick freschi (<3s) e prezzi plausibili
2. tick_queue popolata per i simboli osservati
3. UserStream (testnet) → ORDER_TRADE_UPDATE ricevuti aprendo/chiudendo una posizione reale

Uso:  .venv\\Scripts\\python.exe scripts\\smoke_ws.py
"""
import logging
import pathlib
import queue
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

import events      # noqa: E402
import execution   # noqa: E402
import journal     # noqa: E402
import stream      # noqa: E402


def main() -> None:
    journal.init()
    state = stream.MarketState()
    bus = events.TriggerBus()
    tq: "queue.Queue[str]" = queue.Queue(maxsize=10_000)
    state.set_watch({"BTCUSDT", "ETHUSDT", "XRPUSDT"})
    state.set_held({"XRPUSDT"})

    ms = stream.MarketStream(state, bus, tq)
    ms.start()

    fills: list[dict] = []
    us = stream.UserStream()
    us.on_order_fill = fills.append
    us.start()

    print("... attendo 8s di tick ...")
    time.sleep(8)
    age = state.age_seconds()
    btc = state.price("BTCUSDT")
    print(f"tick age={age:.2f}s  BTC={btc}  XRP funding={state.funding('XRPUSDT')}  queue={tq.qsize()}")
    assert age < 3, f"tick stantii: {age:.1f}s"
    assert btc and btc > 1000, f"prezzo BTC implausibile: {btc}"
    assert tq.qsize() > 0, "tick queue vuota"

    print("... apro/chiudo SHORT XRPUSDT su testnet per testare lo user stream ...")
    client = execution.make_client()
    execution.open_position(client, "XRPUSDT", "SHORT", 20, sl_pct=-0.20, tp_pct=0.30, leverage=5)
    t0 = time.time()
    while not fills and time.time() - t0 < 20:
        time.sleep(0.5)
    pos = next((p for p in execution.get_open_positions(client) if p.symbol == "XRPUSDT"), None)
    assert pos is not None, "posizione non aperta"
    execution.close_position(client, pos, reason="manual_close")
    while len(fills) < 2 and time.time() - t0 < 40:
        time.sleep(0.5)

    print("fill via WS:", [(f.get("s"), f.get("S"), f.get("X"), f.get("R")) for f in fills])
    assert len(fills) >= 2, f"attesi 2 fill via user stream, ricevuti {len(fills)}"

    ms.stop()
    us.stop()
    print("SMOKE FASE 2: OK")


if __name__ == "__main__":
    main()
