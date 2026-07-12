# Trading Bot — Binance Futures Testnet

Crypto futures bot **long/short** con leva per-trade **5x–20x**, margine isolato, protezione **in tempo reale via WebSocket** e agente decisionale Claude chiamato sia a cadenza fissa sia **su eventi di mercato**. **Paper trading only** — `USE_TESTNET=True` è hardcoded in `config.py`. Mainnet richiede un edit esplicito (vedi checklist in fondo).

## Architettura

```
main.py (unico processo, supervisionato da runner.py su Railway)
├── MarketStream   WS mainnet !markPrice@arr@1s → prezzi+funding (MarketState)
├── UserStream     WS testnet → fill e aggiornamenti account (PositionCache)
├── RiskEngine     su ogni tick: guardia pre-liquidazione (75%), SL/TP ROE,
│                  martingala (solo ≤10x) — chiusure reduceOnly in <1s
├── SignalScanner  GRATIS: ogni 5 min analizza watchlist∪posizioni (RSI/EMA/
│                  breakout/notizie/macro) e sveglia Claude SOLO sul segnale
├── Watchdog       tick stantii → restart stream + fallback REST; riconciliazione
└── Main loop      event loop:
                   · baseline ogni 3h → rete di sicurezza, rivaluta tutto il libro
                   · segnale scanner (incrocio EMA, RSI, breakout, notizia,
                     posizione contro-tesi) → ciclo FOCUSED sui simboli coinvolti
                   · trigger macro (F&G/BTC) → baseline completo
                   · debounce 60s, min 180s tra chiamate, max 8 event-call/ora
```

### Perché costa poco

Lo **screening è locale e gratuito**: il bot calcola RSI/EMA/breakout su tutta la
watchlist ogni 5 minuti usando dati Binance (REST/WS, non costano crediti). Claude
— l'unica cosa a pagamento — viene chiamato **solo quando lo scanner trova un
segnale reale** (un incrocio, un breakout, una notizia, una posizione che cambia
tesi), non a orologio fisso. A mercato piatto il bot non spende nulla in API.
Stima: ~$0,40–0,90/giorno invece dei ~$2/giorno del baseline ogni 30 min.

- **Dati di mercato**: sempre da **mainnet** (REST + WS pubblici) — feed stabile.
- **Trading**: sul **testnet** (chiavi in `.env`).
- **SL/TP**: percentuali sul **collaterale (ROE)**, scelte da Claude per ogni trade; distanza prezzo = |ROE|/leva. Enforcement tick-by-tick dal RiskEngine (su testnet gli ordini condizionali server-side sono disabilitati per il bug -4130; su mainnet si riattivano come backstop).
- **Leva**: Claude sceglie 5/10/15/20 per trade; clampata al bracket Binance del simbolo e lo SL è clampato entro il 60% della distanza di liquidazione. A 15x/20x la martingala è disattivata.
- **Costi Claude**: system prompt cachato (TTL 1h); ogni decisione logga token e cache-hit nel journal (`decisions.model/input_tokens/output_tokens`).

## Setup (una tantum)

### 1. Python + dipendenze

