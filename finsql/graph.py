"""The agent workflow as a LangGraph state machine.

    retrieve -> write_sql -> validate -> execute -> summarize -> END
                   ^            |          |
                   +-- error ---+----------+   (repair loop, max MAX_ATTEMPTS)
    write_sql --CLARIFY--> END (asks the user instead of guessing)
    validate --security--> refuse -> END (never retried)
    validate/execute --attempts exhausted--> fail -> END
"""
import operator
import time
from datetime import date
from functools import wraps
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import config, guard
from .agents import date_context, summarize, write_sql
from .llm import LLM
from .retriever import Retrieval, retrieve
from .warehouse import QueryError, Warehouse


class State(TypedDict, total=False):
    question: str
    user: str
    today: date
    retrieval: Retrieval
    attempts: int
    sql: str  # latest SQL from the writer
    checked_sql: str  # the validated, limited SQL that actually ran
    error: str | None
    history: Annotated[list[dict], operator.add]  # failed attempts, fed back to the writer
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    status: str  # answered | clarify | refused | failed
    report: str
    trace: Annotated[list[dict], operator.add]


def _traced(name: str):
    def deco(fn):
        @wraps(fn)
        def inner(state: State) -> dict:
            t0 = time.perf_counter()
            out = fn(state)
            out.setdefault("trace", [])
            out["trace"] = [{"step": name, "ms": round((time.perf_counter() - t0) * 1000),
                             **out.pop("_detail", {})}] + out["trace"]
            return out
        return inner
    return deco


def build_graph(llm: LLM, warehouse: Warehouse, catalog: dict, max_attempts: int = config.MAX_ATTEMPTS,
                max_rows: int = config.MAX_ROWS, with_summary: bool = True):

    @_traced("retrieve")
    def retrieve_node(s: State) -> dict:
        r = retrieve(s["question"], catalog)
        return {"retrieval": r, "attempts": 0, "_detail": {"tables": r.tables, **r.stats}}

    @_traced("write_sql")
    def write_node(s: State) -> dict:
        sql, clarify = write_sql(llm, s["question"], s["retrieval"], catalog, s.get("history", []),
                                 s.get("today") or date.today())
        attempts = s.get("attempts", 0) + 1
        if clarify:
            return {"attempts": attempts, "status": "clarify", "report": clarify, "_detail": {"clarify": clarify}}
        return {"attempts": attempts, "sql": sql, "error": None, "_detail": {"attempt": attempts, "sql": sql}}

    @_traced("validate")
    def validate_node(s: State) -> dict:
        try:
            checked = guard.check(s["sql"], catalog, max_rows)
            warehouse.explain(checked.sql)  # the warehouse compiler is the final judge
        except guard.SecurityViolation as e:
            return {"status": "refused", "error": str(e), "_detail": {"security": str(e)}}
        except (guard.GuardError, QueryError) as e:
            return {"error": str(e), "history": [{"sql": s["sql"], "error": str(e)}], "_detail": {"error": str(e)}}
        return {"checked_sql": checked.sql, "error": None, "_detail": {"ok": True, "tables": checked.tables}}

    @_traced("execute")
    def execute_node(s: State) -> dict:
        try:
            columns, rows = warehouse.execute(s["checked_sql"], tag=s.get("user", ""))
        except QueryError as e:
            return {"error": str(e), "history": [{"sql": s["sql"], "error": str(e)}], "_detail": {"error": str(e)}}
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return {"columns": columns, "rows": rows, "truncated": truncated, "error": None,
                "_detail": {"rows": len(rows), "truncated": truncated}}

    @_traced("summarize")
    def summarize_node(s: State) -> dict:
        if not with_summary:  # eval mode: SQL accuracy only, skip the second LLM call
            return {"report": "", "status": "answered"}
        date_ctx = date_context(s.get("today") or date.today(), catalog.get("fiscal_year_start_month", 1))
        report = summarize(llm, s["question"], s["columns"], s["rows"], s["truncated"],
                           s["retrieval"].metrics, date_ctx)
        return {"report": report, "status": "answered"}

    @_traced("refuse")
    def refuse_node(s: State) -> dict:
        return {"status": "refused", "report": (
            "I can only run read-only reporting queries, so I didn't run this one. "
            f"({s['error']}) For data changes, please contact the data engineering team.")}

    @_traced("fail")
    def fail_node(s: State) -> dict:
        return {"status": "failed", "report": (
            f"I couldn't build a working query after {s['attempts']} attempts. Last error: {s['error']}\n"
            "Try rephrasing with the metric or table you mean, e.g. 'net revenue by month for FY2026'.")}

    def after_write(s: State) -> str:
        return END if s.get("status") == "clarify" else "validate"

    def after_validate(s: State) -> str:
        if s.get("status") == "refused":
            return "refuse"
        if s.get("error"):
            return "write_sql" if s["attempts"] < max_attempts else "fail"
        return "execute"

    def after_execute(s: State) -> str:
        if s.get("error"):
            return "write_sql" if s["attempts"] < max_attempts else "fail"
        return "summarize"

    g = StateGraph(State)
    g.add_node("retrieve", retrieve_node)
    g.add_node("write_sql", write_node)
    g.add_node("validate", validate_node)
    g.add_node("execute", execute_node)
    g.add_node("summarize", summarize_node)
    g.add_node("refuse", refuse_node)
    g.add_node("fail", fail_node)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "write_sql")
    g.add_conditional_edges("write_sql", after_write, ["validate", END])
    g.add_conditional_edges("validate", after_validate, ["execute", "write_sql", "refuse", "fail"])
    g.add_conditional_edges("execute", after_execute, ["summarize", "write_sql", "fail"])
    for terminal in ("summarize", "refuse", "fail"):
        g.add_edge(terminal, END)
    return g.compile()


_app = None


def answer(question: str, user: str = "cli", today: date | None = None) -> dict[str, Any]:
    """Entry point used by the CLI and Slack. Graph and connections are built
    once per process and reused across warm Lambda invocations."""
    global _app
    if _app is None:
        from .catalog import load_catalog
        from .llm import get_llm
        from .warehouse import get_warehouse

        _app = build_graph(get_llm(), get_warehouse(), load_catalog())
    return _app.invoke({"question": question, "user": user, "today": today or date.today()},
                       {"recursion_limit": 25})
