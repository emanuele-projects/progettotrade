"""Trading bot configuration. Every tunable parameter lives here."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent


# Per-strategy capital allocation. Sum = total simulated portfolio.
# - aggressive: places real orders on Binance Futures testnet (Claude-driven mid-cap)
# - others: paper-tracked on real prices (mathematically equivalent to real orders
#   for comparison purposes, without polluting one testnet wallet with 4 overlapping
#   strategies)
STRATEGY_ALLOCATIONS = {
    "aggressive": 2500.0,
    "hodl": 2500.0,
    "dca": 2500.0,
    "conservative_2x": 2500.0,
}
TOTAL_CAPITAL_USDT = sum(STRATEGY_ALLOCATIONS.values())

# 5-crypto blue-chip portfolio used by HODL / DCA / Conservative strategies.
# Aggressive picks dynamically from the mid-cap universe via Claude.
BLUE_CHIP_PORTFOLIO = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]


@dataclass(frozen=True)
class Config:
    # ---- Capital ----
    # Per-strategy budget the aggressive bot uses for sizing. Other strategies use their
    # respective entries in STRATEGY_ALLOCATIONS.
    INITIAL_CAPITAL_USDT: float = STRATEGY_ALLOCATIONS["aggressive"]
    TOTAL_CAPITAL_USDT: float = TOTAL_CAPITAL_USDT

    # ---- Leverage & sizing (operator-chosen aggressive profile) ----
    LEVERAGE: int = 10
    MARGIN_TYPE: str = "ISOLATED"
    INITIAL_DEPLOY_PCT: float = 0.50         # 50% margin deployed initially
    RESERVE_FOR_AVERAGING_PCT: float = 0.50  # 50% kept liquid for martingale
    MAX_CONCURRENT_POSITIONS: int = 5
    POSITION_MARGIN_PCT: float = 0.10        # 10% per initial entry × 5 = 50%

    # ---- Martingale (averaging down on losers) ----
    MARTINGALE_TRIGGER_DRAWDOWN_PCT: float = -0.05  # add at -5% on collateral
    MARTINGALE_ADD_RATIO: float = 0.50              # add 50% of current margin per step
    MARTINGALE_MAX_LEVELS: int = 3                  # max 3 averages per position

    # ---- Hard exits ----
    HARD_STOP_LOSS_PCT: float = -0.30  # absolute hard cut on collateral
    TAKE_PROFIT_PCT: float = 0.10
    COOLDOWN_HOURS_AFTER_LIQUIDATION: int = 6

    # ---- Universe filtering ----
    MIN_MARKET_CAP_USD: float = 200_000_000
    MAX_MARKET_CAP_USD: float = 2_000_000_000
    MIN_VOLUME_24H_USD: float = 50_000_000
    UNIVERSE_REFRESH_HOURS: int = 6
    UNIVERSE_MAX_CANDIDATES: int = 15

    # ---- Loop ----
    LOOP_INTERVAL_SECONDS: int = 30 * 60

    # ---- Emergency rail (instrumentation, not strategy block) ----
    EQUITY_FLOOR_PCT: float = 0.20  # auto-halt if equity < 20% of initial

    # ---- Mode ----
    USE_TESTNET: bool = True
    DRY_RUN: bool = False  # if True, log decisions but don't even submit to testnet

    # ---- API keys (read from .env) ----
    BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    BINANCE_API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CRYPTOPANIC_TOKEN: str = os.getenv("CRYPTOPANIC_TOKEN", "")  # optional

    # ---- Models ----
    CLAUDE_MODEL: str = "claude-sonnet-4-6"
    CLAUDE_MAX_TOKENS: int = 2000

    # ---- Paths ----
    DATA_DIR: Path = ROOT / "data"
    JOURNAL_DB: Path = ROOT / "data" / "journal.db"
    LOG_FILE: Path = ROOT / "data" / "bot.log"
    KILL_SWITCH: Path = ROOT / "KILL_SWITCH"


CFG = Config()
CFG.DATA_DIR.mkdir(parents=True, exist_ok=True)
