"""Vercel serverless function: read-only live view of the Binance testnet account.

Vercel is serverless — it cannot run the always-on bot or the Streamlit server.
This function answers a single request: it signs a few Binance Futures Testnet
REST calls, computes the same money/exposure/track-record figures the Streamlit
dashboard shows, and returns them as JSON. The bot itself keeps running on the
Oracle VM; Vercel is only a public window onto the account.

Auth: the caller must pass ?pw=<DASHBOARD_PASSWORD>. Without the right password
the function returns 401 and no data. Env vars required on Vercel:
  BINANCE_API_KEY, BINANCE_API_SECRET  (testnet keys — fake money)
  DASHBOARD_PASSWORD                    (gate)
  INITIAL_CAPITAL                       (optional, default 3912.89)

Zero third-party dependencies (urllib + hmac from stdlib) → fast cold starts.
"""
from http.server import BaseHTTPRequestHandler
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

BASE = "https://testnet.binancefuture.com"
_LARGE_CAPS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"}


def _get(path: str, params: dict | None = None, signed: bool = False) -> object:
    params = dict(params or {})
    headers = {}
    if signed:
        secret = os.environ["BINANCE_API_SECRET"]
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        qs = urllib.parse.urlencode(params)
        sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{BASE}{path}?{qs}&signature={sig}"
        headers["X-MBX-APIKEY"] = os.environ["BINANCE_API_KEY"]
    else:
        url = f"{BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode())


