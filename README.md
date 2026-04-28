# Trading Bot — Binance Futures Testnet

Long-only crypto futures bot. Leverage 10x, isolated margin, with martingale averaging-down on losers. **Paper trading only** — `USE_TESTNET=True` is hardcoded in `config.py`. Mainnet requires an explicit code edit.

## What it does each cycle (every 30 min)

1. Reads testnet account state.
2. Manages existing positions: take-profit, hard stop-loss, martingale add on drawdown.
3. Filters universe: mid-cap altcoins ($200M-$2B mcap) on Binance Futures perpetuals.
4. Computes features (RSI, EMA20, EMA50, momentum, volume) for each candidate.
5. Asks Claude (forced tool-use, prompt-cached system) which to long / flat / close.
6. Executes orders on testnet.
7. Logs to SQLite journal (`data/journal.db`).
8. Updates 3 shadow benchmarks (HODL BTC, weekly DCA, conservative 50/50 BTC+ETH @ 2x).

## Setup (one time)

### 1. Python + dependencies

```
cd d:\Claude\trading-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. API keys

- **Binance Futures Testnet**: https://testnet.binancefuture.com → log in with GitHub → "API Key" tab → create. (Account starts with 100,000 USDT play money.)
- **Anthropic**: https://console.anthropic.com
- **CryptoPanic** (optional): https://cryptopanic.com/developers/api/

Copy `.env.example` to `.env` and fill in the values.

### 3. Smoke test (one cycle, ignores KILL_SWITCH)

```
python main.py --once
```

Watch `data/bot.log`. You should see: ping ok, account read, universe filtered, features computed, Claude called, decisions logged, shadows updated.

## Run continuously

```
del KILL_SWITCH
scripts\run.bat
```

The bot loops every 30 minutes until stopped.

## Stop

- Graceful: `scripts\stop.bat` — bot exits at the next cycle boundary.
- Hard: Ctrl+C in the bot console.

## Status / report

```
python scripts\report.py
```

Prints equity curves: live testnet bot vs HODL / DCA / conservative-2x benchmarks.

## Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point and main loop |
| `config.py` | All tunable constants |
| `data.py` | Public Binance + CoinGecko + Fear&Greed + CryptoPanic |
| `strategy.py` | Claude API integration (tool use + prompt caching) |
| `execution.py` | Binance Futures testnet client + martingale + hard SL |
| `shadow.py` | Paper-tracked benchmarks |
| `journal.py` | SQLite logger |
| `scripts/` | run / stop / report helpers |

## Strategy parameters (in `config.py`)

| Knob | Default | Notes |
|------|---------|-------|
| `LEVERAGE` | 10 | Isolated margin |
| `INITIAL_DEPLOY_PCT` | 0.50 | 50% of capital deployed initially |
| `RESERVE_FOR_AVERAGING_PCT` | 0.50 | 50% kept liquid for martingale |
| `POSITION_MARGIN_PCT` | 0.10 | 10% per initial entry × max 5 = 50% |
| `MAX_CONCURRENT_POSITIONS` | 5 | |
| `MARTINGALE_TRIGGER_DRAWDOWN_PCT` | -0.05 | Add at -5% on collateral |
| `MARTINGALE_ADD_RATIO` | 0.50 | Add 50% of current margin per step |
| `MARTINGALE_MAX_LEVELS` | 3 | Max averages per position |
| `HARD_STOP_LOSS_PCT` | -0.30 | Hard cut on collateral |
| `TAKE_PROFIT_PCT` | 0.10 | |
| `EQUITY_FLOOR_PCT` | 0.20 | Auto-halt if total equity < 20% of initial |
| `LOOP_INTERVAL_SECONDS` | 1800 | 30 min |

## Safety rails

- `KILL_SWITCH` file: present = halt, absent = run.
- `EQUITY_FLOOR_PCT`: bot auto-halts (writes KILL_SWITCH) if testnet equity drops below 20% of initial. This is instrumentation — the goal is to keep collecting data, not to override your strategy. Set to `0.0` to disable.
- `USE_TESTNET=True` in `config.py` is hardcoded. Mainnet requires an explicit edit.
