"""Binance Futures testnet execution + martingale + risk."""
from __future__ import annotations
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from binance.client import Client
from binance.exceptions import BinanceAPIException

from config import CFG
import journal


@dataclass
class Position:
    symbol: str
    side: str  # "LONG" or "SHORT"
    qty: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float  # vs isolated margin
    leverage: int
    isolated_margin: float
    liquidation_price: float = 0.0  # exchange-computed; 0 when unavailable
    martingale_levels: int = 0


def make_client() -> Client:
    if not CFG.BINANCE_API_KEY or not CFG.BINANCE_API_SECRET:
        raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET missing — set in .env")
    client = Client(CFG.BINANCE_API_KEY, CFG.BINANCE_API_SECRET, testnet=CFG.USE_TESTNET)
    # Align local timestamp to Binance server time to avoid APIError -1021
    # (local Windows clock drift is common and the recvWindow is unforgiving on testnet).
    server_ms = int(client.futures_time()["serverTime"])
    local_ms = int(time.time() * 1000)
    client.timestamp_offset = server_ms - local_ms
    return client


def get_account(client: Client) -> dict:
    acc = client.futures_account()
    return {
        "wallet_balance": float(acc["totalWalletBalance"]),
        "unrealized_pnl": float(acc["totalUnrealizedProfit"]),
        "total_equity": float(acc["totalMarginBalance"]),
        "available_balance": float(acc["availableBalance"]),
    }


def _effective_leverage(raw_position: dict) -> int:
    """Leverage of an open position.

    The v3 positionRisk payload has no `leverage` field. Derive it from
    |notional| / initialMargin (exact by definition: initialMargin = notional/L);
    fall back to the legacy field, then to CFG.LEVERAGE. Never underestimate:
    the risk engine computes liquidation distance from this."""
    if raw_position.get("leverage"):
        return int(raw_position["leverage"])
    try:
        notional = abs(float(raw_position["notional"]))
        initial_margin = float(
            raw_position.get("positionInitialMargin") or raw_position.get("initialMargin") or 0
        )
        if notional > 0 and initial_margin > 0:
            return max(1, round(notional / initial_margin))
    except (KeyError, ValueError, TypeError):
        pass
    return CFG.LEVERAGE


def get_open_positions(client: Client) -> list[Position]:
    raw = client.futures_position_information()
    out: list[Position] = []
    for p in raw:
        qty = float(p["positionAmt"])
        if qty == 0:
            continue
        entry = float(p["entryPrice"])
        mark = float(p["markPrice"])
        side = "LONG" if qty > 0 else "SHORT"
        unrealized = float(p["unRealizedProfit"])
        margin = float(p.get("isolatedWallet") or p.get("isolatedMargin") or 0)
        pnl_pct = unrealized / margin if margin else 0.0
        out.append(Position(
            symbol=p["symbol"], side=side, qty=abs(qty),
            entry_price=entry, mark_price=mark,
            unrealized_pnl=unrealized, unrealized_pnl_pct=pnl_pct,
            leverage=_effective_leverage(p),
            isolated_margin=margin,
            liquidation_price=float(p.get("liquidationPrice") or 0),
        ))
    return out


# futures_exchange_info() is a ~2 MB response; caching is mandatory once orders
# can fire on every tick instead of once per 15-min cycle.
_FILTERS_CACHE: dict[str, tuple[float, float]] = {}
_FILTERS_CACHE_TS: float = 0.0
_FILTERS_TTL_SECONDS = 6 * 3600


def _symbol_filters(client: Client, symbol: str) -> tuple[float, float]:
    global _FILTERS_CACHE_TS
    now = time.monotonic()
    if not _FILTERS_CACHE or now - _FILTERS_CACHE_TS > _FILTERS_TTL_SECONDS:
        info = client.futures_exchange_info()
        fresh: dict[str, tuple[float, float]] = {}
        for s in info["symbols"]:
            lot, tick = 0.001, 0.01
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    lot = float(f["stepSize"])
                if f["filterType"] == "PRICE_FILTER":
                    tick = float(f["tickSize"])
            fresh[s["symbol"]] = (lot, tick)
        _FILTERS_CACHE.clear()
        _FILTERS_CACHE.update(fresh)
        _FILTERS_CACHE_TS = now
    return _FILTERS_CACHE.get(symbol, (0.001, 0.01))


