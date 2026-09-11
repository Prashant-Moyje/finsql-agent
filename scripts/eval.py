"""Execution-accuracy eval against eval/golden.yaml.

For each question the agent's result set is compared with the gold query's
result set (numbers within tolerance, row order ignored). Also reports
retrieval recall, how often the repair loop rescued a failed first attempt,
and prompt size vs the full schema.

Usage:  python scripts/eval.py [--only rev_total_fy,over_budget] [--summary]
"""
import argparse
import json
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from finsql.catalog import load_catalog  # noqa: E402
from finsql.graph import build_graph  # noqa: E402
from finsql.llm import get_llm  # noqa: E402
from finsql.warehouse import get_warehouse  # noqa: E402

TODAY = date(2026, 9, 12)


def _is_num(v) -> bool:
    return isinstance(v, int | float | Decimal) and not isinstance(v, bool)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.011, 1e-4 * max(abs(a), abs(b)))


def _col_equal(gold: list[float], pred: list) -> bool:
    if not all(_is_num(v) or v is None for v in pred):
        return False
    p = sorted(float(v or 0) for v in pred)
    for scale in (1, 100, 0.01):  # accept a margin as 45.2 or 0.452
        if all(_close(g, x * scale) for g, x in zip(gold, p)):
            return True
    return False


def results_match(gold_cols, gold_rows, pred_cols, pred_rows) -> bool:
    if len(gold_rows) != len(pred_rows):
        return False
    if not gold_rows:
        return True
    unused = set(range(len(pred_cols)))
    for gi in range(len(gold_cols)):
        values = [r[gi] for r in gold_rows]
        if not all(_is_num(v) or v is None for v in values):
            continue  # label column: not compared (see golden.yaml header)
        gold = sorted(float(v or 0) for v in values)
        hit = next((pi for pi in sorted(unused) if _col_equal(gold, [r[pi] for r in pred_rows])), None)
        if hit is None:
            return False
        unused.discard(hit)
    return True


def gold_tables(sql: str) -> set[str]:
    tree = sqlglot.parse_one(sql, dialect="duckdb")
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    return {t.name for t in tree.find_all(exp.Table) if t.name not in ctes}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--summary", action="store_true", help="also run the summarizer (slower)")
    args = ap.parse_args()

    cases = yaml.safe_load((ROOT / "eval" / "golden.yaml").read_text(encoding="utf-8"))
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]

    llm, wh, catalog = get_llm(), get_warehouse(), load_catalog()
    app = build_graph(llm, wh, catalog, with_summary=args.summary)
    print(f"Evaluating {len(cases)} cases with {llm.name} on {catalog['stats']['tables']} tables\n")

    results = []
    for c in cases:
        t0 = time.perf_counter()
        try:
            out = app.invoke({"question": c["question"], "user": "eval", "today": TODAY}, {"recursion_limit": 25})
        except Exception as e:  # an LLM/network failure is a failed case, not a crashed eval
            out = {"status": "error", "report": str(e), "attempts": 0}
        secs = time.perf_counter() - t0
        r = {"id": c["id"], "status": out.get("status"), "attempts": out.get("attempts", 0), "seconds": round(secs, 1),
             "sql": out.get("checked_sql"), "errors": [h["error"] for h in out.get("history", [])]}
        retrieved = set(out["retrieval"].tables) if out.get("retrieval") else set()
        r["schema_tokens"] = out["retrieval"].stats["schema_tokens"] if out.get("retrieval") else None
        if "expect" in c:
            r["kind"] = "behaviour"
            r["pass"] = out.get("status") in c["expect"]
        else:
            r["kind"] = "sql"
            gcols, grows = wh.execute(c["sql"])
            r["pass"] = out.get("status") == "answered" and results_match(gcols, grows, out["columns"], out["rows"])
            need = gold_tables(c["sql"])
            r["retrieval_recall"] = len(need & retrieved) / len(need)
        results.append(r)
        if r["status"] == "error":
            r["errors"] = [out.get("report", "")]
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"{mark}  {c['id']:<22} {r['status']:<9} attempts={r['attempts']} {secs:5.1f}s"
              + (f"  errors: {r['errors']}" if r["errors"] else ""))

    sql_cases = [r for r in results if r["kind"] == "sql"]
    beh = [r for r in results if r["kind"] == "behaviour"]
    passed = [r for r in sql_cases if r["pass"]]
    summary = {
        "model": llm.name,
        "execution_accuracy": f"{len(passed)}/{len(sql_cases)}",
        "first_try_accuracy": f"{sum(r['attempts'] == 1 for r in passed)}/{len(sql_cases)}",
        "rescued_by_repair_loop": sum(r["attempts"] > 1 for r in passed),
        "behaviour_cases": f"{sum(r['pass'] for r in beh)}/{len(beh)}",
        "avg_retrieval_recall": round(sum(r["retrieval_recall"] for r in sql_cases) / max(len(sql_cases), 1), 3),
        "avg_schema_tokens": round(sum(r["schema_tokens"] or 0 for r in results) / max(len(results), 1)),
        "full_schema_tokens": catalog["stats"]["full_schema_tokens"],
        "avg_seconds": round(sum(r["seconds"] for r in results) / max(len(results), 1), 1),
    }
    print("\n" + json.dumps(summary, indent=2))
    out_path = ROOT / "eval" / "results.json"
    out_path.write_text(json.dumps({"summary": summary, "cases": results}, indent=1, default=str), encoding="utf-8")
    print(f"Details: {out_path}")


if __name__ == "__main__":
    main()
