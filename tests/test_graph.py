"""The LangGraph workflow with a scripted LLM: tests routing, the repair loop
and the refusal path without calling a real model."""
from datetime import date

from finsql.graph import build_graph

TODAY = date(2026, 9, 12)
GOOD_SQL = "```sql\nSELECT c.segment, COUNT(*) AS customers FROM dim_customer c GROUP BY c.segment\n```"


def run(llm, warehouse, catalog, question="How many customers per segment?"):
    app = build_graph(llm, warehouse, catalog)
    return app.invoke({"question": question, "user": "U123", "today": TODAY})


def test_happy_path(scripted, warehouse, catalog):
    llm = scripted(GOOD_SQL, "*3 segments.*")
    out = run(llm, warehouse, catalog)
    assert out["status"] == "answered" and out["attempts"] == 1
    assert len(out["rows"]) == 3 and out["report"] == "*3 segments.*"
    assert [t["step"] for t in out["trace"]] == ["retrieve", "write_sql", "validate", "execute", "summarize"]


def test_hallucinated_table_is_repaired_via_feedback(scripted, warehouse, catalog):
    llm = scripted("```sql\nSELECT segment, COUNT(*) FROM customers GROUP BY segment\n```", GOOD_SQL, "ok")
    out = run(llm, warehouse, catalog)
    assert out["status"] == "answered" and out["attempts"] == 2
    assert "does not exist" in out["history"][0]["error"]
    retry_prompt = llm.prompts[1][1]
    assert "YOUR PREVIOUS SQL FAILED" in retry_prompt and "dim_customer" in retry_prompt


def test_destructive_sql_is_refused_not_retried(scripted, warehouse, catalog):
    llm = scripted("```sql\nDELETE FROM fct_invoices WHERE status = 'void'\n```")
    out = run(llm, warehouse, catalog, "Delete all void invoices")
    assert out["status"] == "refused" and out["attempts"] == 1
    assert "rows" not in out and len(llm.prompts) == 1


def test_gives_up_after_max_attempts(scripted, warehouse, catalog):
    bad = "```sql\nSELECT x.nope FROM made_up_table x\n```"
    out = run(scripted(bad, bad, bad), warehouse, catalog)
    assert out["status"] == "failed" and out["attempts"] == 3
    assert "made_up_table" in out["report"] or "does not exist" in out["report"]


def test_clarify_path(scripted, warehouse, catalog):
    out = run(scripted("CLARIFY: Which fiscal year do you mean?"), warehouse, catalog, "revenue?")
    assert out["status"] == "clarify" and out["report"] == "Which fiscal year do you mean?"


def test_prompt_contains_semantic_context(scripted, warehouse, catalog):
    llm = scripted(GOOD_SQL, "ok")
    run(llm, warehouse, catalog, "What was net revenue last quarter?")
    prompt = llm.prompts[0][1]
    assert "FY2027-Q2" in prompt  # today's fiscal period, from fiscal_year_start_month
    assert "net_revenue:" in prompt and "status <> 'void'" in prompt
    assert "VERIFIED EXAMPLES" in prompt