_MAX_LEV_CACHE: dict[str, int] = {}
_MAX_LEV_CACHE_TS: float = 0.0


def get_max_leverage(client: Client, symbol: str) -> int:
    """Max leverage Binance allows on `symbol` (first bracket), capped at CFG.MAX_LEVERAGE.

    All brackets are fetched in ONE call and cached for
    LEVERAGE_BRACKET_REFRESH_HOURS. On testnet the endpoint can be flaky —
    fall back to CFG.MAX_LEVERAGE and log."""
    global _MAX_LEV_CACHE_TS
    now = time.monotonic()
    ttl = CFG.LEVERAGE_BRACKET_REFRESH_HOURS * 3600
    if not _MAX_LEV_CACHE or now - _MAX_LEV_CACHE_TS > ttl:
        try:
            data = client.futures_leverage_bracket()  # every symbol at once
            fresh = {
                entry["symbol"]: min(int(entry["brackets"][0]["initialLeverage"]), CFG.MAX_LEVERAGE)
                for entry in data
            }
            _MAX_LEV_CACHE.clear()
            _MAX_LEV_CACHE.update(fresh)
            _MAX_LEV_CACHE_TS = now
        except (BinanceAPIException, KeyError, IndexError, TypeError) as e:
            journal.log_event("WARN", f"leverage brackets refresh: {e} — assuming {CFG.MAX_LEVERAGE}x")
            _MAX_LEV_CACHE_TS = now  # don't hammer a broken endpoint every order
    return _MAX_LEV_CACHE.get(symbol, CFG.MAX_LEVERAGE)


def symbol_tradable(client: Client, symbol: str) -> bool:
    """True if the CURRENT exchange (testnet when USE_TESTNET) lists the symbol.

    The candidate universe is built from MAINNET data; the testnet lists fewer
    symbols and answers with malformed payloads for unknown ones."""
    _symbol_filters(client, symbol)  # refreshes the exchange-info cache if stale
    return symbol in _FILTERS_CACHE


def estimated_liq_distance(leverage: int) -> float:
    """Approximate adverse price move (fraction) that liquidates an isolated position.

    liq ≈ 1/L − maintenance-margin-rate. Conservative MMR estimate: at $500 margin
    × 20x = $10k notional every symbol sits in its lowest (tier-1) bracket."""
    return max(1.0 / leverage - CFG.RISK_MMR_ESTIMATE, 0.001)


def clamp_sl_to_liquidation(sl_pct: float, leverage: int) -> float:
    """Clamp a ROE-based stop-loss so its price distance stays safely inside liquidation.

    sl_pct is negative ROE (fraction of margin). Price distance = |sl_pct| / leverage.
    Max allowed price distance = RISK_SL_MAX_FRACTION_OF_LIQ × estimated_liq_distance.
    Returns the (possibly clamped) sl_pct, still negative."""
    max_price_dist = CFG.RISK_SL_MAX_FRACTION_OF_LIQ * estimated_liq_distance(leverage)
    price_dist = abs(sl_pct) / leverage
    if price_dist <= max_price_dist:
        return sl_pct
    return -(max_price_dist * leverage)


