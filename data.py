"""Market data + macro + news + feature engineering.

Uses public Binance Futures endpoints (no auth needed) for prices/klines so the
data feed is consistent regardless of whether the trading client is on testnet
or mainnet. Testnet symbol availability and prices can drift.
"""
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

from config import CFG


_PUBLIC_BASE = "https://fapi.binance.com"


@dataclass
class Features:
    symbol: str
    last_price: float
    ret_1h: float
    ret_24h: float
    ret_7d: float
    rsi_14: float
    ema20: float
    ema50: float
    above_ema50: bool
    volume_24h_usd: float


def _safe_get(url: str, params: dict | None = None, retries: int = 3) -> Any:
    last_exc: Exception | None = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_exc = e
            time.sleep(1 + i)
    raise RuntimeError(f"GET {url} failed after {retries} retries: {last_exc}")


def get_price(symbol: str) -> float:
    r = _safe_get(f"{_PUBLIC_BASE}/fapi/v1/ticker/price", params={"symbol": symbol})
    return float(r["price"])


def get_futures_universe() -> list[str]:
    """USDT-margined PERPETUAL pairs that are TRADING on Binance Futures.

    Filters out non-ASCII symbols (Binance lists some CJK-named meme contracts).
    """
    data = _safe_get(f"{_PUBLIC_BASE}/fapi/v1/exchangeInfo")
    out = []
    for s in data.get("symbols", []):
        sym = s["symbol"]
        if not sym.isascii():
            continue
        if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"):
            out.append(sym)
    return out


def get_24h_volumes(symbols: list[str]) -> dict[str, float]:
    data = _safe_get(f"{_PUBLIC_BASE}/fapi/v1/ticker/24hr")
    wanted = set(symbols)
    return {row["symbol"]: float(row.get("quoteVolume", 0))
            for row in data if row["symbol"] in wanted}


def get_market_caps(symbols: list[str]) -> dict[str, float]:
    """Approximate market cap via CoinGecko top-500 list. Symbols not found are omitted."""
    base_to_binance = {s.replace("USDT", "").lower(): s for s in symbols if s.endswith("USDT")}
    pages = []
    for page in (1, 2):
        try:
            pages.extend(_safe_get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": 250, "page": page, "sparkline": "false"},
            ))
        except Exception:
            continue
    out = {}
    for c in pages:
        sym = (c.get("symbol") or "").lower()
        binance_sym = base_to_binance.get(sym)
        if binance_sym and c.get("market_cap"):
            out[binance_sym] = float(c["market_cap"])
    return out


def filter_universe() -> list[str]:
    """Return mid-cap candidates listed on Binance Futures, ranked by activity/cap."""
    futures_syms = get_futures_universe()
    volumes = get_24h_volumes(futures_syms)
    caps = get_market_caps(futures_syms)

    candidates = []
    for sym in futures_syms:
        vol = volumes.get(sym, 0)
        cap = caps.get(sym, 0)
        if (CFG.MIN_MARKET_CAP_USD <= cap <= CFG.MAX_MARKET_CAP_USD
                and vol >= CFG.MIN_VOLUME_24H_USD):
            candidates.append((sym, cap, vol))
    # Bias toward "active" mid-caps: high volume relative to cap
    candidates.sort(key=lambda x: x[2] / max(x[1], 1), reverse=True)
    return [c[0] for c in candidates[:CFG.UNIVERSE_MAX_CANDIDATES]]


def get_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    raw = _safe_get(f"{_PUBLIC_BASE}/fapi/v1/klines",
                    params={"symbol": symbol, "interval": interval, "limit": limit})
    df = pd.DataFrame(raw, columns=[
        "ot", "open", "high", "low", "close", "volume",
        "ct", "qv", "trades", "tbv", "tqv", "ignore",
    ])
    for c in ("open", "high", "low", "close", "volume", "qv"):
        df[c] = pd.to_numeric(df[c])
    return df


def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1])


def compute_features(symbol: str) -> Features:
    df = get_klines(symbol, "1h", 200)
    closes = df["close"]
    last = float(closes.iloc[-1])
    ret_1h = float(closes.iloc[-1] / closes.iloc[-2] - 1) if len(closes) > 1 else 0.0
    ret_24h = float(closes.iloc[-1] / closes.iloc[-25] - 1) if len(closes) > 24 else 0.0
    ret_7d = float(closes.iloc[-1] / closes.iloc[0] - 1)
    ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
    rsi = _rsi(closes, 14)
    vol_usd = float((df["close"] * df["volume"]).tail(24).sum())
    return Features(
        symbol=symbol, last_price=last,
        ret_1h=ret_1h, ret_24h=ret_24h, ret_7d=ret_7d,
        rsi_14=rsi, ema20=ema20, ema50=ema50,
        above_ema50=last > ema50, volume_24h_usd=vol_usd,
    )


def get_fear_greed() -> dict:
    try:
        data = _safe_get("https://api.alternative.me/fng/", params={"limit": 1})
        d = data["data"][0]
        return {"value": int(d["value"]), "classification": d["value_classification"]}
    except Exception:
        return {"value": 50, "classification": "unknown"}


def get_news_headlines(symbols: list[str]) -> list[dict]:
    """CryptoPanic recent posts. Returns [] if no token configured or on error."""
    if not CFG.CRYPTOPANIC_TOKEN:
        return []
    bases = [s.replace("USDT", "") for s in symbols][:10]
    try:
        data = _safe_get(
            "https://cryptopanic.com/api/v1/posts/",
            params={"auth_token": CFG.CRYPTOPANIC_TOKEN,
                    "currencies": ",".join(bases),
                    "kind": "news",
                    "public": "true"},
        )
        return [
            {"title": p.get("title"),
             "domain": p.get("domain"),
             "currencies": [c.get("code") for c in p.get("currencies", [])]}
            for p in data.get("results", [])[:15]
        ]
    except Exception:
        return []
