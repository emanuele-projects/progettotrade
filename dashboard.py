"""Streamlit dashboard — fund-style report: money, exposure, positions, Claude's brain.

Run:
  streamlit run dashboard.py

Opens at http://localhost:8501
"""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from config import CFG, STRATEGY_ALLOCATIONS, TOTAL_CAPITAL_USDT
import execution
import journal
import performance


REFRESH_SECONDS = 30

# Claude API pricing (USD per 1M tokens) for the cost panel. The journal stores
# uncached input + output tokens; cache traffic isn't stored, so we add a small
# flat adder per call as a declared approximation.
_MODEL_PRICES = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}
_CACHE_ADDER_PER_CALL = 0.004  # ~6k cache-read tokens/call — declared estimate


st.set_page_config(
    page_title="Trading Bot",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ============================================================================
# Login gate — single shared password from env (DASHBOARD_PASSWORD).
# ============================================================================
def _login_gate() -> None:
    expected = (os.getenv("DASHBOARD_PASSWORD") or "").strip()
    if not expected:
        st.error(
            "🔒 **Dashboard non configurata.**  \n"
            "Imposta la variabile d'ambiente `DASHBOARD_PASSWORD` sul server."
        )
        st.stop()
    if st.session_state.get("authenticated") is True:
        return
    st.markdown("# 🔒 Login")
    st.caption("Inserisci la password per accedere alla dashboard.")
    with st.form("login_form", clear_on_submit=True):
        pw = st.text_input("Password", type="password", label_visibility="collapsed",
                           placeholder="Password")
        submitted = st.form_submit_button("Accedi", type="primary")
        if submitted:
            if pw == expected:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Password sbagliata.")
    st.stop()


_login_gate()
st_autorefresh(interval=REFRESH_SECONDS * 1000, key="auto_refresh_tick")

st.markdown(
    """
    <style>
    .main .block-container { padding-top: 2rem; max-width: 1400px; }
    h1 { font-weight: 600; letter-spacing: -0.02em; }
    h3 { font-weight: 600; margin-top: 1.5rem; }
    [data-testid="stMetricValue"] { font-weight: 600; }
    [data-testid="stMetricLabel"] { font-weight: 400; opacity: 0.75; }
    .stDataFrame { border: 1px solid #e5e5e7; border-radius: 8px; }
    div[data-testid="stExpander"] details { border-radius: 8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

title_col, logout_col = st.columns([8, 1])
with title_col:
    st.title("Trading Bot")
    st.caption(
        f"Paper trading (testnet) · Auto-refresh {REFRESH_SECONDS}s · "
        f"Portafoglio sempre investito · Claude {CFG.CLAUDE_MODEL} · long/short · SL/TP sull'exchange"
    )
with logout_col:
    st.markdown("<div style='height: 1.5rem'></div>", unsafe_allow_html=True)
    if st.button("Logout", width='stretch'):
        st.session_state["authenticated"] = False
        st.rerun()


# ============================================================================
# Data loaders
# ============================================================================
@st.cache_data(ttl=REFRESH_SECONDS)
def load_live_state() -> dict[str, Any]:
    client = execution.make_client()
    account = execution.get_account(client)
    positions = [p.__dict__ for p in execution.get_open_positions(client)]
    # Exchange-held protective orders (SL/TP live in the algo subsystem)
    protection: dict[str, dict[str, float]] = {}
    try:
        for o in client.futures_get_open_orders(conditional=True):
            sym = o.get("symbol", "")
            typ = o.get("orderType") or o.get("type") or ""
            trig = float(o.get("triggerPrice") or o.get("stopPrice") or 0)
            protection.setdefault(sym, {})[typ] = trig
    except Exception:
        pass
    return {"account": account, "positions": positions, "protection": protection}


@st.cache_data(ttl=60)
def load_realized_history() -> pd.DataFrame:
    """Realized-P&L events from Binance income history (source of truth that
    survives journal resets — the same data Claude's self-correction reads)."""
    try:
        client = execution.make_client()
        rows = client.futures_income_history(incomeType="REALIZED_PNL", limit=1000)
    except Exception:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([
        {"ts": pd.to_datetime(int(r["time"]), unit="ms", utc=True),
         "symbol": r.get("symbol", ""), "pnl": float(r["income"])}
        for r in rows
    ]).sort_values("ts")
    return df


@st.cache_data(ttl=60)
def load_perf_review_text() -> str | None:
    """The exact self-correction block injected into Claude's prompt."""
    try:
        client = execution.make_client()
        return performance.build_performance_review(client)
    except Exception:
        return None


@st.cache_data(ttl=REFRESH_SECONDS)
def load_journal() -> dict[str, pd.DataFrame]:
    if not Path(CFG.JOURNAL_DB).exists():
        return {"equity": pd.DataFrame(), "trades": pd.DataFrame(),
                "decisions": pd.DataFrame(), "opens": pd.DataFrame(),
                "calls": pd.DataFrame(), "system_events": pd.DataFrame()}
    with sqlite3.connect(CFG.JOURNAL_DB) as c:
        equity = pd.read_sql_query(
            "SELECT ts, total_equity, source FROM equity ORDER BY ts ASC",
            c, parse_dates=["ts"],
        )
        trades = pd.read_sql_query(
            "SELECT ts, symbol, side, qty, price, notional_usdt, kind, note, trigger "
            "FROM trades ORDER BY id DESC LIMIT 40",
            c, parse_dates=["ts"],
        )
        # Latest 'open' row per symbol: entry timestamp + what woke Claude up
        opens = pd.read_sql_query(
            "SELECT symbol, MAX(ts) AS opened_ts, trigger FROM trades "
            "WHERE kind='open' GROUP BY symbol",
            c, parse_dates=["opened_ts"],
        )
        decisions = pd.read_sql_query(
            "SELECT ts, market_view, decisions_json, trigger, model, "
            "input_tokens, output_tokens FROM decisions "
            "ORDER BY id DESC LIMIT 5",
            c, parse_dates=["ts"],
        )
        calls = pd.read_sql_query(
            "SELECT ts, trigger, model, input_tokens, output_tokens "
            "FROM decisions ORDER BY ts ASC",
            c, parse_dates=["ts"],
        )
        system_events = pd.read_sql_query(
            "SELECT ts, level, msg FROM events WHERE level IN "
            "('WS_STALE','FATAL','HALT','KILL_SOFT','RESUME','TRIGGER_DROPPED',"
            "'RISK_BACKSTOP','ERROR','NOTIONAL_CAP','WARN','SERVER_EXIT_CLEANUP') "
            "ORDER BY id DESC LIMIT 12",
            c, parse_dates=["ts"],
        )
    return {"equity": equity, "trades": trades, "decisions": decisions,
            "opens": opens, "calls": calls, "system_events": system_events}


def load_operator_notes() -> list[dict[str, Any]]:
    return journal.get_active_operator_notes()


# ============================================================================
# Load everything
# ============================================================================
try:
    live = load_live_state()
except Exception as e:
    st.error(f"Non riesco a contattare Binance: {e}")
    st.stop()

account = live["account"]
positions = live["positions"]
protection = live["protection"]
journal_data = load_journal()
eq_df = journal_data["equity"]
realized_df = load_realized_history()

initial_capital = float(TOTAL_CAPITAL_USDT)          # baseline dal reset
equity_now = float(account["total_equity"])           # wallet + non realizzato
wallet_now = float(account["wallet_balance"])         # solo realizzato
unrealized = float(account["unrealized_pnl"])
available = float(account["available_balance"])

pnl_total = equity_now - initial_capital
pnl_total_pct = pnl_total / initial_capital * 100 if initial_capital else 0.0
pnl_realized = wallet_now - initial_capital           # incassato/perso davvero
deployed_margin = sum(p["isolated_margin"] for p in positions) if positions else 0.0
total_exposure = sum(p["qty"] * p["mark_price"] for p in positions) if positions else 0.0
avg_leverage = (total_exposure / deployed_margin) if deployed_margin else 0.0

long_exp = sum(p["qty"] * p["mark_price"] for p in positions if p["side"] == "LONG")
short_exp = sum(p["qty"] * p["mark_price"] for p in positions if p["side"] == "SHORT")
net_exp = long_exp - short_exp
n_long = sum(1 for p in positions if p["side"] == "LONG")
n_short = len(positions) - n_long


# ============================================================================
# SEZIONE 1 — I SOLDI (la domanda: quanto ho messo, quanto vale, quanto ho fatto)
# ============================================================================
st.markdown("### 💰 I soldi")
st.caption(
    f"Capitale iniziale = saldo reale al reset del 15/07/2026. "
    f"P&L totale = valore di adesso − capitale iniziale, scomposto in **realizzato** "
    f"(trade già chiusi, soldi veri in cassa) e **sulla carta** (posizioni ancora aperte)."
)
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Capitale iniziale", f"${initial_capital:,.2f}",
          help="Baseline del 15/07/2026 (reset pulito, posizioni azzerate). Ogni P&L è misurato da qui.")
c2.metric("Valore conto ORA", f"${equity_now:,.2f}",
          f"{pnl_total_pct:+.2f}% dall'inizio",
          help="Equity live: cassa + P&L delle posizioni aperte ai prezzi attuali. Si aggiorna ogni 30s.")
c3.metric("P&L totale", f"${pnl_total:+,.2f}",
          help="Valore ora − capitale iniziale. È la somma delle due colonne a destra.")
c4.metric("→ di cui realizzato", f"${pnl_realized:+,.2f}",
          help="Risultato dei trade GIÀ CHIUSI (cassa − capitale iniziale). Questi soldi sono definitivi.")
c5.metric("→ di cui sulla carta", f"${unrealized:+,.2f}",
          help="P&L delle posizioni ancora aperte. Diventa realizzato solo alla chiusura (stop/target).")

st.divider()

# ============================================================================
# SEZIONE 2 — QUANTO È INVESTITO ADESSO
# ============================================================================
st.markdown("### 📦 Quanto è investito adesso")
st.caption(
    "**Margine impegnato** = soldi tuoi bloccati a garanzia delle posizioni. "
    "**Esposizione** = dimensione vera delle scommesse (margine × leva). "
    "**Bilancia long/short** = da che parte pende il portafoglio."
)
d1, d2, d3, d4, d5 = st.columns(5)
d1.metric("Margine impegnato", f"${deployed_margin:,.0f}",
          f"{deployed_margin / equity_now * 100:.0f}% del conto" if equity_now else "—",
          help="Somma dei margini isolati delle posizioni aperte.")
d2.metric("Disponibile", f"${available:,.0f}",
          help="Liquidità non impegnata, pronta per nuove posizioni.")
d3.metric("Esposizione totale", f"${total_exposure:,.0f}",
          f"leva media {avg_leverage:.1f}x",
          help="Valore di mercato controllato = margine × leva, sommato su tutte le posizioni.")
d4.metric("Long vs Short", f"{n_long}L / {n_short}S",
          f"${long_exp:,.0f} vs ${short_exp:,.0f}",
          help="Numero di posizioni e esposizione per lato.")
bias_pct = (net_exp / total_exposure * 100) if total_exposure else 0.0
d5.metric("Esposizione netta", f"${net_exp:+,.0f}",
          f"{bias_pct:+.0f}% {'long' if net_exp >= 0 else 'short'} bias",
          help="Long − short: quanto il portafoglio guadagna/perde se TUTTO il mercato si muove insieme. Vicino a zero = market-neutral.")

st.divider()

# ============================================================================
# SEZIONE 3 — PORTAFOGLIO (posizioni arricchite)
# ============================================================================
st.markdown(f"### 📊 Portafoglio — {len(positions)} posizioni "
            f"(mandato: min {CFG.MIN_OPEN_POSITIONS} · target {CFG.TARGET_OPEN_POSITIONS})")
st.caption(
    "**Cuscino SL** = quanto ROE può ancora perdere prima dello stop. **Manca al TP** = quanto ROE manca al target. "
    "**Protetta** = coppia stop-loss + take-profit depositata sull'exchange (scatta anche a bot spento)."
)

opens_df = journal_data["opens"]
opens_map = {}
if not opens_df.empty:
    for _, r in opens_df.iterrows():
        opens_map[r["symbol"]] = (r["opened_ts"], r.get("trigger"))

if positions:
    now_utc = pd.Timestamp.now(tz="UTC")
    rows = []
    for p in positions:
        sl_pct, tp_pct = journal.get_position_targets(p["symbol"])
        roe = p["unrealized_pnl_pct"]
        prot = protection.get(p["symbol"], {})
        protected = "STOP_MARKET" in prot and "TAKE_PROFIT_MARKET" in prot
        opened_ts, trig = opens_map.get(p["symbol"], (None, None))
        age_h = ((now_utc - opened_ts).total_seconds() / 3600) if opened_ts is not None else None
        trig_label = ""
        if isinstance(trig, str) and trig:
            trig_label = "⏰ ciclo" if trig == "baseline" else f"⚡ {trig.replace('event:', '')}"
        rows.append({
            "Crypto": p["symbol"].replace("USDT", ""),
            "Direzione": "🟢 Long" if p["side"] == "LONG" else "🔴 Short",
            "Leva": f"{p['leverage']}x",
            "Margine": p["isolated_margin"],
            "% del book": (p["isolated_margin"] / deployed_margin) if deployed_margin else 0.0,
            "Esposizione": p["qty"] * p["mark_price"],
            "Apertura": p["entry_price"],
            "Ora": p["mark_price"],
            "P&L $": p["unrealized_pnl"],
            "ROE %": roe,
            "SL": sl_pct,
            "TP": tp_pct,
            "Cuscino SL": roe - sl_pct,
            "Manca al TP": tp_pct - roe,
            "Protetta": "✅" if protected else "⚠️ engine",
            "Età (h)": age_h,
            "Aperta da": trig_label,
            "Grafico": f"https://www.binance.com/en/futures/{p['symbol']}",
        })
    rows.sort(key=lambda r: -abs(r["P&L $"]))
    st.dataframe(
        pd.DataFrame(rows), width='stretch', hide_index=True,
        column_config={
            "Margine": st.column_config.NumberColumn(format="$%.0f", help="Soldi tuoi in pegno su questa posizione."),
            "% del book": st.column_config.NumberColumn(format="percent", help="Peso della posizione sul margine totale impegnato."),
            "Esposizione": st.column_config.NumberColumn(format="$%.0f", help="Margine × leva."),
            "Apertura": st.column_config.NumberColumn(format="$%.4f"),
            "Ora": st.column_config.NumberColumn(format="$%.4f"),
            "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
            "ROE %": st.column_config.NumberColumn(format="percent", help="P&L in % del margine (return on equity della posizione)."),
            "SL": st.column_config.NumberColumn(format="percent", help="Stop-loss (ROE) deciso da Claude — ordine reale sull'exchange."),
            "TP": st.column_config.NumberColumn(format="percent", help="Take-profit (ROE) deciso da Claude — ordine reale sull'exchange."),
            "Cuscino SL": st.column_config.NumberColumn(format="percent", help="ROE attuale − SL: quanto può ancora scendere prima dello stop. Piccolo = vicina allo stop."),
            "Manca al TP": st.column_config.NumberColumn(format="percent", help="TP − ROE attuale: quanto manca al target."),
            "Protetta": st.column_config.TextColumn(help="✅ = SL+TP depositati sull'exchange. ⚠️ = protezione solo dal risk engine locale."),
            "Età (h)": st.column_config.NumberColumn(format="%.1f", help="Ore dall'apertura."),
            "Aperta da": st.column_config.TextColumn(help="Cosa ha svegliato Claude: ⏰ ciclo periodico o ⚡ evento (segnale/stop scattato)."),
            "Grafico": st.column_config.LinkColumn(display_text="📈"),
        },
    )
    n_protected = sum(1 for r in rows if r["Protetta"] == "✅")
    if n_protected == len(rows):
        st.success(f"🛡️ Tutte le {len(rows)} posizioni hanno stop-loss e take-profit reali depositati sull'exchange.")
    else:
        st.warning(f"🛡️ {n_protected}/{len(rows)} posizioni con ordini exchange — le altre sono protette dal risk engine locale (fallback).")
else:
    st.info("Nessuna posizione aperta — il mandato sempre-investito le riaprirà al prossimo ciclo.")

st.divider()

# ============================================================================
# SEZIONE 4 — LA TESTA DI CLAUDE (esperienza, auto-correzione, decisioni)
# ============================================================================
st.markdown("### 🧠 La testa di Claude")

# --- 4a. Track record (la stessa fotografia che Claude legge di sé stesso) ---
st.markdown("**Il suo track record (ultimi 7 giorni)** — questi numeri vengono iniettati nel prompt: Claude li legge e adatta la strategia.")
if not realized_df.empty:
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)
    week = realized_df[realized_df["ts"] >= cutoff]
    if not week.empty:
        wins = week[week["pnl"] > 0]
        losses = week[week["pnl"] < 0]
        n = len(week)
        wr = len(wins) / n * 100 if n else 0.0
        profit_factor = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if len(losses) and losses["pnl"].sum() != 0 else float("inf")
        last10 = week.tail(10)
        wr10 = (last10["pnl"] > 0).mean() * 100 if len(last10) else 0.0
        e1, e2, e3, e4, e5, e6 = st.columns(6)
        e1.metric("Trade chiusi (7g)", f"{n}")
        e2.metric("Win-rate", f"{wr:.0f}%",
                  help="Percentuale di trade chiusi in profitto. Sotto 45% la guidance impone di ridurre il rischio.")
        e3.metric("Win-rate ultimi 10", f"{wr10:.0f}%",
                  help="Trend recente: sta migliorando o peggiorando?")
        e4.metric("Netto realizzato 7g", f"${week['pnl'].sum():+,.2f}")
        e5.metric("Media win / loss", f"+{wins['pnl'].mean():.0f} / {losses['pnl'].mean():.0f}" if len(wins) and len(losses) else "—",
                  help="Vincita media vs perdita media in $. Vincite più grandi delle perdite compensano un win-rate basso.")
        pf_str = "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}"
        e6.metric("Profit factor", pf_str,
                  help="Somma vincite ÷ somma perdite. Sopra 1.0 = strategia in utile.")
    else:
        st.info("Nessun trade chiuso negli ultimi 7 giorni.")
else:
    st.info("Storico P&L non disponibile (conto appena resettato o API non raggiungibile).")

perf_text = load_perf_review_text()
if perf_text:
    with st.expander("📋 Il blocco di auto-correzione esattamente come lo legge Claude"):
        st.code(perf_text, language=None)

# --- 4b. Ultima vista macro + decisioni per crypto ---
dec = journal_data["decisions"]
if not dec.empty:
    latest = dec.iloc[0]
    st.markdown("**L'ultima lettura del mercato** (market view del ciclo più recente):")
    st.markdown(
        f"<div style='border-left:3px solid #7c4dff;padding:8px 14px;margin:4px 0 14px 0;"
        f"background:#f7f5ff;border-radius:4px'>"
        f"<span style='font-size:0.8rem;color:#86868b'>{latest['ts']:%d/%m %H:%M} UTC · "
        f"{'⏰ ciclo periodico' if latest['trigger'] == 'baseline' else '⚡ ' + str(latest['trigger'])}</span><br/>"
        f"{latest['market_view']}</div>",
        unsafe_allow_html=True,
    )

    st.markdown("**Le decisioni, crypto per crypto** — azione, stop, leva e il ragionamento completo:")
    action_emoji = {"long": "🟢", "short": "🔻", "flat": "⚪", "close": "🚪"}
    action_label = {"long": "Apri LONG", "short": "Apri SHORT", "flat": "Nessuna azione", "close": "Chiudi posizione"}
    for _, row in dec.iterrows():
        try:
            items = json.loads(row["decisions_json"])
        except Exception:
            items = []
        n_long_d = sum(1 for d in items if d.get("action") == "long")
        n_short_d = sum(1 for d in items if d.get("action") == "short")
        n_close_d = sum(1 for d in items if d.get("action") == "close")
        trig = row["trigger"]
        trig_label = "" if pd.isna(trig) or not trig else (
            " · ⏰ ciclo" if trig == "baseline" else f" · ⚡ {trig}"
        )
        header = (
            f"{row['ts']:%d/%m %H:%M UTC} · {len(items)} valutate · "
            f"🟢 {n_long_d} long · 🔻 {n_short_d} short · 🚪 {n_close_d} chiusure{trig_label}"
        )
        with st.expander(header):
            st.markdown(f"**Vista macro:** {row['market_view']}")
            st.markdown("---")
            for d in items:
                symbol = d.get("symbol", "")
                action = d.get("action", "")
                conf = float(d.get("confidence", 0))
                reasoning = d.get("reasoning", "")
                clean_sym = symbol.replace("USDT", "")
                emoji = action_emoji.get(action, "•")
                label = action_label.get(action, action)
                params = ""
                if action in ("long", "short") and d.get("stop_loss_pct") is not None:
                    params = (f" · leva **{d.get('leverage')}x** · "
                              f"SL **{d.get('stop_loss_pct', 0):+.0%}** · "
                              f"TP **{d.get('take_profit_pct', 0):+.0%}**")
                chart_main = f"https://www.binance.com/en/futures/{symbol}"
                tv_link = f"https://www.tradingview.com/symbols/{symbol}.P/?exchange=BINANCE"
                st.markdown(
                    f"**{emoji} {clean_sym}** — {label} · fiducia **{conf:.0%}**{params}  \n"
                    f"<span style='color:#3a3a3c'>{reasoning}</span>  \n"
                    f"<span style='font-size:0.85rem'>"
                    f"📈 <a href='{chart_main}' target='_blank'>Binance</a> · "
                    f"<a href='{tv_link}' target='_blank'>TradingView</a></span>",
                    unsafe_allow_html=True,
                )
                st.markdown("")
else:
    st.info("Nessuna decisione registrata ancora.")

st.divider()

# ============================================================================
# SEZIONE 5 — ANDAMENTO
# ============================================================================
st.markdown("### 📈 Andamento")
g1, g2 = st.columns(2)

with g1:
    st.markdown("**Equity nel tempo** (% dal capitale iniziale, un punto per ciclo)")
    if not eq_df.empty:
        eq_live = eq_df[eq_df["source"] == "live"]
        if not eq_live.empty:
            eq_live = eq_live.set_index("ts")
            pct_change = ((eq_live[["total_equity"]] / initial_capital) - 1) * 100
            pct_change = pct_change.rename(columns={"total_equity": "Equity %"})
            st.line_chart(pct_change, height=300)
        else:
            st.info("Nessun dato equity ancora.")
    else:
        st.info("Nessun dato equity ancora.")

with g2:
    st.markdown("**P&L realizzato cumulato** ($, trade chiusi — solo soldi veri)")
    if not realized_df.empty:
        cum = realized_df.set_index("ts")[["pnl"]].cumsum()
        cum = cum.rename(columns={"pnl": "Realizzato cumulato $"})
        st.line_chart(cum, height=300)
    else:
        st.info("Nessun trade chiuso ancora.")

st.divider()

# ============================================================================
# SEZIONE 6 — OPERAZIONI RECENTI (con esito)
# ============================================================================
st.markdown("### 🧾 Operazioni recenti")
st.caption("🎯/🛑 = chiusa dall'ordine exchange (take-profit/stop-loss). Il ROE nella nota è il risultato % sul margine al momento della chiusura.")
tr = journal_data["trades"]
if tr.empty:
    st.info("Nessuna operazione ancora.")
else:
    kind_map = {
        "open": "🟢 Apertura",
        "martingale_add": "➕ Martingale",
        "tp": "🎯 Take Profit",
        "sl": "🛑 Stop Loss",
        "liq_guard": "⛔ Pre-liquidazione",
        "manual_close": "🚪 Chiusura decisa da Claude",
        "server_exit_cleanup": "🧹 Pulizia ordini",
    }
    tr_display = tr.copy()
    tr_display["Tipo"] = tr_display["kind"].map(kind_map).fillna(tr_display["kind"])
    tr_display["Crypto"] = tr_display["symbol"].str.replace("USDT", "")
    tr_display["Direzione"] = tr_display["side"].map({"LONG": "🟢 Long", "SHORT": "🔴 Short"}).fillna(tr_display["side"])
    trig_col = tr_display["trigger"].fillna("")
    tr_display["Da"] = trig_col.map(lambda t: "" if not t else ("⏰ ciclo" if t == "baseline" else f"⚡ {str(t).replace('event:', '').replace('risk:', '')}"))
    tr_display = tr_display[["ts", "Crypto", "Direzione", "Tipo", "Da", "qty", "price", "notional_usdt", "note"]]
    tr_display.columns = ["Quando", "Crypto", "Direzione", "Tipo", "Da", "Quantità", "Prezzo", "Valore", "Nota"]
    st.dataframe(
        tr_display, width='stretch', hide_index=True,
        column_config={
            "Quantità": st.column_config.NumberColumn(format="%.4f"),
            "Prezzo": st.column_config.NumberColumn(format="$%.4f"),
            "Valore": st.column_config.NumberColumn(format="$%.2f"),
            "Nota": st.column_config.TextColumn(help="Per le chiusure: ROE al momento dell'uscita."),
        },
    )

st.divider()

# ============================================================================
# SEZIONE 7 — COSTI DEL CERVELLO (chiamate Claude)
# ============================================================================
st.markdown("### 💸 Costi del cervello")
st.caption(
    "Ogni decisione è una chiamata all'API Anthropic. Stima calcolata dai token registrati "
    f"(prezzi {CFG.CLAUDE_MODEL}: ${_MODEL_PRICES.get(CFG.CLAUDE_MODEL, (5, 25))[0]}/M input, "
    f"${_MODEL_PRICES.get(CFG.CLAUDE_MODEL, (5, 25))[1]}/M output) + piccolo forfait cache. Approssimata per difetto."
)
calls = journal_data["calls"]
if not calls.empty:
    calls = calls.copy()

    def _row_cost(r):
        p_in, p_out = _MODEL_PRICES.get(str(r.get("model") or ""), (5.00, 25.00))
        return ((r.get("input_tokens") or 0) * p_in + (r.get("output_tokens") or 0) * p_out) / 1e6 + _CACHE_ADDER_PER_CALL

    calls["cost"] = calls.apply(_row_cost, axis=1)
    calls["day"] = calls["ts"].dt.date
    today = datetime.now(timezone.utc).date()
    today_calls = calls[calls["day"] == today]
    f1, f2, f3, f4 = st.columns(4)
    f1.metric("Chiamate oggi", f"{len(today_calls)}",
              help="Cicli periodici + chiamate a evento (cap 4/ora).")
    f2.metric("Costo stimato oggi", f"${today_calls['cost'].sum():.2f}")
    f3.metric("Costo totale registrato", f"${calls['cost'].sum():.2f}",
              f"{len(calls)} chiamate totali")
    f4.metric("Costo medio/chiamata", f"${calls['cost'].mean():.3f}",
              help="La cache del prompt tiene basso il costo: le regole di strategia si pagano una volta e si riusano per un'ora.")
    daily = calls.groupby("day").agg(costo=("cost", "sum")).tail(14)
    daily.index = pd.to_datetime(daily.index)
    st.bar_chart(daily, height=200)
else:
    st.info("Nessuna chiamata registrata ancora.")

st.divider()

# ============================================================================
# Operator notes
# ============================================================================
st.markdown("### 📝 Note operatore (input manuale per Claude)")
st.caption(
    "Tutto quello che inserisci qui viene letto da Claude al prossimo ciclo come contesto ad ALTA priorità. "
    "Usalo per news, eventi macro, rumor, catalizzatori specifici."
)
note_col1, note_col2 = st.columns([3, 1])
with note_col1:
    new_note_text = st.text_area(
        "Nuova nota",
        placeholder="es: 'Powell parla mercoledì 14:00 ET, attesi toni hawkish'",
        key="new_note_text", height=80,
    )
with note_col2:
    note_symbol = st.text_input("Simbolo (opzionale)", placeholder="es: BTCUSDT — vuoto = globale",
                                key="new_note_symbol").strip().upper()
    note_hours = st.number_input("Scade in (ore)", min_value=1, max_value=720, value=48, step=1,
                                 key="new_note_hours")
    if st.button("Aggiungi nota", type="primary", width='stretch'):
        if new_note_text.strip():
            journal.add_operator_note(new_note_text.strip(), symbol=(note_symbol or None),
                                      expires_hours=float(note_hours))
            st.success("Nota salvata. Verrà passata a Claude al prossimo ciclo.")
            st.rerun()
        else:
            st.warning("Scrivi qualcosa prima di salvare.")

active_notes = load_operator_notes()
if active_notes:
    st.markdown(f"**Note attive ({len(active_notes)})**")
    for n in active_notes:
        target = n.get("symbol") or "Globale"
        expires = n.get("expires_at")
        expires_str = f" · scade {expires[:16]}" if expires else ""
        cols = st.columns([6, 1])
        cols[0].markdown(
            f"<div style='border-left:3px solid #0066ff;padding:6px 12px;margin:6px 0;"
            f"background:#f5f5f7;border-radius:4px'>"
            f"<span style='font-size:0.8rem;color:#86868b'>{n['ts'][:16]} · {target}{expires_str}</span><br/>"
            f"{n['note']}</div>",
            unsafe_allow_html=True,
        )
        if cols[1].button("Disattiva", key=f"deactivate_{n['id']}"):
            journal.deactivate_operator_note(n["id"])
            st.rerun()

st.divider()

# ============================================================================
# Salute sistema + glossario
# ============================================================================
with st.expander("🩺 Salute sistema — eventi stream / risk engine"):
    sys_ev = journal_data.get("system_events")
    if sys_ev is None or sys_ev.empty:
        st.info("Nessun evento di sistema recente. Tutto regolare.")
    else:
        level_map = {
            "WS_STALE": "📡 Stream stantio", "FATAL": "💀 Fatale", "HALT": "🛑 Halt",
            "KILL_SOFT": "⏸️ Pausa (soft kill)", "RESUME": "▶️ Ripresa",
            "TRIGGER_DROPPED": "🔇 Trigger scartati", "RISK_BACKSTOP": "🚨 Backstop ciclo",
            "ERROR": "❌ Errore", "NOTIONAL_CAP": "🧢 Cap esposizione", "WARN": "⚠️ Warning",
            "SERVER_EXIT_CLEANUP": "🧹 Pulizia ordini",
        }
        ev_display = sys_ev.copy()
        ev_display["Tipo"] = ev_display["level"].map(level_map).fillna(ev_display["level"])
        ev_display = ev_display[["ts", "Tipo", "msg"]]
        ev_display.columns = ["Quando", "Tipo", "Dettaglio"]
        st.dataframe(ev_display, width='stretch', hide_index=True)

with st.expander("📖 Glossario — significato di ogni termine"):
    st.markdown(
        """
**Capitale iniziale** — Il saldo reale del conto al reset del 15/07/2026 ($3.912,89). Tutti i P&L sono misurati da questa baseline.

**Equity (Valore conto)** — Quanto vale il conto adesso: cassa + P&L sulla carta delle posizioni aperte.

**P&L realizzato** — Il risultato dei trade già chiusi. Soldi veri, definitivi.

**P&L sulla carta (non realizzato)** — Il guadagno/perdita delle posizioni ancora aperte ai prezzi attuali. Cambia di continuo; diventa realizzato alla chiusura.

**Margine** — I soldi tuoi "in pegno" per tenere aperta una posizione con leva. Con leva 10x, $100 di margine controllano $1.000 di crypto.

**Esposizione (Notional)** — Margine × leva: la dimensione vera della scommessa.

**Esposizione netta** — Long − short. Vicino a zero = market-neutral: il portafoglio non dipende dalla direzione generale del mercato ma dalle scelte relative (i long battono gli short).

**ROE** — Return on equity della posizione: P&L in % del margine. A leva 10x, un movimento di prezzo dell'1% = ROE del 10%.

**Stop Loss (SL) / Take Profit (TP)** — Soglie di uscita in ROE decise da Claude per ogni trade e depositate come ORDINI REALI sull'exchange: scattano anche se il bot fosse spento.

**Cuscino SL / Manca al TP** — Distanza (in ROE) dall'uscita in perdita / dal target di profitto.

**Guardia pre-liquidazione** — Se il prezzo percorre il 75% della strada verso la liquidazione, il risk engine locale chiude d'ufficio.

**Portafoglio sempre investito** — Mandato: minimo 10 posizioni aperte, target 12. La prudenza si esprime con leva più bassa e stop più larghi, mai stando fuori dal mercato.

**Auto-correzione** — Prima di ogni ciclo Claude legge il proprio track record reale (win-rate, P&L) e una guidance calcolata: con risultati scarsi riduce leva e bilancia il book, con buoni risultati mantiene la disciplina.

**Trigger (⏰/⚡)** — Cosa ha attivato la decisione: il ciclo periodico (4h) o un evento (segnale tecnico, stop scattato, movimento improvviso).

**Profit factor** — Somma delle vincite ÷ somma delle perdite. Sopra 1.0 la strategia è in utile.

**Testnet** — Ambiente di test Binance, soldi finti, prezzi reali.
        """
    )

st.caption(
    f"Auto-refresh {REFRESH_SECONDS}s · Ciclo completo ogni {CFG.BASELINE_INTERVAL_SECONDS // 3600}h "
    f"+ chiamate a evento (max {CFG.EVENT_MAX_CALLS_PER_HOUR}/h) · "
    f"Modello {CFG.CLAUDE_MODEL} · DB: {CFG.JOURNAL_DB.name}"
)