def _floor_step(value: float, step: float) -> float:
    """Floor `value` to nearest multiple of `step` without IEEE-754 float noise.

    Binance rejects orders with extra precision (-1111), so we round via Decimal.
    """
    d_v = Decimal(str(value))
    d_s = Decimal(str(step))
    n = (d_v // d_s) * d_s
    return float(n.quantize(d_s, rounding=ROUND_DOWN))


def _round_price(value: float, tick: float) -> float:
    return _floor_step(value, tick)


def place_protective_orders(client: Client, symbol: str, entry_price: float, side: str = "LONG",
                            sl_pct: float | None = None, tp_pct: float | None = None,
                            leverage: int | None = None) -> None:
    """Place server-side STOP_MARKET (SL) + TAKE_PROFIT_MARKET (TP) with closePosition=true.

    sl_pct/tp_pct are ROE-based (fraction of isolated margin), leverage is the
    position's own leverage. Translation: collateral_pct / leverage = price_pct.
    e.g. sl_pct=-0.30 with leverage=10 → price moves 3% adversely to trigger.
    Falls back to CFG defaults when per-trade values are missing (legacy rows).

    2026-07-15 (always-invested portfolio): ENABLED on testnet too — exchange-held
    exits fire even when this process is down, and free the local risk engine to
    act as backstop only. If the testnet's conditional-order subsystem misbehaves
    (historical phantom -4130s), the WARN path below leaves the position without
    server orders and the risk engine automatically keeps enforcing SL/TP for it
    (PositionCache marks server_protected only when BOTH orders are live).
    """
    if CFG.DRY_RUN or (CFG.USE_TESTNET and not CFG.SERVER_SIDE_PROTECTION_ON_TESTNET):
        return
    lev = leverage if leverage is not None else CFG.LEVERAGE
    sl = sl_pct if sl_pct is not None else CFG.HARD_STOP_LOSS_PCT
    tp = tp_pct if tp_pct is not None else CFG.TAKE_PROFIT_PCT
    _, tick = _symbol_filters(client, symbol)
    sl_price_pct = sl / lev
    tp_price_pct = tp / lev

    if side == "LONG":
        sl_price = _round_price(entry_price * (1 + sl_price_pct), tick)
        tp_price = _round_price(entry_price * (1 + tp_price_pct), tick)
        close_side = "SELL"
    else:
        sl_price = _round_price(entry_price * (1 - sl_price_pct), tick)
        tp_price = _round_price(entry_price * (1 - tp_price_pct), tick)
        close_side = "BUY"

    try:
        client.futures_create_order(
            symbol=symbol, side=close_side, type="STOP_MARKET",
            stopPrice=sl_price, closePosition=True, workingType="MARK_PRICE",
        )
    except BinanceAPIException as e:
        journal.log_event("WARN", f"SL order on {symbol}: {e}")
    try:
        client.futures_create_order(
            symbol=symbol, side=close_side, type="TAKE_PROFIT_MARKET",
            stopPrice=tp_price, closePosition=True, workingType="MARK_PRICE",
        )
    except BinanceAPIException as e:
        journal.log_event("WARN", f"TP order on {symbol}: {e}")
    journal.log_event("PROTECT", f"{symbol} sl={sl_price} tp={tp_price}")


def cancel_protective_orders(client: Client, symbol: str) -> None:
    """Cancel BOTH order books for the symbol: the classic one and the algo/
    conditional one. STOP_MARKET / TAKE_PROFIT_MARKET now live in the algo
    subsystem (python-binance ≥1.0.37 routes them there) and are INVISIBLE to
    the classic openOrders/cancel-all endpoints — pass conditional=True."""
    if CFG.DRY_RUN or (CFG.USE_TESTNET and not CFG.SERVER_SIDE_PROTECTION_ON_TESTNET):
        return
    for conditional in (False, True):
        try:
            client.futures_cancel_all_open_orders(symbol=symbol, conditional=conditional)
        except BinanceAPIException as e:
            # -2011 = unknown order; harmless when there were none
            if "2011" not in str(e):
                journal.log_event("WARN", f"cancel_all({'algo' if conditional else 'classic'}) on {symbol}: {e}")


def protective_order_types(order: dict) -> str:
    """Order type across payload dialects (classic: `type`; algo: `orderType`)."""
    return order.get("orderType") or order.get("type") or order.get("origType") or ""


def ensure_protective_orders(client: Client, position: "Position") -> None:
    """Idempotent: if SL or TP missing for this open position, replace the pair."""
    if CFG.DRY_RUN or (CFG.USE_TESTNET and not CFG.SERVER_SIDE_PROTECTION_ON_TESTNET):
        return
    try:
        open_orders = list(client.futures_get_open_orders(symbol=position.symbol, conditional=True))
        open_orders += list(client.futures_get_open_orders(symbol=position.symbol))
    except BinanceAPIException as e:
        journal.log_event("WARN", f"get_open_orders {position.symbol}: {e}")
        return
    has_sl = any(protective_order_types(o) == "STOP_MARKET" for o in open_orders)
    has_tp = any(protective_order_types(o) == "TAKE_PROFIT_MARKET" for o in open_orders)
    if has_sl and has_tp:
        return
    cancel_protective_orders(client, position.symbol)
    sl_pct, tp_pct = journal.get_position_targets(position.symbol)
    place_protective_orders(client, position.symbol, position.entry_price, position.side,
                            sl_pct=sl_pct, tp_pct=tp_pct, leverage=position.leverage)


def ensure_leverage_and_margin(client: Client, symbol: str, leverage: int | None = None) -> None:
    lev = leverage if leverage is not None else CFG.LEVERAGE
    try:
        client.futures_change_leverage(symbol=symbol, leverage=lev)
    except BinanceAPIException as e:
        journal.log_event("WARN", f"set leverage {symbol} {lev}x: {e}")
    try:
        client.futures_change_margin_type(symbol=symbol, marginType=CFG.MARGIN_TYPE)
    except BinanceAPIException as e:
        # -4046 = margin type already set; harmless
        if "4046" not in str(e):
            journal.log_event("WARN", f"set margin type {symbol}: {e}")


def open_position(client: Client, symbol: str, side: str, margin_usdt: float,
                  sl_pct: float | None = None, tp_pct: float | None = None,
                  leverage: int | None = None, trigger: str | None = None) -> dict | None:
    """Open a LONG or SHORT market position with per-trade leverage and stops.

    Leverage is clamped to the symbol's Binance bracket and CFG.MAX_LEVERAGE;
    the ROE stop-loss is clamped so its price distance stays inside the
    estimated liquidation distance (both no-ops in the allowed schema ranges,
    logged when they do fire)."""
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")
    if CFG.DRY_RUN:
        journal.log_event("DRY_RUN", f"would open {side.lower()} {symbol} margin={margin_usdt:.2f}")
        return None
    if not symbol_tradable(client, symbol):
        journal.log_event("WARN", f"{symbol}: not listed on the current exchange (testnet?) — skipped")
        return None

    lev = leverage if leverage is not None else CFG.LEVERAGE
    max_lev = get_max_leverage(client, symbol)
    if lev > max_lev:
        journal.log_event("WARN", f"{symbol}: leverage {lev}x > bracket max {max_lev}x — clamped")
        lev = max_lev
    if sl_pct is not None:
        clamped = clamp_sl_to_liquidation(sl_pct, lev)
        if clamped != sl_pct:
            journal.log_event("WARN", f"{symbol}: SL {sl_pct:+.2f} beyond liq-safe range at {lev}x — clamped to {clamped:+.2f}")
            sl_pct = clamped

    # Re-entry hygiene: a stale closePosition SL/TP left over from a previous
    # position on this symbol would close the NEW position at the wrong level.
    cancel_protective_orders(client, symbol)

    ensure_leverage_and_margin(client, symbol, leverage=lev)
    ticker = client.futures_symbol_ticker(symbol=symbol)
    price_raw = ticker.get("price") if isinstance(ticker, dict) else None
    if not price_raw or float(price_raw) <= 0:
        journal.log_event("WARN", f"{symbol}: ticker without a usable price — skipped")
        return None
    price = float(price_raw)
    notional = margin_usdt * lev
    lot_step, _ = _symbol_filters(client, symbol)
    qty = _floor_step(notional / price, lot_step)
    if qty <= 0:
        journal.log_event("WARN", f"qty rounded to 0 on {symbol} — margin too small")
        return None

    order = client.futures_create_order(
        symbol=symbol, side="BUY" if side == "LONG" else "SELL",
        type="MARKET", quantity=qty,
    )
    # Testnet sometimes returns avgPrice="0" right after MARKET fill — refetch position.
    time.sleep(0.5)
    new_positions = get_open_positions(client)
    new_pos = next((p for p in new_positions if p.symbol == symbol), None)
    fill = new_pos.entry_price if (new_pos and new_pos.entry_price > 0) else price
    journal.log_trade(
        symbol=symbol, side=side, qty=qty, price=fill,
        notional=qty * fill, leverage=lev,
        kind="open", note=f"margin_usdt={margin_usdt:.2f}",
        sl_pct=sl_pct, tp_pct=tp_pct, trigger=trigger,
    )
    if fill > 0:
        place_protective_orders(client, symbol, fill, side,
                                sl_pct=sl_pct, tp_pct=tp_pct, leverage=lev)
    else:
        journal.log_event("WARN", f"{symbol}: zero fill price, protection deferred to next cycle")
    return order


def add_martingale(client: Client, position: Position) -> dict | None:
    if not CFG.MARTINGALE_ENABLED:
        return None  # averaging-down disabled (amplified drawdown on live data)
    if CFG.DRY_RUN:
        journal.log_event("DRY_RUN", f"would martingale-add {position.symbol}")
        return None
    # Averaging into a losing position is only sane at moderate leverage:
    # above MARTINGALE_MAX_LEVERAGE the position lives or dies on its stop.
    if position.leverage > CFG.MARTINGALE_MAX_LEVERAGE:
        journal.log_event("WARN", f"martingale skipped on {position.symbol}: "
                                  f"{position.leverage}x > {CFG.MARTINGALE_MAX_LEVERAGE}x cap")
        return None

    add_margin = position.isolated_margin * CFG.MARTINGALE_ADD_RATIO
    notional = add_margin * position.leverage   # use the leverage this position was opened with
    lot_step, _ = _symbol_filters(client, position.symbol)
    qty = _floor_step(notional / position.mark_price, lot_step)
    if qty <= 0:
        return None

    # Cancel old SL/TP — they'll be replaced based on the new average entry
    cancel_protective_orders(client, position.symbol)

    # Averaging adds in the SAME direction as the position (BUY for LONG, SELL for SHORT)
    order = client.futures_create_order(
        symbol=position.symbol, side="BUY" if position.side == "LONG" else "SELL",
        type="MARKET", quantity=qty,
    )
    fill = float(order.get("avgPrice") or position.mark_price)
    journal.log_trade(
        symbol=position.symbol, side=position.side, qty=qty, price=fill,
        notional=qty * fill, leverage=position.leverage,
        kind="martingale_add",
        note=f"new_level={position.martingale_levels + 1}",
    )

    # Re-place protective orders against the new average entry, honoring the
    # per-trade targets Claude chose at open (journal is the source of truth).
    new_positions = get_open_positions(client)
    new_pos = next((p for p in new_positions if p.symbol == position.symbol), None)
    if new_pos:
        sl_pct, tp_pct = journal.get_position_targets(position.symbol)
        place_protective_orders(client, position.symbol, new_pos.entry_price, position.side,
                                sl_pct=sl_pct, tp_pct=tp_pct, leverage=position.leverage)

    return order


def close_position(client: Client, position: Position, reason: str,
                   trigger: str | None = None) -> dict | None:
    """reason in {'tp','sl','liq_guard','manual_close'}."""
    if CFG.DRY_RUN:
        journal.log_event("DRY_RUN", f"would close {position.symbol} reason={reason}")
        return None
    # Cancel any pending SL/TP first so they don't fire on a future re-entry of this symbol
    cancel_protective_orders(client, position.symbol)
    side = "SELL" if position.side == "LONG" else "BUY"
    order = client.futures_create_order(
        symbol=position.symbol, side=side, type="MARKET",
        quantity=position.qty, reduceOnly=True,
    )
    fill = float(order.get("avgPrice") or position.mark_price)
    journal.log_trade(
        symbol=position.symbol, side=position.side, qty=position.qty, price=fill,
        notional=position.qty * fill, leverage=position.leverage,
        kind=reason,
        note=f"unrealized_pnl_pct={position.unrealized_pnl_pct:+.2%}",
        trigger=trigger,
    )
    return order


def count_martingale_levels(symbol: str) -> int:
    """How many martingale_add entries since the most recent open for this symbol."""
    with sqlite3.connect(CFG.JOURNAL_DB) as c:
        c.row_factory = sqlite3.Row
        last_open = c.execute(
            "SELECT ts FROM trades WHERE symbol=? AND kind='open' ORDER BY id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if not last_open:
            return 0
        row = c.execute(
            "SELECT COUNT(*) AS n FROM trades "
            "WHERE symbol=? AND kind='martingale_add' AND ts > ?",
            (symbol, last_open["ts"]),
        ).fetchone()
        return int(row["n"])
