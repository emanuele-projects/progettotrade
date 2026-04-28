"""Streamlit dashboard — multi-strategy, aggregate + granular, clean theme.

Run:
  streamlit run dashboard.py

Opens at http://localhost:8501
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from config import CFG, STRATEGY_ALLOCATIONS, TOTAL_CAPITAL_USDT
import execution


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

st.title("Trading Bot")
st.caption(
    f"Paper trading · Auto-refresh {REFRESH_SECONDS}s · "
    f"Capitale: ${TOTAL_CAPITAL_USDT:,.0f} su strategia Aggressive (Claude · 10x · martingale)"
)


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
    return shadow.get_all_breakdowns()


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
    f"Capitale ${agg_alloc:,.0f} · Leva {CFG.LEVERAGE}x · "
    f"Max {CFG.MAX_CONCURRENT_POSITIONS} posizioni · "
    f"Margine per entry ${agg_alloc * CFG.POSITION_MARGIN_PCT:,.0f} ({CFG.POSITION_MARGIN_PCT:.1%}) · "
    f"Initial deploy {CFG.INITIAL_DEPLOY_PCT:.0%} · "
    f"Reserve martingale {CFG.RESERVE_FOR_AVERAGING_PCT:.0%} · "
    f"TP {CFG.TAKE_PROFIT_PCT:+.0%} · SL {CFG.HARD_STOP_LOSS_PCT:+.0%} · "
    f"Universo {CFG.UNIVERSE_MAX_CANDIDATES} mid-cap + 5 large-cap ancore"
)

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
        rows.append({
            "Crypto": p["symbol"].replace("USDT", ""),
            "Grafico": f"https://www.binance.com/en/futures/{p['symbol']}",
            "Quantità": p["qty"],
            "Prezzo apertura": p["entry_price"],
            "Prezzo ora": p["mark_price"],
            "Variazione prezzo": price_change_pct,
            "Margine": p["isolated_margin"],
            "Esposizione": p["qty"] * p["mark_price"],
            "P&L $": p["unrealized_pnl"],
            "P&L %": p["unrealized_pnl_pct"],
            "Martingale": p["martingale_levels"],
        })
    st.dataframe(
        pd.DataFrame(rows), width='stretch', hide_index=True,
        column_config={
            "Grafico": st.column_config.LinkColumn(display_text="📈 Apri", help="Apre il grafico su Binance Futures."),
            "Quantità": st.column_config.NumberColumn(format="%.4f"),
            "Prezzo apertura": st.column_config.NumberColumn(format="$%.4f"),
            "Prezzo ora": st.column_config.NumberColumn(format="$%.4f"),
            "Variazione prezzo": st.column_config.NumberColumn(format="percent"),
            "Margine": st.column_config.NumberColumn(format="$%.2f", help="Soldi tuoi in pegno."),
            "Esposizione": st.column_config.NumberColumn(format="$%.2f", help="Margine × leva 10x."),
            "P&L $": st.column_config.NumberColumn(format="$%+,.2f"),
            "P&L %": st.column_config.NumberColumn(format="percent",
                                                   help="A -30% scatta SL, a +10% scatta TP."),
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
