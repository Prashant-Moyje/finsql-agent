from finsql.catalog import estimate_tokens
from finsql.retriever import render_table, retrieve


def test_excluded_tables_never_reach_the_catalog(catalog):
    assert not [t for t in catalog["tables"] if t.startswith(("stg_", "tmp_"))]
    assert catalog["stats"]["excluded_tables"] == 2


def test_metric_synonym_pins_the_right_tables(catalog):
    r = retrieve("How much revenue did we make last quarter?", catalog)
    assert "net_revenue" in [m["name"] for m in r.metrics]
    assert "fct_invoices" in r.tables
    assert "dim_fiscal_period" in r.tables  # pulled in through the FK graph


def test_budget_question_gets_both_fact_tables_and_department(catalog):
    r = retrieve("Which departments were over budget in FY2026?", catalog)
    assert {"fct_expenses", "fct_budget", "dim_department"} <= set(r.tables)


def test_schema_slice_respects_token_budget(catalog):
    r = retrieve("net revenue by customer segment and region and department and account", catalog, budget=600)
    assert r.stats["schema_tokens"] <= 600 + 400  # first table always included, the rest must fit
    assert r.stats["schema_tokens"] < r.stats["full_schema_tokens"] / 5


def test_wide_table_is_pruned_but_keeps_keys_and_relevant_columns(catalog):
    t = catalog["tables"]["dim_customer"]
    assert len(t["columns"]) > 70
    card = render_table(t, {"segment"}, max_cols=25)
    assert "customer_id INTEGER PK" in card and "segment" in card
    assert "crm_attr_050" not in card and "more columns not shown" in card
    assert estimate_tokens(card) < estimate_tokens(render_table(t)) / 3


def test_categorical_values_are_sampled(catalog):
    status = next(c for c in catalog["tables"]["fct_invoices"]["columns"] if c["name"] == "status")
    assert set(status["samples"]) == {"paid", "open", "overdue", "void"}


def _samples(catalog, table, column):
    return next(c for c in catalog["tables"][table]["columns"] if c["name"] == column)["samples"]


def test_dimension_labels_are_fully_listed(catalog):
    # Regression (eval): without these the writer asked "which account is Subscription Revenue?"
    assert "Hosting Costs" in _samples(catalog, "dim_account", "account_name")
    assert len(_samples(catalog, "dim_department", "department_name")) == 10
    # High-cardinality names are never dumped into the prompt
    assert _samples(catalog, "dim_customer", "customer_name") == []


def test_wide_table_is_not_buried_by_bm25_length_normalisation(catalog):
    # Regression (eval): dim_customer's 70 crm_attr columns made it lose to dim_fiscal_period
    r = retrieve("How many new customers signed up in calendar year 2025?", catalog)
    assert "dim_customer" in r.tables


def test_event_tables_get_their_own_metric(catalog):
    r = retrieve("Cash collected from customers by payment method in FY2026", catalog)
    assert "cash_collected" in [m["name"] for m in r.metrics] and "fct_payments" in r.tables
