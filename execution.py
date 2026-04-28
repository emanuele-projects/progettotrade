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
            leverage=int(p.get("leverage", CFG.LEVERAGE)),
            isolated_margin=margin,
        ))
    return out


def _symbol_filters(client: Client, symbol: str) -> tuple[float, float]:
    info = client.futures_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            lot, tick = 0.001, 0.01
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    lot = float(f["stepSize"])
                if f["filterType"] == "PRICE_FILTER":
                    tick = float(f["tickSize"])
            return lot, tick
    return 0.001, 0.01


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


def place_protective_orders(client: Client, symbol: str, entry_price: float, side: str = "LONG") -> None:
    """Place server-side STOP_MARKET (SL) + TAKE_PROFIT_MARKET (TP) with closePosition=true.

    Translation: collateral_pct / leverage = price_pct.
    e.g. HARD_STOP_LOSS_PCT=-0.30 with LEVERAGE=10 → price drops 3% to trigger.

    NOTE: disabled on Binance Futures Testnet — the testnet routes conditional
    orders through a separate "algo" system that doesn't reconcile with the
    regular open-orders / cancel-all endpoints (causes phantom -4130 errors).
    Cycle-level protection in main.py handles SL/TP during paper trading.
    Re-enabled automatically on mainnet (USE_TESTNET=False).
    """
    if CFG.DRY_RUN or CFG.USE_TESTNET:
        return
    _, tick = _symbol_filters(client, symbol)
    sl_price_pct = CFG.HARD_STOP_LOSS_PCT / CFG.LEVERAGE
    tp_price_pct = CFG.TAKE_PROFIT_PCT / CFG.LEVERAGE

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
    if CFG.DRY_RUN or CFG.USE_TESTNET:
        return
    try:
        client.futures_cancel_all_open_orders(symbol=symbol)
    except BinanceAPIException as e:
        # -2011 = unknown order; harmless when there were none
        if "2011" not in str(e):
            journal.log_event("WARN", f"cancel_all on {symbol}: {e}")


def ensure_protective_orders(client: Client, position: "Position") -> None:
    """Idempotent: if SL or TP missing for this open position, replace the pair."""
    if CFG.DRY_RUN or CFG.USE_TESTNET:
        return
    try:
        open_orders = client.futures_get_open_orders(symbol=position.symbol)
    except BinanceAPIException as e:
        journal.log_event("WARN", f"get_open_orders {position.symbol}: {e}")
        return
    has_sl = any(o.get("type") == "STOP_MARKET" for o in open_orders)
    has_tp = any(o.get("type") == "TAKE_PROFIT_MARKET" for o in open_orders)
    if has_sl and has_tp:
        return
    cancel_protective_orders(client, position.symbol)
    place_protective_orders(client, position.symbol, position.entry_price, position.side)


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


def open_long(client: Client, symbol: str, margin_usdt: float,
              sl_pct: float | None = None, tp_pct: float | None = None,
              leverage: int | None = None) -> dict | None:
    if CFG.DRY_RUN:
        journal.log_event("DRY_RUN", f"would open long {symbol} margin={margin_usdt:.2f}")
        return None

    lev = leverage if leverage is not None else CFG.LEVERAGE
    ensure_leverage_and_margin(client, symbol, leverage=lev)
    price = float(client.futures_symbol_ticker(symbol=symbol)["price"])
    notional = margin_usdt * lev
    lot_step, _ = _symbol_filters(client, symbol)
    qty = _floor_step(notional / price, lot_step)
    if qty <= 0:
        journal.log_event("WARN", f"qty rounded to 0 on {symbol} — margin too small")
        return None

    order = client.futures_create_order(
        symbol=symbol, side="BUY", type="MARKET", quantity=qty,
    )
    # Testnet sometimes returns avgPrice="0" right after MARKET fill — refetch position.
    time.sleep(0.5)
    new_positions = get_open_positions(client)
    new_pos = next((p for p in new_positions if p.symbol == symbol), None)
    fill = new_pos.entry_price if (new_pos and new_pos.entry_price > 0) else price
    journal.log_trade(
        symbol=symbol, side="LONG", qty=qty, price=fill,
        notional=qty * fill, leverage=lev,
        kind="open", note=f"margin_usdt={margin_usdt:.2f}",
        sl_pct=sl_pct, tp_pct=tp_pct,
    )
    if fill > 0:
        place_protective_orders(client, symbol, fill, "LONG")
    else:
        journal.log_event("WARN", f"{symbol}: zero fill price, protection deferred to next cycle")
    return order


def add_martingale(client: Client, position: Position) -> dict | None:
    if CFG.DRY_RUN:
        journal.log_event("DRY_RUN", f"would martingale-add {position.symbol}")
        return None

    add_margin = position.isolated_margin * CFG.MARTINGALE_ADD_RATIO
    notional = add_margin * position.leverage   # use the leverage this position was opened with
    lot_step, _ = _symbol_filters(client, position.symbol)
    qty = _floor_step(notional / position.mark_price, lot_step)
    if qty <= 0:
        return None

    # Cancel old SL/TP — they'll be replaced based on the new average entry
    cancel_protective_orders(client, position.symbol)

    order = client.futures_create_order(
        symbol=position.symbol, side="BUY", type="MARKET", quantity=qty,
    )
    fill = float(order.get("avgPrice") or position.mark_price)
    journal.log_trade(
        symbol=position.symbol, side="LONG", qty=qty, price=fill,
        notional=qty * fill, leverage=position.leverage,
        kind="martingale_add",
        note=f"new_level={position.martingale_levels + 1}",
    )

    # Re-place protective orders against the new average entry
    new_positions = get_open_positions(client)
    new_pos = next((p for p in new_positions if p.symbol == position.symbol), None)
    if new_pos:
        place_protective_orders(client, position.symbol, new_pos.entry_price, position.side)

    return order


def close_position(client: Client, position: Position, reason: str) -> dict | None:
    """reason in {'tp','sl','manual_close'}."""
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
