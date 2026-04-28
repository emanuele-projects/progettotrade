"""Quick post-mortem: print equity curves for live testnet bot vs shadow benchmarks."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CFG  # noqa: E402
import journal  # noqa: E402


def fmt_change(start: float, end: float) -> str:
    pct = (end - start) / start * 100 if start else 0.0
    sign = "+" if pct >= 0 else ""
    return f"{end:>12,.2f} ({sign}{pct:.2f}%)"


def main() -> None:
    journal.init()
    sources = ["live", "shadow_hodl", "shadow_dca", "shadow_lowlev"]

    print("\n" + "=" * 72)
    print("Trading Bot Report")
    print("=" * 72)
    print(f"Initial capital: {CFG.INITIAL_CAPITAL_USDT:,.2f} USDT (paper)\n")

    print(f"{'Strategy':<18} {'First':>14} {'Last':>22} {'Samples':>10}")
    print("-" * 72)
    for src in sources:
        curve = journal.equity_curve(source=src)
        if not curve:
            print(f"{src:<18} {'(no data)':>14}")
            continue
        first = curve[0][1]
        last = curve[-1][1]
        print(f"{src:<18} {first:>14,.2f} {fmt_change(first, last):>22} {len(curve):>10}")

    print()


if __name__ == "__main__":
    main()