```
cd d:\Claude\trading-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Chiavi API

- **Binance Futures Testnet**: https://testnet.binancefuture.com → login con GitHub → tab "API Key" → create. (L'account parte con USDT finti.)
- **Anthropic**: https://console.anthropic.com — servono **crediti attivi**, altrimenti le decisioni falliscono con `credit balance too low` (il loop sopravvive e riprova, ma non trada).
- **CryptoPanic** (opzionale): https://cryptopanic.com/developers/api/

Copia `.env.example` in `.env` e riempi i valori.

### 3. Smoke test

```
python main.py --once            # un ciclo completo (ignora KILL_SWITCH)
.venv\Scripts\python.exe -m unittest discover -s tests   # unit test matematica rischio/policy
python scripts\smoke_ws.py       # stream WS: tick freschi + fill via user stream
python scripts\smoke_risk.py     # RiskEngine: chiusura reale <2s dal tick
```

## Esecuzione continua

```
del KILL_SWITCH
scripts\run.bat
```

## Stop — soft vs hard

- **Soft** (default): `scripts\stop.bat` o crea il file `KILL_SWITCH` con testo qualsiasi. Il trading e le chiamate Claude si fermano, ma **stream e RiskEngine restano vivi a proteggere le posizioni aperte**. Cancella il file per riprendere.
- **Hard**: scrivi la parola `hard` dentro `KILL_SWITCH` — il processo esce del tutto (a leva alta lascia le posizioni senza protezione locale: usalo consapevolmente).
- L'auto-halt per equity floor scrive un kill **soft**: le posizioni restano protette.

## File

| File | Scopo |
|------|-------|
| `main.py` | Event loop: baseline + cicli focused, avvio thread |
| `config.py` | Tutte le costanti (leve, soglie trigger, policy Claude, rischio) |
| `stream.py` | MarketState, stream WS mainnet/testnet, Watchdog |
| `risk_engine.py` | PositionCache + enforcement tick-level |
| `events.py` | Trigger, TriggerBus, TriggerPolicy (debounce/cap) |
| `strategy.py` | Claude API (tool use forzato + prompt caching), schema decisioni |
| `execution.py` | Ordini, bracket leva, clamp liquidazione, martingala |
| `data.py` | Features multi-timeframe + flow futures (REST mainnet) |
| `journal.py` | SQLite (WAL): decisioni, trade, equity, eventi, note operatore |
| `dashboard.py` | Dashboard Streamlit (password: env `DASHBOARD_PASSWORD`) |
| `scripts/` | run / stop / report / smoke test |

## Parametri principali (`config.py`)

| Chiave | Default | Note |
|--------|---------|------|
| `ALLOWED_LEVERAGES` | (5,10,15,20) | Scelta per-trade dell'agente |
| `MAX_TOTAL_NOTIONAL_USDT` | 60.000 | Cap Σ(margine×leva) |
| `POSITION_MARGIN_PCT` | 0.05 | $500 per entry su $10k |
| `MAX_CONCURRENT_POSITIONS` | 10 | long+short |
| `RISK_LIQ_GUARD_FRACTION` | 0.75 | Chiusura forzata al 75% della strada verso la liquidazione |
| `RISK_SL_MAX_FRACTION_OF_LIQ` | 0.60 | Clamp SL entro il 60% della distanza di liquidazione |
| `MARTINGALE_MAX_LEVERAGE` | 10 | Niente media sopra 10x |
| `MARTINGALE_TRIGGER_ROE` | -0.15 | Add a -15% ROE, max 2 livelli, ≥30 min tra add |
| `BASELINE_INTERVAL_SECONDS` | 10800 | Rete di sicurezza: libro completo ogni 3h |
| `SCANNER_INTERVAL_SECONDS` | 300 | Screening locale gratuito ogni 5 min |
| `SIGNAL_DEBOUNCE_SECONDS` | 3600 | Non ripete lo stesso segnale su un simbolo entro 1h |
| `EVENT_MAX_CALLS_PER_HOUR` | 8 | Token bucket chiamate-evento a Claude |
| `NEWS_TRIGGER_ENABLED` | True | Notizie CryptoPanic (serve CRYPTOPANIC_TOKEN nel .env) |
| `EQUITY_FLOOR_PCT` | 0.20 | Auto-halt (soft) sotto il 20% del capitale |

## Checklist mainnet (quando e se)

1. `USE_TESTNET=False` in `config.py` — gli ordini protettivi server-side (STOP_MARKET/TAKE_PROFIT_MARKET, ora con parametri per-trade) si **riattivano automaticamente** come backstop dietro il RiskEngine.
2. Chiavi mainnet in `.env` (permessi futures, **niente** withdrawal).
3. Verifica i bracket di leva reali (`execution.get_max_leverage`) — su mainnet variano per simbolo.
4. Il liq-guard diventa esatto (prezzo di liquidazione dell'exchange, stessi prezzi del feed).
5. Riduci il capitale/POSITION_MARGIN_PCT per la prima fase e osserva 48h con leve basse.
6. Ricontrolla i rate limit (l'engine fa una chiamata REST per chiusura, il ciclo ~40 chiamate features).

## Safety rail

- KILL_SWITCH soft/hard (vedi sopra) + equity floor 20% (halt soft automatico).
- Cap esposizione totale, clamp leva al bracket, clamp SL, guardia pre-liquidazione al 75%.
- Watchdog: stream morto → fallback REST → restart → dopo 2 fallimenti exit(1) e Railway riavvia (policy `ON_FAILURE`).
