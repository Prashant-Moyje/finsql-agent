"""Ask FinSQL a question from the terminal.

Usage:  python scripts/ask.py "What was net revenue by region in FY2026?" [-v] [--today 2026-09-12]
"""
import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from finsql.agents import result_table  # noqa: E402
from finsql.graph import answer  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every agent step")
    ap.add_argument("--today", type=date.fromisoformat, help="pretend today is this date")
    args = ap.parse_args()

    t0 = time.perf_counter()
    out = answer(args.question, user="cli", today=args.today)
    elapsed = time.perf_counter() - t0

    if args.verbose:
        print("=== trace ===")
        for step in out.get("trace", []):
            detail = {k: v for k, v in step.items() if k not in ("step", "ms")}
            print(f"[{step['step']:<9} {step['ms']:>6} ms] {detail}")
        print()
    if out.get("checked_sql"):
        print("=== SQL ===\n" + out["checked_sql"] + "\n")
    if out.get("rows"):
        print("=== result (first 10 rows) ===\n" + result_table(out["columns"], out["rows"], limit=10) + "\n")
    print(f"=== report [{out.get('status')}, {out.get('attempts', 0)} attempt(s), {elapsed:.1f}s] ===")
    print(out.get("report", ""))


if __name__ == "__main__":
    main()
