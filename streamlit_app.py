"""Live demo of the FinSQL agent, hosted free on Streamlit Community Cloud.

The warehouse and context store are build artifacts, so they aren't in git: on
first load this seeds the 610-table DuckDB warehouse and runs the schema
pipeline (a few seconds, then cached for the life of the container). Questions
are answered by Groq's free API.
"""
import os
import tempfile
import time
from datetime import date
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent
WORK = Path(tempfile.gettempdir()) / "finsql_demo"
DEMO_TODAY = date(2026, 9, 12)  # the synthetic warehouse ends 2026-08-31

st.set_page_config(page_title="FinSQL: text-to-SQL for finance", page_icon="📊", layout="wide")

MAX_QUESTIONS = 8  # per browser session: the demo runs on a free Groq daily quota

EXAMPLES = [
    "Which departments were over budget last quarter, and by how much?",
    "Net revenue by customer region for FY2026",
    "What is our total overdue receivables balance right now?",
    "Top 5 vendors by approved spend in FY2026",
    "Engineering hosting costs by month in calendar year 2026",
    "Delete all the void invoices",  # refused: read-only by construction
]


def _configure_env() -> None:
    """Secrets and paths must be set before finsql.config is imported."""
    WORK.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DUCKDB_PATH", str(WORK / "finance.duckdb"))
    os.environ.setdefault("CATALOG_PATH", str(WORK / "catalog.json"))
    os.environ.setdefault("SEMANTIC_PATH", str(ROOT / "semantic.yaml"))
    os.environ.setdefault("LLM_PROVIDER", "groq")
    try:
        for key in ("GROQ_API_KEY", "GROQ_MODEL", "LLM_PROVIDER"):
            if key in st.secrets:
                os.environ[key] = str(st.secrets[key])
    except Exception:
        pass  # no secrets file locally; env vars are used instead


@st.cache_resource(show_spinner="Seeding the demo warehouse and running the schema pipeline…")
def bootstrap():
    import sys

    _configure_env()
    sys.path.insert(0, str(ROOT / "scripts"))
    import seed_duckdb

    db = Path(os.environ["DUCKDB_PATH"])
    if not db.exists():
        seed_duckdb.main(["--out", str(db), "--quiet"])

    from finsql.catalog import build_catalog, load_semantic, save_catalog
    from finsql.graph import build_graph
    from finsql.llm import get_llm
    from finsql.warehouse import DuckDBWarehouse

    wh = DuckDBWarehouse(str(db))  # read-only connection, external access disabled
    catalog = build_catalog(wh, load_semantic(os.environ["SEMANTIC_PATH"]))
    save_catalog(catalog, os.environ["CATALOG_PATH"])
    return build_graph(get_llm(), wh, catalog), catalog


def render_result(out: dict, elapsed: float) -> None:
    status = out.get("status", "failed")
    icon = {"answered": "✅", "clarify": "🤔", "refused": "⛔", "failed": "⚠️"}.get(status, "")
    st.markdown(f"#### {icon} {out.get('report', '')}")

    cols = st.columns(4)
    cols[0].metric("Status", status)
    cols[1].metric("Attempts", out.get("attempts", 0))
    cols[2].metric("Seconds", f"{elapsed:.1f}")
    retrieval = out.get("retrieval")
    if retrieval:
        cols[3].metric("Schema tokens sent", f"{retrieval.stats['schema_tokens']:,}",
                       help=f"vs {retrieval.stats['full_schema_tokens']:,} tokens for the whole warehouse")

    if out.get("rows"):
        import pandas as pd

        st.dataframe(pd.DataFrame(out["rows"], columns=out["columns"]), use_container_width=True, hide_index=True)

    if out.get("checked_sql"):
        with st.expander("SQL that ran (validated, row-limited)"):
            st.code(out["checked_sql"], language="sql")

    with st.expander("How the agent got there"):
        for step in out.get("trace", []):
            detail = {k: v for k, v in step.items() if k not in ("step", "ms", "sql")}
            st.markdown(f"**{step['step']}** · {step['ms']} ms")
            if detail:
                st.json(detail, expanded=False)
        if out.get("history"):
            st.markdown("**Repair loop** — the validator rejected these and told the writer why:")
            for h in out["history"]:
                st.code(h["sql"], language="sql")
                st.error(h["error"])


def main() -> None:
    _configure_env()
    st.title("FinSQL: agentic text-to-SQL for financial reporting")
    st.caption("Ask a finance question in plain English. An LLM writes the SQL, deterministic code "
               "validates it against the real schema, and a second agent writes the report.")

    if not os.environ.get("GROQ_API_KEY"):
        st.error("No GROQ_API_KEY configured. Add it under app settings → Secrets, or run locally with a .env file.")
        st.stop()

    app, catalog = bootstrap()
    stats = catalog["stats"]

    with st.sidebar:
        st.header("The warehouse")
        st.metric("Tables", f"{stats['tables']:,}")
        st.metric("Columns", f"{stats['columns']:,}")
        st.metric("Full schema", f"~{stats['full_schema_tokens']:,} tokens")
        st.caption("Far too large for a prompt, so each question retrieves only the tables it needs "
                   "(~1,000 tokens) via metric pinning, BM25 and the foreign-key graph.")
        st.divider()
        st.header("Guardrails")
        st.markdown(
            "- Read-only connection (writes fail at the engine)\n"
            "- One SELECT only, checked on the parsed query, not by regex\n"
            "- Every table and column must exist in the curated catalog\n"
            "- Hard row limit and query timeout\n"
            "- The summarizer has no database access"
        )
        st.caption(f"Synthetic data; 'today' is fixed at {DEMO_TODAY}.")
        st.markdown("[Source on GitHub](https://github.com/Prashant-Moyje/finsql-agent)")

    asked = st.session_state.setdefault("asked", 0)
    st.markdown("**Try one of these:**")
    cols = st.columns(3)
    for i, example in enumerate(EXAMPLES):
        if cols[i % 3].button(example, use_container_width=True, key=f"ex{i}"):
            st.session_state["question"] = example

    question = st.text_input("Your question", key="question", placeholder="e.g. Net revenue by quarter for FY2026")
    run = st.button("Ask", type="primary", disabled=not question)

    if run and question:
        if asked >= MAX_QUESTIONS:
            st.warning(f"Demo limit of {MAX_QUESTIONS} questions per session reached (it runs on a free "
                       "Groq quota). Reload the page to start over.")
            st.stop()
        st.session_state["asked"] = asked + 1
        t0 = time.perf_counter()
        try:
            with st.spinner("Retrieving schema → writing SQL → validating → running → summarising…"):
                out = app.invoke({"question": question, "user": "demo", "today": DEMO_TODAY},
                                 {"recursion_limit": 25})
        except Exception as e:
            st.error(f"{e}")
            st.stop()
        render_result(out, time.perf_counter() - t0)


if __name__ == "__main__":
    main()
