"""The two LLM agents: SQL writer and report summarizer.

The third role, the tester, is deliberately not an LLM: see guard.py. An LLM
asked "is this SQL valid?" can say yes when it isn't; a parser and the
warehouse compiler can't.

The summarizer never gets tools or a database connection. Warehouse data is
untrusted input (a customer name could contain "ignore previous instructions"),
so the model that reads it can only produce text.
"""
import re
from datetime import date
from decimal import Decimal

from .llm import LLM
from .retriever import Retrieval

WRITER_SYSTEM = """You are a senior analytics engineer writing {dialect} SQL for a finance team.
Rules:
1. Use ONLY tables and columns listed in SCHEMA. Never invent a table or column name.
2. Write exactly one read-only SELECT statement (CTEs are fine). Never write INSERT/UPDATE/DELETE/DDL.
3. Follow BUSINESS RULES and METRIC DEFINITIONS exactly; they override your own assumptions.
4. Give every table an alias and prefix every column with it.
5. When filtering text columns, use the exact values listed in the schema (they are case-sensitive).
6. Use readable column aliases, round money to 2 decimals, ORDER BY something meaningful.
7. If the question cannot be answered with this schema, or is not a data question, reply with one line:
   CLARIFY: <a short question for the user>
Otherwise reply with only the SQL in a single ```sql code block, no explanation."""

SUMMARIZER_SYSTEM = """You are a financial analyst writing a short Slack report for a finance team.
- Use ONLY numbers that appear in RESULT or COLUMN STATS. Never estimate, extrapolate or invent figures.
- If RESULT is empty, say that no data matched and suggest a likely reason.
- Format with Slack mrkdwn:
  line 1: *a bold one-sentence direct answer*
  then 2-4 bullets starting with "•" covering key figures, trends or outliers
  last line: _Definitions: which metric definitions and filters were applied_
- Money as $1,234,567 (or $1.23M for millions). Under 130 words. No preamble."""


def fiscal_quarter(today: date, start_month: int) -> tuple[int, int]:
    """(fiscal_year, fiscal_quarter); fiscal years are named by the year they end in."""
    fy = today.year + 1 if start_month > 1 and today.month >= start_month else today.year
    fm = (today.month - start_month) % 12 + 1
    return fy, (fm - 1) // 3 + 1


def date_context(today: date, start_month: int) -> str:
    """Relative dates are resolved here, in code, and handed to the writer as
    literals. Left to the LLM, "last quarter" became a runtime lookup that
    returned nothing (found by the eval)."""
    fy, q = fiscal_quarter(today, start_month)
    pfy, pq = (fy, q - 1) if q > 1 else (fy - 1, 4)
    return (f"TODAY: {today.isoformat()}. Current fiscal quarter: FY{fy}-Q{q} (fiscal_year = {fy}, fiscal_quarter = {q}). "
            f"Last quarter: FY{pfy}-Q{pq} (fiscal_year = {pfy}, fiscal_quarter = {pq}). "
            f"This fiscal year / YTD: fiscal_year = {fy}. Last fiscal year: fiscal_year = {fy - 1}.\n"
            "Use these literal values in filters; do not derive the current period inside the SQL.")


def build_writer_prompt(question: str, r: Retrieval, catalog: dict, history: list[dict], today: date) -> str:
    parts = [
        date_context(today, catalog.get("fiscal_year_start_month", 1)),
        "SCHEMA:\n" + r.schema_text,
    ]
    if catalog.get("rules"):
        parts.append("BUSINESS RULES:\n" + "\n".join(f"- {x}" for x in catalog["rules"]))
    if r.metrics:
        parts.append("METRIC DEFINITIONS:\n" + "\n".join(f"- {m['name']}: {m['definition']}" for m in r.metrics))
    if r.examples:
        parts.append("VERIFIED EXAMPLES:\n" + "\n\n".join(
            f"Q: {e['question']}\n```sql\n{e['sql'].strip()}\n```" for e in r.examples))
    parts.append(f"QUESTION: {question}")
    for h in history[-2:]:  # only the latest failures: keeps retries inside the context budget
        parts.append(f"YOUR PREVIOUS SQL FAILED:\n```sql\n{h['sql']}\n```\nERROR: {h['error']}\n"
                     "Fix it. Use only tables and columns from SCHEMA.")
    return "\n\n".join(parts)


def write_sql(llm: LLM, question: str, r: Retrieval, catalog: dict, history: list[dict],
              today: date) -> tuple[str | None, str | None]:
    """Returns (sql, None) or (None, clarifying_question)."""
    text = llm.complete(WRITER_SYSTEM.format(dialect=catalog["dialect"]),
                        build_writer_prompt(question, r, catalog, history, today))
    return parse_writer_output(text)


def parse_writer_output(text: str) -> tuple[str | None, str | None]:
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    if m and m.group(1).strip():
        return m.group(1).strip().rstrip(";"), None
    m = re.search(r"CLARIFY:\s*(.+)", text, re.I | re.S)
    if m:
        return None, m.group(1).strip()
    if re.match(r"\s*(SELECT|WITH)\b", text, re.I):
        return text.strip().rstrip(";"), None
    return None, "Could you rephrase that as a question about the finance data (revenue, spend, budget, receivables)?"


def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float | Decimal):
        return f"{v:,.2f}"
    return str(v)


def profile(columns: list[str], rows: list[tuple]) -> str:
    """Column stats over *all* rows, so the summary can speak to totals and
    extremes even though only the first rows are shown to the model."""
    lines = []
    for i, c in enumerate(columns):
        vals = [r[i] for r in rows if r[i] is not None]
        nums = [float(v) for v in vals if isinstance(v, int | float | Decimal) and not isinstance(v, bool)]
        if nums and len(nums) == len(vals):
            lines.append(f"- {c}: min {min(nums):,.2f}, max {max(nums):,.2f}, sum {sum(nums):,.2f}, "
                         f"avg {sum(nums) / len(nums):,.2f}")
        else:
            lines.append(f"- {c}: {len(set(map(str, vals)))} distinct values")
    return "\n".join(lines)


def result_table(columns: list[str], rows: list[tuple], limit: int = 25) -> str:
    out = [" | ".join(columns)] + [" | ".join(fmt(v) for v in r) for r in rows[:limit]]
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more rows")
    return "\n".join(out)


def summarize(llm: LLM, question: str, columns: list[str], rows: list[tuple], truncated: bool,
              metrics: list[dict], date_ctx: str = "") -> str:
    defs = "\n".join(f"- {m['name']}: {m['definition']}" for m in metrics) or "- (none matched)"
    # The same fiscal context the writer received: without it the summary
    # mislabels periods ("Q4 budgets" for what the SQL filtered as FY2027-Q1).
    user = (f"{date_ctx}\n\n" if date_ctx else "") + (f"QUESTION: {question}\n\nMETRIC DEFINITIONS USED:\n{defs}\n\n"
            f"ROW COUNT: {len(rows)}{' (truncated at the row limit)' if truncated else ''}\n\n"
            f"COLUMN STATS:\n{profile(columns, rows)}\n\nRESULT:\n{result_table(columns, rows)}")
    return llm.complete(SUMMARIZER_SYSTEM, user)
