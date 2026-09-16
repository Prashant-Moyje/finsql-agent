"""Gradio demo of the FinSQL agent, for Hugging Face Spaces.

Seeds the 610-table DuckDB warehouse and builds the context store on first
load, then answers questions through Groq's free API. Hosted on Hugging Face
Spaces on ZeroGPU hardware, which requires the Gradio SDK.
No GPU is used - the LLM runs on Groq's servers and DuckDB is CPU-only.
"""
import os
import tempfile
import time
from datetime import date
from pathlib import Path

try:
    import spaces  # present on Hugging Face ZeroGPU hardware; import before gradio
except ImportError:  # running locally
    spaces = None

import gradio as gr
import pandas as pd

ROOT = Path(__file__).parent
WORK = Path(tempfile.gettempdir()) / "finsql_demo"
DEMO_TODAY = date(2026, 9, 12)   # the synthetic warehouse ends 2026-08-31
MAX_QUESTIONS = 8                # per session: the demo runs on a free Groq daily quota

EXAMPLES = [
    "Which departments were over budget last quarter, and by how much?",
    "Net revenue by customer region for FY2026",
    "What is our total overdue receivables balance right now?",
    "Top 5 vendors by approved spend in FY2026",
    "Engineering hosting costs by month in calendar year 2026",
    "Delete all the void invoices",   # refused: read-only by construction
]

STATUS_ICON = {"answered": "✅", "clarify": "🤔", "refused": "⛔", "failed": "⚠️"}

_STATE: dict = {}


def _gpu(fn):
    return spaces.GPU(fn) if spaces else fn


@_gpu
def _zerogpu_startup_check():
    """ZeroGPU refuses to start an app with no @spaces.GPU function. Nothing in
    this app needs a GPU (the LLM runs on Groq, DuckDB is CPU-only), so this
    no-op exists only to pass that check. It is never called, so it uses no
    GPU quota."""
    return None


def _configure_env() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DUCKDB_PATH", str(WORK / "finance.duckdb"))
    os.environ.setdefault("CATALOG_PATH", str(WORK / "catalog.json"))
    os.environ.setdefault("SEMANTIC_PATH", str(ROOT / "semantic.yaml"))
    os.environ.setdefault("LLM_PROVIDER", "groq")


def bootstrap():
    """Seed the warehouse and build the graph once per container."""
    if _STATE:
        return _STATE["app"], _STATE["catalog"]
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

    if os.environ.get("LLM_PROVIDER") == "groq" and not os.environ.get("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY is not set. On Hugging Face, add it under "
                           "Settings -> Variables and secrets, then restart the Space.")

    wh = DuckDBWarehouse(str(db))  # read-only, external access disabled
    catalog = build_catalog(wh, load_semantic(os.environ["SEMANTIC_PATH"]))
    save_catalog(catalog, os.environ["CATALOG_PATH"])
    _STATE.update(app=build_graph(get_llm(), wh, catalog), catalog=catalog)
    return _STATE["app"], _STATE["catalog"]


def warehouse_blurb() -> str:
    try:
        _, catalog = bootstrap()
    except Exception as e:
        return f"⚠️ {e}"
    s = catalog["stats"]
    return (f"**The warehouse:** {s['tables']:,} tables · {s['columns']:,} columns · "
            f"about {s['full_schema_tokens']:,} tokens of schema — far more than fits in a prompt, so each "
            f"question retrieves only the tables it needs (about 1,000 tokens) via metric pinning, BM25 and "
            f"the foreign-key graph.")


def ask(question: str, asked: int):
    question = (question or "").strip()
    if not question:
        return "Ask a finance question, or pick one of the examples.", "", gr.update(value=None, visible=False), "", {}, asked
    if asked >= MAX_QUESTIONS:
        return (f"⚠️ Demo limit of {MAX_QUESTIONS} questions per session reached "
                "(it runs on a free Groq quota). Reload the page to start over."), "", gr.update(value=None, visible=False), "", {}, asked

    t0 = time.perf_counter()
    try:
        app, _ = bootstrap()
        out = app.invoke({"question": question, "user": "hf-demo", "today": DEMO_TODAY},
                         {"recursion_limit": 25})
    except Exception as e:  # rate limits, network, etc.
        return f"⚠️ {e}", "", gr.update(value=None, visible=False), "", {}, asked
    elapsed = time.perf_counter() - t0

    status = out.get("status", "failed")
    report = f"{STATUS_ICON.get(status, '')} {out.get('report', '')}"
    retrieval = out.get("retrieval")
    tokens = retrieval.stats["schema_tokens"] if retrieval else 0
    full = retrieval.stats["full_schema_tokens"] if retrieval else 0
    meta = (f"**{status}** · {out.get('attempts', 0)} attempt(s) · {elapsed:.1f}s · "
            f"**{tokens:,}** schema tokens sent (of about {full:,} in the full schema)")
    rows, columns = out.get("rows"), out.get("columns")
    frame = pd.DataFrame(rows, columns=columns) if rows else None
    trace = {t["step"]: {k: v for k, v in t.items() if k not in ("step", "sql")} for t in out.get("trace", [])}
    return report, meta, gr.update(value=frame, visible=frame is not None), out.get("checked_sql", ""), trace, asked + 1


with gr.Blocks(title="FinSQL: text-to-SQL for finance", theme=gr.themes.Soft()) as demo:
    gr.Markdown(
        "# FinSQL: agentic text-to-SQL for financial reporting\n"
        "Ask a finance question in plain English. An LLM writes the SQL, deterministic code validates it "
        "against the real schema, and a second agent writes the report. "
        "[Source on GitHub](https://github.com/Prashant-Moyje/finsql-agent)"
    )
    blurb = gr.Markdown()
    with gr.Row():
        question = gr.Textbox(label="Your question", scale=4,
                              placeholder="e.g. Net revenue by quarter for FY2026")
        submit = gr.Button("Ask", variant="primary", scale=1)
    gr.Examples(examples=EXAMPLES, inputs=question, label="Try one of these")

    report = gr.Markdown(label="Report")
    meta = gr.Markdown()
    table = gr.Dataframe(label="Result", interactive=False, wrap=True, visible=False)
    with gr.Accordion("SQL that ran (validated, row-limited)", open=False):
        sql = gr.Code(language="sql")
    with gr.Accordion("How the agent got there", open=False):
        trace = gr.JSON()

    gr.Markdown(
        "**Guardrails:** read-only connection (writes fail at the engine) · one SELECT only, checked on the "
        "parsed query rather than by regex · every table and column must exist in the curated catalog · hard "
        "row limit and query timeout · the summarizer has no database access.\n\n"
        f"*Synthetic data; 'today' is fixed at {DEMO_TODAY}.*"
    )

    asked = gr.State(0)
    outputs = [report, meta, table, sql, trace, asked]
    submit.click(ask, inputs=[question, asked], outputs=outputs)
    question.submit(ask, inputs=[question, asked], outputs=outputs)
    demo.load(warehouse_blurb, outputs=blurb)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)))
