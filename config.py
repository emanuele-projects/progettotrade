"""Trading bot configuration. Every tunable parameter lives here."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent


# Single-strategy mode: all capital concentrated on the Claude-driven aggressive
# bot. The shadow benchmark slots exist in the dict for legacy compatibility but
# are zeroed out — main.py / shadow.py / dashboard.py skip them when 0.
# 2026-07-15: full RESET to the ACTUAL post-flatten testnet balance ($3,912.89) —
# all positions closed flat, journal archived to journal_backup_pre_reset.db, P&L
# baseline restarts here for a clean, token-save, self-correcting 7-day run.
STRATEGY_ALLOCATIONS = {
    "aggressive": 3912.89,
    "hodl": 0.0,
    "dca": 0.0,
    "conservative_2x": 0.0,
}
TOTAL_CAPITAL_USDT = sum(STRATEGY_ALLOCATIONS.values())

# Always-included anchors: large-cap names that the bot will *always* see in the
# candidate set, alongside the dynamic mid-cap shortlist. Lets Claude balance
# the portfolio between high-vol mid-caps and steadier blue-chips.
LARGE_CAP_ANCHORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

# Legacy alias used by shadow.py only — kept to avoid breaking imports.
BLUE_CHIP_PORTFOLIO = LARGE_CAP_ANCHORS


@dataclass(frozen=True)
class Config:
    # ---- Capital ----
    # Per-strategy budget the aggressive bot uses for sizing. Other strategies use their
    # respective entries in STRATEGY_ALLOCATIONS.
    INITIAL_CAPITAL_USDT: float = STRATEGY_ALLOCATIONS["aggressive"]
    TOTAL_CAPITAL_USDT: float = TOTAL_CAPITAL_USDT
    # Clean-run start: the 2026-07-15 reset flattened every position and rebased
    # capital to $3912.89 AFTER the flatten. Binance income history is NOT reset,
    # so it still holds the contaminated pre-reset trades (incl. the −$1141 from
    # the double-instance bug). Anything that reports "since reset" — per-symbol
    # P&L, drawdown, concentration — must filter realized events to ≥ this epoch.
    # (Boundary sits after the 18:15 flatten cluster, before the first real trade.)
    RESET_TS_MS: int = 1784142000000  # 2026-07-15 19:00:00 UTC

    # ---- Leverage & sizing (operator-chosen aggressive profile) ----
    LEVERAGE: int = 10
    MARGIN_TYPE: str = "ISOLATED"
    INITIAL_DEPLOY_PCT: float = 0.50         # 50% margin deployed initially
    RESERVE_FOR_AVERAGING_PCT: float = 0.50  # 50% kept liquid for martingale
    MAX_CONCURRENT_POSITIONS: int = 14       # portfolio mode: min 10, target 12, cap 14
    POSITION_MARGIN_PCT: float = 0.05        # 5% per entry × 12 target = ~60% margin deployed

    # ---- Leverage v3 (5x-20x, per-trade, agent-chosen) ----
    ALLOWED_LEVERAGES: tuple[int, ...] = (5, 10, 15, 20)
    MAX_LEVERAGE: int = 20
    LEVERAGE_BRACKET_REFRESH_HOURS: int = 24  # per-symbol max-leverage cache TTL

    # ---- Always-invested portfolio (2026-07-15, user mandate) ----
    # The book must hold at least MIN_OPEN_POSITIONS at all times, aiming for
    # TARGET_OPEN_POSITIONS. Caution is expressed through leverage/stops and
    # long-short balance, not by sitting flat. When a server-side SL/TP fill
    # empties a slot, the user-stream handler emits a risk_exit trigger and the
    # next (batched) Claude call refills strategically.
    MIN_OPEN_POSITIONS: int = 10
    TARGET_OPEN_POSITIONS: int = 12

    # ---- Server-side protection (exchange-held SL/TP orders) ----
    # Place STOP_MARKET + TAKE_PROFIT_MARKET (closePosition) on the exchange at
    # entry: exits fire even if this process is down, and the local risk engine
    # demotes to backstop (liq-guard + fallback when order placement failed).
    SERVER_SIDE_PROTECTION_ON_TESTNET: bool = True

    # ---- Real-time risk engine ----
    RISK_LIQ_GUARD_FRACTION: float = 0.75     # force-close at 75% of est. liquidation distance
    RISK_MMR_ESTIMATE: float = 0.005          # conservative maintenance-margin-rate estimate
    RISK_SL_MAX_FRACTION_OF_LIQ: float = 0.60 # clamp: SL price distance ≤ 60% of liq distance
    RISK_MIN_CLOSE_INTERVAL_SECONDS: int = 5  # per-symbol latch against duplicate closes
    WS_STALE_SECONDS: int = 30                # no tick for this long → watchdog kicks in
    WS_REST_FALLBACK_POLL_SECONDS: int = 5    # REST price polling while streams are down
    RECONCILE_INTERVAL_SECONDS: int = 300     # periodic PositionCache vs REST reconciliation

    # ---- Exposure guard ----
    # Σ(margin × leverage) across open positions, ~6× the capital: with $4,991
    # → 10 pos × ~$250 × 20x would be ~50k unchecked; cap at 30k.
    MAX_TOTAL_NOTIONAL_USDT: float = 30_000.0

    # ---- Martingale (averaging down on losers) ----
    # DISABLED 2026-07-14: on live data it added 14× onto losing positions and
    # amplified the drawdown. A bad trade now stays small and hits its stop.
    MARTINGALE_ENABLED: bool = False
    MARTINGALE_TRIGGER_DRAWDOWN_PCT: float = -0.05  # add at -5% on collateral (legacy cycle check; superseded by MARTINGALE_TRIGGER_ROE in the risk engine)
    MARTINGALE_ADD_RATIO: float = 0.50              # add 50% of current margin per step
    MARTINGALE_MAX_LEVELS: int = 2                  # max averages per position (was 3 pre-v3)

    # ---- Martingale v3 (risk-engine enforcement; replaces the cycle check in Phase 3) ----
    MARTINGALE_MAX_LEVERAGE: int = 10               # no averaging-down above 10x
    MARTINGALE_TRIGGER_ROE: float = -0.15           # add at -15% ROE (was -5%: noise at 10x+)
    MARTINGALE_MIN_INTERVAL_SECONDS: int = 1800     # ≥30 min between adds on the same position

    # ---- Hard exits ----
    HARD_STOP_LOSS_PCT: float = -0.30  # absolute hard cut on collateral
    TAKE_PROFIT_PCT: float = 0.10
    COOLDOWN_HOURS_AFTER_LIQUIDATION: int = 6

    # ---- Universe filtering ----
    # Intraday-momentum profile (2026-07-14): rank by 24h price MOVE, not by
    # mid-cap size — we want the coins that are actually moving today. A liquidity
    # floor keeps out illiquid pump-and-dumps the risk engine couldn't exit.
    UNIVERSE_MODE: str = "movers"             # "movers" | "midcap" (legacy)
    MIN_VOLUME_24H_USD: float = 40_000_000    # liquidity floor for movers
    MOVER_MIN_ABS_CHANGE_24H: float = 0.04    # only coins that moved ≥4% in 24h
    MOVER_MAX_ABS_CHANGE_24H: float = 0.60    # skip already-blown-off >60% pumps
    MIN_MARKET_CAP_USD: float = 200_000_000   # (legacy midcap mode only)
    MAX_MARKET_CAP_USD: float = 2_000_000_000 # (legacy midcap mode only)
    UNIVERSE_REFRESH_HOURS: int = 6
    UNIVERSE_MAX_CANDIDATES: int = 18          # token-save: fewer candidates per prompt

    # ---- Loop ----
    LOOP_INTERVAL_SECONDS: int = 15 * 60  # legacy fixed cycle; Phase 5 switches to BASELINE_INTERVAL_SECONDS

    # ---- Claude call policy (event-driven agent) ----
    # TOKEN-SAVE profile (2026-07-15, for a low-cost 7-day run): baseline every 4h
    # as safety net; the free scanner drives the fast decisions on real breakouts.
    # Event cap kept low so a week of running stays ~$1-1.5/day.
    BASELINE_INTERVAL_SECONDS: int = 4 * 60 * 60  # 4h full-book review
    BASELINE_SKIP_IF_CALLED_WITHIN: int = 1200  # skip baseline if a Claude call ran in the last 20 min
    EVENT_DEBOUNCE_SECONDS: int = 90           # collect triggers for this long before calling
    EVENT_MIN_CALL_INTERVAL_SECONDS: int = 300 # ≥5 min gap between any two Claude calls
    EVENT_MAX_CALLS_PER_HOUR: int = 4          # token bucket for event-triggered calls
    # Raw price-move trigger from the WS layer: kept only for EXTREME fast moves
    # (the scanner is the primary, qualified trigger source now).
    EVENT_PRICE_MOVE_PCT_HELD: float = 0.05       # 5% move in window on a held symbol
    EVENT_PRICE_MOVE_PCT_WATCHLIST: float = 0.07  # 7% move in window on a watchlist symbol
    EVENT_PRICE_MOVE_WINDOW_SECONDS: int = 900
    EVENT_FUNDING_ABS_THRESHOLD: float = 0.0008   # |funding| > 0.08% (or sign flip) on held

    # ---- Local signal scanner (free: pure Python over REST/WS data) ----
    SCANNER_ENABLED: bool = True
    SCANNER_INTERVAL_SECONDS: int = 600        # re-scan watchlist ∪ positions every 10 min
    SIGNAL_DEBOUNCE_SECONDS: int = 7200        # don't re-fire the same signal on a symbol within 2h
    # Technical-signal thresholds (transitions vs the previous scan, not levels):
    SIGNAL_RSI_OVERBOUGHT: float = 70.0        # RSI_4h crossing down out of this = short-interest signal
    SIGNAL_RSI_OVERSOLD: float = 30.0          # RSI_4h crossing up out of this = long-interest signal
    SIGNAL_MOMENTUM_1H: float = 0.05           # |ret_1h| ≥ 5% = notable impulse (token-save: only bigger moves)
    SIGNAL_BREAKOUT_DIST_30D: float = 0.01     # within 1% of the 30d high/low = breakout watch
    # Macro-regime triggers:
    SIGNAL_MACRO_ENABLED: bool = True          # F&G zone change or BTC EMA50 flip → full-book review

    # ---- News trigger (CryptoPanic) ----
    NEWS_TRIGGER_ENABLED: bool = True          # needs CRYPTOPANIC_TOKEN in .env, else auto-disabled
    NEWS_POLL_SECONDS: int = 600               # poll headlines every 10 min

    # ---- Long-term memory (reflection loop — see memory.py) ----
    # The bot runs for months; the API is stateless. Once a day it re-reads its
    # own decisions + realized outcomes and rewrites a small, durable lesson set
    # that gets injected (with a free per-symbol track record) into every future
    # decision. Bounded in size so it stays token-cheap; one paid call/day.
    MEMORY_ENABLED: bool = True
    MEMORY_LOOKBACK_DAYS: int = 30       # window for the per-symbol track record
    MEMORY_MAX_LESSONS: int = 12         # hard cap on the active lesson set
    MEMORY_SYMBOL_TOP_N: int = 12        # per-symbol lines injected on baseline calls
    REFLECT_ENABLED: bool = True
    REFLECT_INTERVAL_SECONDS: int = 24 * 60 * 60  # distill lessons once a day
    REFLECT_MAX_TOKENS: int = 1200       # the reflection output is a short list
    REFLECT_DECISIONS_SAMPLE: int = 12   # recent market views fed to the reflection

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
    # 2026-07-15: switched to Opus 4.8 (user request: more capable model).
    # Only 1.67x Sonnet 4.6 per token ($5/$25 vs $3/$15); with the token-save
    # call policy the daily cost stays ~$1.3-1.6. Cache still works (min
    # cacheable prefix 4096 tokens on Opus, our prefix is ~4.1k+). NO thinking
    # param: omitted = off on Opus 4.8, and forced tool_choice requires it off.
    CLAUDE_MODEL: str = "claude-opus-4-8"
    # Opus 4.7/4.8 tokenizer counts ~20-30% more tokens for the same text than
    # Sonnet 4.6: baseline outputs measured ~3.3k would risk truncation at 4000
    # (the same failure mode seen 2026-07-12 at 2000). 6000 gives headroom.
    CLAUDE_MAX_TOKENS: int = 6000

    # ---- Decision source ----
    # "api"  = call the Anthropic API directly (needs credits on ANTHROPIC_API_KEY)
    # "file" = write data/decision_request.json and wait for an external decider
    #          (a Claude Code session running /loop) to write decision_response.json.
    DECISION_SOURCE: str = os.getenv("DECISION_SOURCE", "api")
    FILE_DECISION_TIMEOUT_SECONDS: int = 300  # must exceed the /loop interval
    DECISION_REQUEST_FILE: Path = ROOT / "data" / "decision_request.json"
    DECISION_RESPONSE_FILE: Path = ROOT / "data" / "decision_response.json"

    # ---- Paths ----
    DATA_DIR: Path = ROOT / "data"
    JOURNAL_DB: Path = ROOT / "data" / "journal.db"
    LOG_FILE: Path = ROOT / "data" / "bot.log"
    KILL_SWITCH: Path = ROOT / "KILL_SWITCH"


CFG = Config()
CFG.DATA_DIR.mkdir(parents=True, exist_ok=True)