def build_state() -> dict:
    initial = float(os.environ.get("INITIAL_CAPITAL", "3912.89"))
    # Clean-run start (2026-07-15 reset). Binance income history is NOT reset, so
    # it still holds pre-reset trades contaminated by the old double-instance bug
    # (−$1141). Everything "since reset" filters realized events to ≥ this epoch.
    # Override via env TRACK_START_MS if the account is ever rebased again.
    reset_ms = int(os.environ.get("TRACK_START_MS", "1784142000000"))

    account = _get("/fapi/v2/account", signed=True)
    risk = _get("/fapi/v2/positionRisk", signed=True)
    income_all = _get("/fapi/v1/income",
                      {"incomeType": "REALIZED_PNL", "limit": 1000}, signed=True)
    income = [r for r in income_all if int(r.get("time", 0)) >= reset_ms]
    # Broader ledger (all types: realized P&L, funding, commissions, transfers)
    # for the "recent movements" feed and the fee/funding tallies.
    ledger_all = _get("/fapi/v1/income", {"limit": 1000}, signed=True)
    ledger = [r for r in ledger_all if int(r.get("time", 0)) >= reset_ms]
    funding_total = sum(float(r["income"]) for r in ledger if r.get("incomeType") == "FUNDING_FEE")
    commission_total = sum(float(r["income"]) for r in ledger if r.get("incomeType") == "COMMISSION")

    equity = float(account["totalMarginBalance"])
    wallet = float(account["totalWalletBalance"])
    unrealized = float(account["totalUnrealizedProfit"])
    available = float(account["availableBalance"])

    # --- positions (from positionRisk: has markPrice + liquidationPrice) ---
    positions = []
    for p in risk:
        amt = float(p.get("positionAmt") or 0)
        if amt == 0:
            continue
        entry = float(p.get("entryPrice") or 0)
        mark = float(p.get("markPrice") or 0)
        upnl = float(p.get("unRealizedProfit") or 0)
        # leverage: v3 positionRisk may lack it → derive from notional/margin
        lev = int(float(p.get("leverage") or 0)) or None
        margin = float(p.get("isolatedWallet") or p.get("isolatedMargin") or 0)
        if not lev and margin > 0:
            lev = max(1, round(abs(amt) * entry / margin))
        roe = (upnl / margin) if margin else 0.0
        positions.append({
            "symbol": p["symbol"],
            "base": p["symbol"].replace("USDT", ""),
            "side": "LONG" if amt > 0 else "SHORT",
            "qty": abs(amt),
            "entry": entry,
            "mark": mark,
            "leverage": lev or 0,
            "margin": margin,
            "exposure": abs(amt) * mark,
            "upnl": upnl,
            "roe": roe,
            "liq": float(p.get("liquidationPrice") or 0),
            "anchor": p["symbol"] in _LARGE_CAPS,
        })
    positions.sort(key=lambda x: -abs(x["upnl"]))

    deployed = sum(p["margin"] for p in positions)
    exposure = sum(p["exposure"] for p in positions)
    long_exp = sum(p["exposure"] for p in positions if p["side"] == "LONG")
    short_exp = sum(p["exposure"] for p in positions if p["side"] == "SHORT")
    n_long = sum(1 for p in positions if p["side"] == "LONG")
    n_short = len(positions) - n_long

    # --- realized-P&L track record (last 7 days) ---
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - 7 * 24 * 3600 * 1000
    evs = sorted(
        [(int(r["time"]), float(r["income"]), r.get("symbol", ""))
         for r in income if int(r.get("time", 0)) >= cutoff],
        key=lambda x: x[0],
    )
    vals = [v for _, v, _ in evs]
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    n = len(vals)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    last10 = vals[-10:]
    track = {
        "n": n,
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "win_rate_10": (sum(1 for v in last10 if v > 0) / len(last10) * 100) if last10 else 0.0,
        "net": sum(vals),
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else (None if not wins else -1),
    }
    # cumulative realized curve (all history, for the chart)
    cum = []
    running = 0.0
    for t, v, _ in sorted([(int(r["time"]), float(r["income"]), 0) for r in income],
                          key=lambda x: x[0]):
        running += v
        cum.append([t, round(running, 2)])

    # --- per-symbol P&L + trade stats (realized since reset + unrealized open) ---
    events_by_sym: dict[str, list] = {}
    for r in sorted(income, key=lambda r: int(r.get("time", 0))):
        s = r.get("symbol", "")
        if s:
            events_by_sym.setdefault(s, []).append((int(r["time"]), float(r["income"])))
    realized_by_sym = {s: sum(v for _, v in evs) for s, evs in events_by_sym.items()}
    unreal_by_sym = {p["symbol"]: p["upnl"] for p in positions}
    per_symbol = []
    for s in set(realized_by_sym) | set(unreal_by_sym):
        evs = events_by_sym.get(s, [])
        w = [v for _, v in evs if v > 0]
        l = [v for _, v in evs if v < 0]
        rz = realized_by_sym.get(s, 0.0)
        uz = unreal_by_sym.get(s, 0.0)
        tot = rz + uz
        per_symbol.append({
            "base": s.replace("USDT", ""),
            "realized": round(rz, 2),
            "unrealized": round(uz, 2),
            "total": round(tot, 2),
            "pct": (tot / initial * 100) if initial else 0.0,
            "open": s in unreal_by_sym,
            "n": len(evs), "wins": len(w), "losses": len(l),
            "win_rate": (len(w) / len(evs) * 100) if evs else None,
            "best": round(max((v for _, v in evs), default=0.0), 2),
            "worst": round(min((v for _, v in evs), default=0.0), 2),
            "last_ts": evs[-1][0] if evs else None,
        })
    per_symbol.sort(key=lambda x: -x["total"])

    # --- per-symbol cumulative realized curves (top 8 by |total|): the time-axis
    # view that correlates each crypto as an investment over the run ---
    curves = []
    for s in sorted((x for x in per_symbol if x["n"] > 0),
                    key=lambda x: -abs(x["total"]))[:8]:
        sym = s["base"] + "USDT"
        run, pts = 0.0, [[reset_ms, 0.0]]
        for t, v in events_by_sym.get(sym, []):
            run += v
            pts.append([t, round(run, 2)])
        curves.append({"base": s["base"], "points": pts})

    # --- computed insights, ordered by importance ---
    insights = []
    def _ins(icon, title, text): insights.append({"icon": icon, "title": title, "text": text})
    top3 = [s for s in per_symbol if s["total"] > 0][:3]
    if top3:
        _ins("🏆", "Chi ti sta pagando",
             " · ".join(f"{s['base']} +${s['total']:.0f} ({s['pct']:+.1f}% del capitale)" for s in top3))
    bot3 = sorted((s for s in per_symbol if s["total"] < 0), key=lambda s: s["total"])[:3]
    if bot3:
        _ins("🔻", "Chi ti sta costando",
             " · ".join(f"{s['base']} −${abs(s['total']):.0f}" for s in bot3))
    if income:
        ev = sorted(((int(r["time"]), float(r["income"]), (r.get("symbol", "") or "").replace("USDT", ""))
                     for r in income), key=lambda x: x[1])
        _ins("🎯", "Colpo migliore", f"{ev[-1][2]} +${ev[-1][1]:.0f} in un singolo trade")
        _ins("💥", "Perdita peggiore", f"{ev[0][2]} −${abs(ev[0][1]):.0f} in un singolo trade")
    mt = max(per_symbol, key=lambda s: s["n"], default=None)
    if mt and mt["n"] >= 3:
        _ins("📊", "Dove insisti di più",
             f"{mt['base']}: {mt['n']} trade chiusi (win-rate {mt['win_rate']:.0f}%), "
             f"netto ${mt['total']:+.0f} — {'e ti paga' if mt['total'] > 0 else 'ma finora ci perdi'}")
    wins_all = sorted((float(r["income"]) for r in income if float(r["income"]) > 0), reverse=True)
    if len(wins_all) >= 3:
        share = sum(wins_all[:2]) / sum(wins_all) * 100
        _ins("🧮", "Concentrazione dei profitti",
             f"I 2 trade migliori valgono il {share:.0f}% di tutte le vincite"
             + (" — campione ancora fragile" if share > 60 else " — profitto ben distribuito"))
    gross_wins = sum(wins_all) if wins_all else 0.0
    drag = abs(commission_total) + abs(min(funding_total, 0.0))
    if gross_wins > 0 and drag > 0:
        _ins("💸", "Attrito dei costi",
             f"Commissioni ${abs(commission_total):.0f} + funding ${abs(funding_total):.0f} "
             f"= ~{drag / gross_wins * 100:.0f}% del lordo vinto se ne va in costi")
    seq = [float(r["income"]) for r in sorted(income, key=lambda r: int(r.get("time", 0)))]
    if seq:
        k = 1
        for a, b in zip(seq[::-1], seq[::-1][1:]):
            if (a >= 0) == (b >= 0): k += 1
            else: break
        _ins("🔁", "Striscia attuale", f"{k} {'vincite' if seq[-1] >= 0 else 'perdite'} di fila")
    if positions:
        big = max(positions, key=lambda p: p["exposure"])
        _ins("⚖️", "Assetto adesso",
             f"{n_long} long / {n_short} short · posizione più esposta {big['base']} "
             f"(${big['exposure']:.0f}, {big['leverage']}x)")

    # --- Claude's brain (reasoning, lessons, equity curve) from the VM snapshot.
    # The journal lives on the Oracle VM; the bot uploads a JSON snapshot to
    # Vercel Blob and SNAPSHOT_URL points at it. Absent env → section omitted. ---
    claude = None
    snapshot_url = os.environ.get("SNAPSHOT_URL", "")
    if snapshot_url:
        try:
            req = urllib.request.Request(snapshot_url, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=8) as r:
                claude = json.loads(r.read().decode())
        except Exception:
            claude = None

    # --- max drawdown on the REALIZED equity curve (initial + cumulative realized) ---
    # No equity snapshots on Vercel (those live in the VM journal), so this is the
    # drawdown of realized cash, not mark-to-market — labelled as such on the page.
    max_dd = 0.0
    peak = initial
    for _, run in cum:
        eqp = initial + run
        if eqp > peak:
            peak = eqp
        if peak > 0:
            dd = (eqp / peak - 1) * 100
            if dd < max_dd:
                max_dd = dd

    # --- alpha vs BTC buy-and-hold over the run window (best-effort) ---
    benchmark = None
    try:
        start_ms = min(int(r["time"]) for r in income) if income else None
        if start_ms:
            kl = _get("/fapi/v1/klines",
                      {"symbol": "BTCUSDT", "interval": "1h", "startTime": start_ms, "limit": 1})
            btc_start = float(kl[0][1]) if kl else 0.0
            btc_now = float(_get("/fapi/v1/ticker/price", {"symbol": "BTCUSDT"})["price"])
            if btc_start:
                btc_ret = (btc_now / btc_start - 1) * 100
                strat_ret = (equity / initial - 1) * 100 if initial else 0.0
                benchmark = {
                    "start_ms": start_ms,
                    "btc_ret_pct": round(btc_ret, 2),
                    "strat_ret_pct": round(strat_ret, 2),
                    "alpha_pct": round(strat_ret - btc_ret, 2),
                }
    except Exception:
        benchmark = None

    # --- recent movements feed = the meaningful events (position closes, i.e.
    # realized P&L). Commissions/funding are noise here and live in the tallies
    # below. Opens aren't in income history (Vercel has no journal), so the feed
    # is the realized outcomes: "🎯 ESPORTS +$231" / "🛑 XRP −$52". ---
    movements = []
    for r in sorted(income, key=lambda r: int(r.get("time", 0)), reverse=True)[:30]:
        amt = round(float(r.get("income", 0)), 2)
        movements.append({
            "kind": "win" if amt >= 0 else "loss",
            "symbol": r.get("symbol", ""),
            "base": (r.get("symbol", "") or "").replace("USDT", ""),
            "amount": amt,
            "ts": int(r.get("time", 0)),
        })

    # --- best / worst open position (by unrealized P&L) ---
    best_pos = worst_pos = None
    if positions:
        bp = max(positions, key=lambda p: p["upnl"])
        wp = min(positions, key=lambda p: p["upnl"])
        best_pos = {"base": bp["base"], "upnl": bp["upnl"], "roe": bp["roe"]}
        worst_pos = {"base": wp["base"], "upnl": wp["upnl"], "roe": wp["roe"]}

    pnl_total = equity - initial
    return {
        "ts": now_ms,
        "money": {
            "initial": initial,
            "equity": equity,
            "wallet": wallet,
            "unrealized": unrealized,
            "available": available,
            "pnl_total": pnl_total,
            "pnl_total_pct": (pnl_total / initial * 100) if initial else 0.0,
            "pnl_realized": wallet - initial,
        },
        "exposure": {
            "deployed_margin": deployed,
            "deployed_pct": (deployed / equity * 100) if equity else 0.0,
            "total_exposure": exposure,
            "avg_leverage": (exposure / deployed) if deployed else 0.0,
            "long_exp": long_exp,
            "short_exp": short_exp,
            "net_exp": long_exp - short_exp,
            "n_long": n_long,
            "n_short": n_short,
        },
        "positions": positions,
        "per_symbol": per_symbol,
        "track": track,
        "benchmark": benchmark,
        "risk": {"max_drawdown_pct": round(max_dd, 2)},
        "costs": {
            "funding_total": round(funding_total, 2),
            "commission_total": round(commission_total, 2),
        },
        "best_pos": best_pos,
        "worst_pos": worst_pos,
        "movements": movements,
        "per_symbol_curves": curves,
        "insights": insights,
        "claude": claude,
        "realized_curve": cum[-500:],
        "mandate": {"min": 10, "target": 12},
    }


class handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (Vercel/BaseHTTPRequestHandler convention)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pw = query.get("pw", [""])[0]
        expected = os.environ.get("DASHBOARD_PASSWORD", "")
        if not expected:
            return self._json(500, {"error": "DASHBOARD_PASSWORD non impostata su Vercel"})
        if not hmac.compare_digest(pw, expected):
            return self._json(401, {"error": "unauthorized"})
        try:
            return self._json(200, build_state())
        except Exception as e:  # surface the reason to the page
            return self._json(502, {"error": f"Binance/env error: {e}"})
