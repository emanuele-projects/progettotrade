"""Streamlit dashboard — multi-strategy, aggregate + granular, clean theme.

Run:
  streamlit run dashboard.py

Opens at http://localhost:8501
"""
from __future__ import annotations
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from config import CFG, STRATEGY_ALLOCATIONS, TOTAL_CAPITAL_USDT
import execution
import journal


_SHADOWS_ENABLED = any(
    STRATEGY_ALLOCATIONS.get(k, 0) > 0 for k in ("hodl", "dca", "conservative_2x")
)


REFRESH_SECONDS = 30


st.set_page_config(
    page_title="Trading Bot",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ============================================================================
# Login gate — single shared password from env (DASHBOARD_PASSWORD).
# If env unset, dashboard refuses to serve. If set, user must type the password
# once per browser session.
# ============================================================================
def _login_gate() -> None:
    expected = (os.getenv("DASHBOARD_PASSWORD") or "").strip()
    if not expected:
        st.error(
            "🔒 **Dashboard non configurata.**  \n"
            "Imposta la variabile d'ambiente `DASHBOARD_PASSWORD` sul server "
            "(su Railway: Variables del servizio) e fai un redeploy."
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
st.markdown(
    f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>",
    unsafe_allow_html=True,
)

# Subtle CSS polish: tighten spacing, soften card backgrounds
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
        f"Paper trading · Auto-refresh {REFRESH_SECONDS}s · "
        f"Capitale: ${TOTAL_CAPITAL_USDT:,.0f} su strategia Aggressive (Claude · 5x/10x · martingale)"
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
    return {"account": account, "positions": positions}


@st.cache_data(ttl=REFRESH_SECONDS)
def load_journal() -> dict[str, pd.DataFrame]:
    if not Path(CFG.JOURNAL_DB).exists():
        return {"equity": pd.DataFrame(), "trades": pd.DataFrame(), "decisions": pd.DataFrame()}
    with sqlite3.connect(CFG.JOURNAL_DB) as c:
        equity = pd.read_sql_query(
            "SELECT ts, total_equity, source FROM equity ORDER BY ts ASC",
            c, parse_dates=["ts"],
        )
        trades = pd.read_sql_query(
            "SELECT ts, symbol, side, qty, price, notional_usdt, kind, note "
            "FROM trades ORDER BY id DESC LIMIT 30",
            c, parse_dates=["ts"],
        )
        decisions = pd.read_sql_query(
            "SELECT ts, market_view, decisions_json FROM decisions "
            "ORDER BY id DESC LIMIT 5",
            c, parse_dates=["ts"],
        )
    return {"equity": equity, "trades": trades, "decisions": decisions}


@st.cache_data(ttl=REFRESH_SECONDS)
def load_shadow_breakdowns() -> dict[str, list[dict[str, Any]]]:
    import shadow
    return shadow.get_all_breakdowns()


def load_operator_notes() -> list[dict[str, Any]]:
    return journal.get_active_operator_notes()


# ============================================================================
# Live + journal + shadow
# ============================================================================
try:
    live = load_live_state()
except Exception as e:
    st.error(f"Non riesco a contattare Binance: {e}")
    st.stop()

account = live["account"]
positions = live["positions"]
journal_data = load_journal()
eq_df = journal_data["equity"]

if _SHADOWS_ENABLED:
    try:
        shadow_breakdowns = load_shadow_breakdowns()
    except Exception as e:
        st.error(f"Errore nel caricare le strategie shadow: {e}")
        shadow_breakdowns = {"hodl": [], "dca": [], "conservative_2x": []}
else:
    shadow_breakdowns = {"hodl": [], "dca": [], "conservative_2x": []}


# ----------------------------------------------------------------------------
# Aggressive value
# ----------------------------------------------------------------------------
agg_alloc = STRATEGY_ALLOCATIONS["aggressive"]
s = eq_df[eq_df["source"] == "live"] if not eq_df.empty else eq_df
if s.empty:
    agg_value = agg_alloc
    agg_pct = 0.0
else:
    first = float(s.iloc[0]["total_equity"])
    last = float(s.iloc[-1]["total_equity"])
    agg_pct = (last - first) / first if first else 0.0
    agg_value = agg_alloc * (1 + agg_pct)

total_value = agg_value
total_pnl = total_value - TOTAL_CAPITAL_USDT
total_pct = total_pnl / TOTAL_CAPITAL_USDT * 100 if TOTAL_CAPITAL_USDT else 0.0

deployed_margin = sum(p["isolated_margin"] for p in positions) if positions else 0.0
total_exposure = sum(p["qty"] * p["mark_price"] for p in positions) if positions else 0.0


# ============================================================================
# AGGREGATE — top hero
# ============================================================================
st.markdown("### Vista d'insieme")

c1, c2, c3, c4 = st.columns([1.3, 1, 1, 1])
c1.metric(
    "Valore totale ora",
    f"${total_value:,.2f}",
    f"{total_pct:+.2f}% vs ${TOTAL_CAPITAL_USDT:,.0f} iniziali",
    help="Equity attuale della strategia aggressive (testnet).",
)
c2.metric(
    "Profitto/Perdita",
    f"${total_pnl:+,.2f}",
    help="Differenza tra valore attuale e capitale iniziale.",
)
c3.metric(
    "Posizioni aperte",
    f"{len(positions)} / {CFG.MAX_CONCURRENT_POSITIONS}",
    help=f"Massimo {CFG.MAX_CONCURRENT_POSITIONS} posizioni simultanee, ${agg_alloc * CFG.POSITION_MARGIN_PCT:,.0f} di margine ognuna.",
)
c4.metric(
    "Esposizione live",
    f"${total_exposure:,.0f}",
    f"margine usato ${deployed_margin:,.0f}",
    help="Somma del valore notional di tutte le posizioni aperte (margine × leva 10x).",
)

st.divider()


# ============================================================================
# Strategia: caratteristiche e parametri
# ============================================================================
st.markdown("### Profilo strategia")
st.caption(
    f"Capitale ${agg_alloc:,.0f} · Leva 5x o 10x decisa da Claude · "
    f"Max {CFG.MAX_CONCURRENT_POSITIONS} posizioni · "
    f"Margine per entry ${agg_alloc * CFG.POSITION_MARGIN_PCT:,.0f} ({CFG.POSITION_MARGIN_PCT:.1%}) · "
    f"Initial deploy {CFG.INITIAL_DEPLOY_PCT:.0%} · "
    f"Reserve martingale {CFG.RESERVE_FOR_AVERAGING_PCT:.0%} · "
    f"SL/TP per posizione decisi da Claude (rotazione attiva) · "
    f"Universo {CFG.UNIVERSE_MAX_CANDIDATES} mid-cap + 5 large-cap ancore · "
    f"Multi-timeframe (1h/4h/1d) + ATR + flow futures"
)

st.divider()


# ============================================================================
# Operator notes — manual context the operator surfaces to Claude.
# ============================================================================
st.markdown("### Note operatore (input manuale per Claude)")
st.caption(
    "Tutto quello che inserisci qui viene letto da Claude al prossimo ciclo come contesto ad ALTA priorità. "
    "Usalo per news non in CryptoPanic, eventi macro, rumor, catalizzatori specifici."
)

note_col1, note_col2 = st.columns([3, 1])
with note_col1:
    new_note_text = st.text_area(
        "Nuova nota",
        placeholder="es: 'Powell parla mercoledì 14:00 ET, attesi toni hawkish' o 'rumor su SOL hack, attendere conferme'",
        key="new_note_text",
        height=80,
    )
with note_col2:
    note_symbol = st.text_input(
        "Simbolo (opzionale)",
        placeholder="es: BTCUSDT — vuoto = nota globale",
        key="new_note_symbol",
    ).strip().upper()
    note_hours = st.number_input(
        "Scade in (ore)",
        min_value=1, max_value=720, value=48, step=1,
        help="Dopo N ore la nota viene ignorata automaticamente.",
        key="new_note_hours",
    )
    if st.button("Aggiungi nota", type="primary", width='stretch'):
        if new_note_text.strip():
            journal.add_operator_note(
                new_note_text.strip(),
                symbol=(note_symbol or None),
                expires_hours=float(note_hours),
            )
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
        expires_str = f" · scade {expires[:16]}" if expires else " · nessuna scadenza"
        cols = st.columns([6, 1])
        cols[0].markdown(
            f"<div style='border-left:3px solid #0066ff;padding:6px 12px;margin:6px 0;"
            f"background:#f5f5f7;border-radius:4px'>"
            f"<span style='font-size:0.8rem;color:#86868b'>{n['ts'][:16]} · {target}{expires_str}</span><br/>"
            f"{n['note']}"
            f"</div>",
            unsafe_allow_html=True,
        )
        if cols[1].button("Disattiva", key=f"deactivate_{n['id']}"):
            journal.deactivate_operator_note(n["id"])
            st.rerun()
else:
    st.info("Nessuna nota attiva. Aggiungine una sopra per dare contesto a Claude.")

st.divider()


# ============================================================================
# GRANULAR — aggressive only now
# ============================================================================
st.markdown("### Dettaglio posizioni live")


# --- AGGRESSIVE — live testnet positions (Claude-driven) ---
if positions:
    rows = []
    for p in positions:
        price_change_pct = (p["mark_price"] - p["entry_price"]) / p["entry_price"]
        sl_pct, tp_pct = journal.get_position_targets(p["symbol"])
        rows.append({
            "Crypto": p["symbol"].replace("USDT", ""),
            "Grafico": f"https://www.binance.com/en/futures/{p['symbol']}",
            "Leva": f"{p['leverage']}x",
            "Quantità": p["qty"],
            "Prezzo apertura": p["entry_price"],
            "Prezzo ora": p["mark_price"],
            "Variazione prezzo": price_change_pct,
            "Margine": p["isolated_margin"],
            "Esposizione": p["qty"] * p["mark_price"],
            "P&L $": p["unrealized_pnl"],
            "P&L %": p["unrealized_pnl_pct"],
            "SL": sl_pct,
            "TP": tp_pct,
            "Martingale": p["martingale_levels"],
        })
    st.dataframe(
        pd.DataFrame(rows), width='stretch', hide_index=True,
        column_config={
            "Grafico": st.column_config.LinkColumn(display_text="📈 Apri", help="Apre il grafico su Binance Futures."),
            "Leva": st.column_config.TextColumn(help="Leva scelta da Claude per questa posizione (5x o 10x)."),
            "Quantità": st.column_config.NumberColumn(format="%.4f"),
            "Prezzo apertura": st.column_config.NumberColumn(format="$%.4f"),
            "Prezzo ora": st.column_config.NumberColumn(format="$%.4f"),
            "Variazione prezzo": st.column_config.NumberColumn(format="percent"),
            "Margine": st.column_config.NumberColumn(format="$%.2f", help="Soldi tuoi in pegno."),
            "Esposizione": st.column_config.NumberColumn(format="$%.2f", help="Margine × leva 10x."),
            "P&L $": st.column_config.NumberColumn(format="$%+,.2f"),
            "P&L %": st.column_config.NumberColumn(format="percent",
                                                   help="Confronta con SL e TP ragionati da Claude (colonne accanto)."),
            "SL": st.column_config.NumberColumn(format="percent",
                                                 help="Stop loss specifico per questa posizione, deciso da Claude."),
            "TP": st.column_config.NumberColumn(format="percent",
                                                 help="Take profit specifico per questa posizione, deciso da Claude."),
            "Martingale": st.column_config.NumberColumn(help="Livelli usati su 3."),
        },
    )
else:
    st.info("Nessuna posizione aperta. Claude sta valutando i candidati al prossimo ciclo.")

st.divider()


# ============================================================================
# Equity curves chart
# ============================================================================
st.markdown("### Andamento nel tempo")
st.caption("Equity curve live della strategia Aggressive ($10.000 di partenza). Variazione percentuale.")

if not eq_df.empty:
    eq_live = eq_df[eq_df["source"] == "live"]
    if eq_live.empty:
        pct_change = pd.DataFrame()
    else:
        eq_live = eq_live.set_index("ts")
        first = float(eq_live.iloc[0]["total_equity"])
        pct_change = ((eq_live[["total_equity"]] / first) - 1) * 100
        pct_change = pct_change.rename(columns={"total_equity": "Aggressive"})
    st.line_chart(pct_change, width='stretch', height=380)
else:
    st.info("Nessun dato di equity ancora — aspetta il primo ciclo.")

st.divider()


# ============================================================================
# Decisions + trades (compact)
# ============================================================================
col_left, col_right = st.columns([1, 1])

with col_left:
    st.markdown("### Decisioni di Claude — dettaglio per crypto")
    st.caption("Una card per ogni crypto valutata: azione, confidence, motivazione completa, link al grafico.")
    dec = journal_data["decisions"]
    if dec.empty:
        st.info("Nessuna decisione ancora.")
    else:
        action_emoji = {"long": "🟢", "flat": "⚪", "close": "🔴"}
        action_label = {"long": "Apri LONG", "flat": "Sta fuori", "close": "Chiudi posizione"}
        for _, row in dec.iterrows():
            try:
                items = json.loads(row["decisions_json"])
            except Exception:
                items = []
            n_long = sum(1 for d in items if d.get("action") == "long")
            n_close = sum(1 for d in items if d.get("action") == "close")
            header = (
                f"{row['ts']:%d/%m %H:%M UTC} · {len(items)} crypto · "
                f"🟢 {n_long} long · 🔴 {n_close} close"
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
                    chart_main = f"https://www.binance.com/en/futures/{symbol}"
                    chart_test = f"https://testnet.binancefuture.com/en/futures/{symbol}"
                    tv_link = f"https://www.tradingview.com/symbols/{symbol}.P/?exchange=BINANCE"
                    emoji = action_emoji.get(action, "•")
                    label = action_label.get(action, action)
                    st.markdown(
                        f"**{emoji} {clean_sym}** — {label} · sicurezza **{conf:.0%}**  \n"
                        f"<span style='color:#3a3a3c'>{reasoning}</span>  \n"
                        f"<span style='font-size:0.85rem'>"
                        f"📈 <a href='{chart_main}' target='_blank'>Binance</a> · "
                        f"<a href='{chart_test}' target='_blank'>Testnet</a> · "
                        f"<a href='{tv_link}' target='_blank'>TradingView</a>"
                        f"</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown("")

with col_right:
    st.markdown("### Operazioni recenti")
    st.caption("Strategia Aggressive — ordini eseguiti sul testnet.")
    tr = journal_data["trades"]
    if tr.empty:
        st.info("Nessuna operazione ancora.")
    else:
        kind_map = {
            "open": "🟢 Apertura",
            "martingale_add": "➕ Martingale",
            "tp": "🎯 Take Profit",
            "sl": "🛑 Stop Loss",
            "manual_close": "🚪 Chiusura",
        }
        tr_display = tr.copy()
        tr_display["Tipo"] = tr_display["kind"].map(kind_map).fillna(tr_display["kind"])
        tr_display["Crypto"] = tr_display["symbol"].str.replace("USDT", "")
        tr_display = tr_display[["ts", "Crypto", "Tipo", "qty", "price", "notional_usdt"]]
        tr_display.columns = ["Quando", "Crypto", "Tipo", "Quantità", "Prezzo", "Valore"]
        st.dataframe(
            tr_display, width='stretch', hide_index=True,
            column_config={
                "Quantità": st.column_config.NumberColumn(format="%.4f"),
                "Prezzo": st.column_config.NumberColumn(format="$%.4f"),
                "Valore": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

st.divider()


# ============================================================================
# Glossary
# ============================================================================
with st.expander("Glossario — significato di ogni termine"):
    st.markdown(
        """
**Patrimonio totale (Equity)** — Quanto vale il tuo conto in questo momento se chiudessi tutte le posizioni ai prezzi attuali. La cifra finale che conta.

**Profitto/Perdita non realizzato** — Il guadagno o la perdita "sulla carta" delle posizioni ancora aperte. Diventa reale solo quando chiudi.

**Margine** — I soldi tuoi "in pegno" per tenere aperta una posizione con la leva. Con leva 10x, $100 di margine controllano $1.000 di crypto.

**Esposizione (Notional)** — Margine × leva. La dimensione vera della scommessa.

**Leva (Leverage)** — Moltiplicatore. 10x significa che con $1 muovi $10. Amplifica guadagni e perdite di 10×.

**LONG / SHORT** — LONG: scommetti che il prezzo salga (compri). SHORT: scommetti che scenda (vendi allo scoperto). Il bot fa solo LONG.

**Stop Loss (SL)** — Soglia di perdita oltre la quale la posizione viene chiusa automaticamente. Default: -30% sul margine.

**Take Profit (TP)** — Soglia di guadagno alla quale la posizione viene chiusa automaticamente. Default: +10% sul margine.

**Martingale** — Quando una posizione perde -5%, il bot raddoppia per "recuperare". Max 3 volte. Rischioso se il prezzo continua a scendere.

**HODL** — Compra e tieni, senza mai vendere.

**DCA (Dollar Cost Averaging)** — Comprare un po' alla volta a intervalli regolari (qui: settimanale per 8 settimane), per mediare il prezzo.

**Blue-chip** — Le 5 crypto più grandi e stabili: BTC, ETH, SOL, BNB, XRP.

**Live vs Paper** — Live: posizioni vere sul testnet, confermate dal broker. Paper: simulate sui prezzi reali. Per crypto liquide il risultato è praticamente identico.

**Testnet** — Ambiente di test della borsa Binance, soldi finti. Tutto qui è simulato, ma i prezzi sono reali.
        """
    )

st.caption(
    f"Auto-refresh ogni {REFRESH_SECONDS}s · Bot cycle ogni 15 min · "
    f"DB: {CFG.JOURNAL_DB.name}"
)
