"""Layer 1 of the security model, tested on its own: even if the guard were
bypassed entirely, the connection itself refuses to write or read files."""
import pytest

from finsql.warehouse import QueryError


@pytest.mark.parametrize("sql", [
    "DELETE FROM fct_invoices",
    "DROP TABLE fct_budget",
    "CREATE TABLE x (a INT)",
    "UPDATE dim_account SET account_name = 'x'",
])
def test_connection_is_read_only(warehouse, sql):
    with pytest.raises(QueryError):
        warehouse.execute(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('pyproject.toml')",
    "COPY (SELECT 1) TO 'out.csv'",
    "SET enable_external_access = true",
])
def test_no_file_access_and_config_locked(warehouse, sql):
    with pytest.raises(QueryError):
        warehouse.execute(sql)


def test_explain_catches_type_errors_without_running(warehouse):
    with pytest.raises(QueryError):
        warehouse.explain("SELECT i.status + 1 FROM fct_invoices i")


def test_metadata_includes_keys_and_comments(warehouse):
    meta = {t["name"]: t for t in warehouse.extract_metadata(["main"])}
    cols = {c["name"]: c for c in meta["fct_invoices"]["columns"]}
    assert cols["invoice_id"]["pk"]
    assert cols["customer_id"]["fk"] == "dim_customer.customer_id"
    assert "void" in cols["status"]["description"]
