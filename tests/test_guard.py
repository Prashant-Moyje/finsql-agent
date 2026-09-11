import pytest

from finsql import guard

DESTRUCTIVE = [
    "DELETE FROM fct_invoices",
    "DROP TABLE fct_invoices",
    "UPDATE fct_invoices SET status = 'paid'",
    "INSERT INTO fct_refunds VALUES (1, 1, DATE '2026-01-01', 1, 'x')",
    "TRUNCATE TABLE fct_budget",
    "ALTER TABLE fct_invoices ADD COLUMN x INT",
    "CREATE TABLE evil AS SELECT * FROM fct_invoices",
    "GRANT SELECT ON fct_invoices TO ROLE public",
    "MERGE INTO fct_budget b USING fct_budget s ON b.budget_id = s.budget_id WHEN MATCHED THEN DELETE",
    "SELECT 1; DROP TABLE fct_invoices",
    "SELECT * FROM fct_invoices; DELETE FROM fct_invoices",
    "WITH x AS (DELETE FROM fct_invoices RETURNING *) SELECT * FROM x",
    "SELECT * INTO backup_copy FROM fct_invoices",
    "dElEtE /* sneaky */ FROM fct_invoices",
    "COPY fct_invoices TO 'stolen.csv'",
    "ATTACH 'other.db' AS other",
    "INSTALL httpfs",
    "SET enable_external_access = true",
    "PRAGMA database_list",
    "CALL dbgen(sf = 1)",
    "SELECT * FROM read_csv('C:/Windows/win.ini')",
    "SELECT * FROM 'secrets.parquet'",
    "SELECT * FROM read_parquet('s3://bucket/x.parquet')",
    "SELECT * FROM fct_invoices FOR UPDATE",
]


@pytest.mark.parametrize("sql", DESTRUCTIVE)
def test_blocks_destructive_and_escape_attempts(sql, catalog):
    with pytest.raises(guard.GuardError) as e:
        guard.check(sql, catalog)
    # Parse failures are fine too (fail closed); anything that parsed must be a security violation.
    assert isinstance(e.value, guard.SecurityViolation) or "syntax" in str(e.value).lower()


@pytest.mark.parametrize("sql, expected_hint", [
    ("SELECT * FROM revenue_2024", "fct_invoices"),
    ("SELECT r.amount FROM refunds_table r", "fct_refunds"),
    ("SELECT * FROM net_revenue", "is a metric"),
    ("SELECT * FROM stg_invoices_raw", "does not exist"),  # excluded by semantic.yaml
    ("SELECT * FROM information_schema.tables", "does not exist"),
    ("SELECT c.segmnt FROM dim_customer c", "dim_customer.segment"),
    ("SELECT i.region FROM fct_invoices i", "dim_customer"),  # exists elsewhere: tells the writer to join
    ("SELECT SUM(i.revenue) FROM fct_invoices i", "does not exist"),
])
def test_hallucinations_rejected_with_actionable_hint(sql, expected_hint, catalog):
    with pytest.raises(guard.GuardError) as e:
        guard.check(sql, catalog)
    assert not isinstance(e.value, guard.SecurityViolation)
    assert expected_hint in str(e.value)


def test_valid_query_passes_and_gets_row_limit(catalog):
    out = guard.check("SELECT c.segment, COUNT(*) AS n FROM dim_customer c GROUP BY c.segment", catalog, max_rows=100)
    assert out.tables == ["dim_customer"]
    assert "LIMIT 101" in out.sql


def test_small_user_limit_is_kept_large_one_is_capped(catalog):
    assert "LIMIT 5" in guard.check("SELECT * FROM dim_account LIMIT 5", catalog).sql
    assert "LIMIT 1001" in guard.check("SELECT * FROM dim_account LIMIT 999999", catalog, max_rows=1000).sql


def test_ctes_and_unions_are_allowed(catalog):
    sql = """WITH paid AS (SELECT p.invoice_id, SUM(p.amount) AS amt FROM fct_payments p GROUP BY 1)
             SELECT i.invoice_id, paid.amt FROM fct_invoices i LEFT JOIN paid ON paid.invoice_id = i.invoice_id
             UNION ALL SELECT 0, 0"""
    out = guard.check(sql, catalog)
    assert set(out.tables) == {"fct_invoices", "fct_payments"}


def test_semantic_layer_examples_pass_guard_and_run(catalog, warehouse):
    """The verified few-shot examples must stay valid as the schema evolves."""
    for ex in catalog["examples"]:
        checked = guard.check(ex["sql"], catalog)
        warehouse.explain(checked.sql)
        warehouse.execute(checked.sql)
