"""Builds a synthetic finance warehouse in DuckDB so the whole system runs for
free without a Snowflake account.

Core model (documented with COMMENTs, like a well-kept warehouse):
  dim_customer, dim_department, dim_account, dim_vendor, dim_fiscal_period,
  fct_invoices, fct_payments, fct_refunds, fct_expenses, fct_budget

Plus `--noise-tables` extra tables from other domains (HR, marketing, ops, ...)
and a few very wide ones, to simulate the "massive schema" problem: the full
schema is far too large to paste into an LLM prompt.

Usage:  python scripts/seed_duckdb.py [--noise-tables 250] [--out data/finance.duckdb]
"""
import argparse
import csv
import math
import random
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
START, AS_OF = date(2023, 4, 1), date(2026, 8, 31)
N_CRM_ATTRS = 70  # custom CRM fields on dim_customer: a realistic "wide" table

DDL = f"""
CREATE TABLE dim_fiscal_period (
    period_id INTEGER PRIMARY KEY,
    period_start DATE, period_end DATE,
    calendar_year INTEGER, calendar_month INTEGER, month_name VARCHAR,
    fiscal_year INTEGER, fiscal_quarter INTEGER, fiscal_month INTEGER,
    fiscal_quarter_label VARCHAR, is_closed BOOLEAN
);
CREATE TABLE dim_department (
    department_id INTEGER PRIMARY KEY,
    department_name VARCHAR, cost_center VARCHAR, division VARCHAR, region VARCHAR
);
CREATE TABLE dim_account (
    account_id INTEGER PRIMARY KEY,
    account_number VARCHAR, account_name VARCHAR, account_type VARCHAR, account_category VARCHAR
);
CREATE TABLE dim_customer (
    customer_id INTEGER PRIMARY KEY,
    customer_name VARCHAR, segment VARCHAR, region VARCHAR, industry VARCHAR,
    signup_date DATE, account_manager VARCHAR,
    {", ".join(f"crm_attr_{i:03d} VARCHAR" for i in range(1, N_CRM_ATTRS + 1))}
);
CREATE TABLE dim_vendor (
    vendor_id INTEGER PRIMARY KEY,
    vendor_name VARCHAR, vendor_category VARCHAR, payment_terms_days INTEGER
);
CREATE TABLE fct_invoices (
    invoice_id INTEGER PRIMARY KEY,
    invoice_number VARCHAR,
    customer_id INTEGER REFERENCES dim_customer(customer_id),
    department_id INTEGER REFERENCES dim_department(department_id),
    account_id INTEGER REFERENCES dim_account(account_id),
    period_id INTEGER REFERENCES dim_fiscal_period(period_id),
    invoice_date DATE, due_date DATE,
    gross_amount DECIMAL(14,2), discount_amount DECIMAL(14,2), tax_amount DECIMAL(14,2),
    status VARCHAR
);
CREATE TABLE fct_payments (
    payment_id INTEGER PRIMARY KEY,
    invoice_id INTEGER REFERENCES fct_invoices(invoice_id),
    payment_date DATE, amount DECIMAL(14,2), payment_method VARCHAR
);
CREATE TABLE fct_refunds (
    refund_id INTEGER PRIMARY KEY,
    invoice_id INTEGER REFERENCES fct_invoices(invoice_id),
    refund_date DATE, amount DECIMAL(14,2), reason VARCHAR
);
CREATE TABLE fct_expenses (
    expense_id INTEGER PRIMARY KEY,
    department_id INTEGER REFERENCES dim_department(department_id),
    account_id INTEGER REFERENCES dim_account(account_id),
    vendor_id INTEGER REFERENCES dim_vendor(vendor_id),
    period_id INTEGER REFERENCES dim_fiscal_period(period_id),
    expense_date DATE, amount DECIMAL(14,2), description VARCHAR, approval_status VARCHAR
);
CREATE TABLE fct_budget (
    budget_id INTEGER PRIMARY KEY,
    department_id INTEGER REFERENCES dim_department(department_id),
    account_id INTEGER REFERENCES dim_account(account_id),
    period_id INTEGER REFERENCES dim_fiscal_period(period_id),
    budget_amount DECIMAL(14,2)
);
-- Staging / backup tables that exist in every real warehouse. The semantic
-- layer excludes them so the agent can never query unvetted data.
CREATE TABLE stg_invoices_raw (raw_id INTEGER, payload VARCHAR, loaded_at TIMESTAMP);
CREATE TABLE tmp_revenue_backup_2024 (invoice_id INTEGER, gross_amount DECIMAL(14,2), note VARCHAR);
"""

COMMENTS = {
    "dim_fiscal_period": ("Fiscal calendar, one row per month. Fiscal year starts April 1.", {
        "period_id": "YYYYMM of the calendar month", "fiscal_year": "Fiscal year, named by the calendar year it ends in",
        "fiscal_quarter": "1-4; Q1 = Apr-Jun", "fiscal_quarter_label": "e.g. FY2026-Q1",
        "is_closed": "True once the month's books are closed"}),
    "dim_department": ("Departments / cost centers", {"division": "Revenue, R&D or G&A"}),
    "dim_account": ("Chart of accounts (general ledger accounts)", {
        "account_type": "Revenue, COGS, Operating Expense, Asset, Liability", "account_category": "Finer grouping, e.g. Payroll, Travel"}),
    "dim_customer": ("Customers (accounts) that are invoiced", {
        "segment": "Enterprise, Mid-Market or SMB", "region": "NA, EMEA, APAC or LATAM",
        **{f"crm_attr_{i:03d}": "Custom CRM field synced from Salesforce" for i in range(1, N_CRM_ATTRS + 1)}}),
    "dim_vendor": ("Suppliers we pay for goods and services", {}),
    "fct_invoices": ("Customer invoices (accounts receivable / revenue)", {
        "gross_amount": "Invoice amount before discount and tax", "discount_amount": "Discount granted",
        "tax_amount": "Sales tax / VAT collected (not revenue)", "status": "paid, open, overdue or void",
        "period_id": "Fiscal period of invoice_date", "department_id": "Sales team that owns the invoice",
        "account_id": "Revenue account (product, subscription or services)"}),
    "fct_payments": ("Customer payments received against invoices", {"amount": "Cash received"}),
    "fct_refunds": ("Refunds issued to customers", {"amount": "Refunded amount (positive number)"}),
    "fct_expenses": ("Spend transactions (payroll, vendors, T&E)", {
        "approval_status": "approved, pending or rejected", "vendor_id": "NULL for payroll"}),
    "fct_budget": ("Monthly budget by department and account", {"budget_amount": "Planned spend for the month"}),
}

ACCOUNTS = [  # id, number, name, type, category
    (1, "1000", "Cash", "Asset", "Cash"), (2, "1100", "Accounts Receivable", "Asset", "Receivables"),
    (3, "2000", "Accounts Payable", "Liability", "Payables"),
    (10, "4000", "Product Revenue", "Revenue", "Product"), (11, "4100", "Subscription Revenue", "Revenue", "Subscription"),
    (12, "4200", "Services Revenue", "Revenue", "Services"),
    (20, "5000", "Cost of Services", "COGS", "Services Delivery"), (21, "5100", "Hosting Costs", "COGS", "Hosting"),
    (30, "6000", "Salaries & Wages", "Operating Expense", "Payroll"), (31, "6100", "Travel & Entertainment", "Operating Expense", "Travel"),
    (32, "6200", "Software Subscriptions", "Operating Expense", "Software"), (33, "6300", "Marketing Programs", "Operating Expense", "Marketing"),
    (34, "6400", "Rent & Facilities", "Operating Expense", "Facilities"), (35, "6500", "Professional Services", "Operating Expense", "Professional Fees"),
    (36, "6600", "Office Supplies", "Operating Expense", "Office"), (37, "6700", "Training & Development", "Operating Expense", "Training"),
]
DEPARTMENTS = [  # id, name, cost center, division, region
    (1, "Sales NA", "CC-1001", "Revenue", "NA"), (2, "Sales EMEA", "CC-1002", "Revenue", "EMEA"),
    (3, "Sales APAC", "CC-1003", "Revenue", "APAC"), (4, "Marketing", "CC-2001", "Revenue", "Global"),
    (5, "Engineering", "CC-3001", "R&D", "Global"), (6, "Customer Success", "CC-1101", "Revenue", "Global"),
    (7, "Finance", "CC-4001", "G&A", "Global"), (8, "HR", "CC-4002", "G&A", "Global"),
    (9, "IT", "CC-4003", "G&A", "Global"), (10, "Operations", "CC-4004", "G&A", "Global"),
]
# Monthly spend baseline per department, keyed by account_id.
SPEND = {
    1: {30: 180_000, 31: 25_000, 32: 4_000, 36: 1_500, 37: 3_000},
    2: {30: 140_000, 31: 22_000, 32: 3_000, 36: 1_200, 37: 2_500},
    3: {30: 90_000, 31: 18_000, 32: 2_000, 36: 1_000, 37: 2_000},
    4: {30: 110_000, 33: 90_000, 32: 12_000, 31: 8_000, 35: 15_000},
    5: {30: 420_000, 21: 85_000, 32: 30_000, 37: 8_000, 31: 6_000},
    6: {30: 130_000, 20: 20_000, 31: 5_000, 32: 5_000},
    7: {30: 90_000, 35: 25_000, 32: 6_000},
    8: {30: 60_000, 37: 10_000, 35: 8_000},
    9: {30: 80_000, 32: 45_000, 36: 3_000},
    10: {30: 70_000, 34: 95_000, 36: 5_000},
}
VENDORS = {  # category -> names; category maps to expense accounts below
    "Travel": ["SkyBridge Airlines", "Harbor Hotels", "MetroRide", "Summit Travel Co"],
    "Software": ["CloudDesk", "Notable Docs", "PipeFlow CRM", "SecureVault", "DataLoom"],
    "Hosting": ["Nimbus Cloud", "Stratus Compute"],
    "Marketing": ["Brightline Media", "AdSpark", "EventForge", "Signal Research"],
    "Facilities": ["Keystone Properties", "CleanCo Services"],
    "Professional Fees": ["Hale & Partners LLP", "Northstar Consulting", "Ledger Audit Group"],
    "Office": ["DeskMart", "PaperTrail Supplies"],
    "Training": ["SkillPath Academy", "LearnLoop"],
    "Services Delivery": ["FieldPro Contractors", "ImplementIQ"],
}
ACCOUNT_VENDOR_CATEGORY = {31: "Travel", 32: "Software", 21: "Hosting", 33: "Marketing", 34: "Facilities",
                           35: "Professional Fees", 36: "Office", 37: "Training", 20: "Services Delivery"}


def months(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def month_end(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)


def period_id(d: date) -> int:
    return d.year * 100 + d.month


def fiscal(d: date):
    fy = d.year + 1 if d.month >= 4 else d.year
    fm = (d.month - 4) % 12 + 1
    return fy, (fm - 1) // 3 + 1, fm


def build_rows(rng: random.Random) -> dict[str, list[tuple]]:
    rows: dict[str, list[tuple]] = {}

    rows["dim_fiscal_period"] = []
    for m in months(date(2023, 4, 1), date(2027, 3, 1)):
        fy, fq, fm = fiscal(m)
        rows["dim_fiscal_period"].append((
            period_id(m), m, month_end(m), m.year, m.month, m.strftime("%B"),
            fy, fq, fm, f"FY{fy}-Q{fq}", month_end(m) < date(2026, 9, 1)))

    rows["dim_department"] = DEPARTMENTS
    rows["dim_account"] = ACCOUNTS

    # Customers
    adj = ["Blue", "North", "Apex", "Silver", "Quantum", "Evergreen", "Iron", "Bright", "Crimson", "Atlas",
           "Pioneer", "Vertex", "Cobalt", "Summit", "Harbor", "Golden", "Polar", "Nova", "Orbit", "Prime"]
    noun = ["Logistics", "Health", "Foods", "Dynamics", "Analytics", "Retail", "Energy", "Capital", "Labs",
            "Systems", "Media", "Motors", "Pharma", "Networks", "Textiles", "Robotics", "Farms", "Aerospace"]
    suffix = ["Inc", "Ltd", "GmbH", "Corp", "LLC", "Group", "Pte Ltd", "SA"]
    industries = ["Manufacturing", "Healthcare", "Retail", "Financial Services", "Technology", "Energy", "Logistics"]
    managers = ["Priya Shah", "Daniel Kim", "Sofia Rossi", "Marcus Lee", "Aisha Bello", "Tom Becker", "Mei Chen"]
    names, customers = set(), []
    while len(customers) < 400:
        name = f"{rng.choice(adj)} {rng.choice(noun)} {rng.choice(suffix)}"
        if name in names:
            continue
        names.add(name)
        segment = rng.choices(["Enterprise", "Mid-Market", "SMB"], [0.15, 0.35, 0.5])[0]
        region = rng.choices(["NA", "EMEA", "APAC", "LATAM"], [0.45, 0.3, 0.17, 0.08])[0]
        signup = date(2021, 1, 1) + timedelta(days=int(rng.betavariate(1.3, 2.2) * (date(2026, 6, 30) - date(2021, 1, 1)).days))
        customers.append((len(customers) + 1, name, segment, region, rng.choice(industries), signup,
                          rng.choice(managers), *([None] * N_CRM_ATTRS)))
    rows["dim_customer"] = customers

    # Vendors
    rows["dim_vendor"], vendors_by_cat = [], {}
    for cat, vnames in VENDORS.items():
        for vn in vnames:
            vid = len(rows["dim_vendor"]) + 1
            rows["dim_vendor"].append((vid, vn, cat, rng.choice([15, 30, 45, 60])))
            vendors_by_cat.setdefault(cat, []).append(vid)

    # Invoices, payments, refunds
    sales_dept = {"NA": 1, "LATAM": 1, "EMEA": 2, "APAC": 3}
    tax_rate = {"NA": 0.07, "EMEA": 0.20, "APAC": 0.10, "LATAM": 0.12}
    amount_mu = {"Enterprise": 10.3, "Mid-Market": 9.2, "SMB": 7.8}
    discounts = {"Enterprise": [0, 0.05, 0.1, 0.15], "Mid-Market": [0, 0, 0.05, 0.1], "SMB": [0, 0, 0, 0.05]}
    inv, pay, ref = [], [], []
    for i, m in enumerate(months(START, AS_OF)):
        season = 1.25 if m.month in (1, 2, 3) else 0.9 if m.month in (7, 8) else 1.0
        eligible = [c for c in customers if c[5] <= m]
        last_day = month_end(m).day
        for _ in range(int(110 * 1.018 ** i * season * rng.uniform(0.92, 1.08))):
            c = rng.choice(eligible)
            cid, seg, region = c[0], c[2], c[3]
            d = date(m.year, m.month, rng.randint(1, last_day))
            gross = round(math.exp(rng.gauss(amount_mu[seg], 0.6)), 2)
            disc = round(gross * rng.choice(discounts[seg]), 2)
            tax = round((gross - disc) * tax_rate[region], 2)
            terms = 60 if seg == "Enterprise" else 30
            due = d + timedelta(days=terms)
            acct = rng.choices([10, 11, 12], [0.5, 0.35, 0.15])[0]
            total = round(gross - disc + tax, 2)
            if rng.random() < 0.02:
                status = "void"
            elif due >= AS_OF:
                status = "paid" if rng.random() < 0.45 else "open"
            else:
                status = "overdue" if rng.random() < 0.06 else "paid"
            iid = len(inv) + 1
            inv.append((iid, f"INV-{iid:06d}", cid, sales_dept[region], acct, period_id(d), d, due, gross, disc, tax, status))
            method = rng.choice(["ACH", "Wire", "Card", "Check"])
            if status == "paid":
                pd_ = min(d + timedelta(days=rng.randint(5, terms + 15)), AS_OF)
                pay.append((len(pay) + 1, iid, pd_, total, method))
                if rng.random() < 0.03:
                    rd = pd_ + timedelta(days=rng.randint(5, 45))
                    if rd <= AS_OF:
                        ref.append((len(ref) + 1, iid, rd, round((gross - disc) * rng.choice([0.1, 0.25, 0.5, 1.0]), 2),
                                    rng.choice(["Service credit", "Billing error", "Cancellation", "Duplicate charge"])))
            elif status == "overdue" and rng.random() < 0.3:
                pd_ = min(d + timedelta(days=rng.randint(20, 90)), AS_OF)
                pay.append((len(pay) + 1, iid, pd_, round(total * rng.uniform(0.2, 0.6), 2), method))
    rows["fct_invoices"], rows["fct_payments"], rows["fct_refunds"] = inv, pay, ref

    # Expenses: baseline * growth * noise, with two deliberate stories in the data:
    # Engineering hosting jumps ~40% from May 2026; Marketing overspends in FY2027-Q1.
    exp_rows = []
    for i, m in enumerate(months(START, AS_OF)):
        for dept, accts in SPEND.items():
            for acct, base in accts.items():
                amt = base * 1.012 ** i * rng.uniform(0.85, 1.15)
                if dept == 5 and acct == 21 and m >= date(2026, 5, 1):
                    amt *= 1.4
                if dept == 4 and acct == 33 and date(2026, 4, 1) <= m <= date(2026, 6, 1):
                    amt *= 1.35
                if acct == 31 and m.month in (7, 8):
                    amt *= 0.7
                parts = 2 if acct == 30 else rng.randint(1, 5)
                weights = [rng.random() + 0.2 for _ in range(parts)]
                for w in weights:
                    d = date(m.year, m.month, rng.randint(1, month_end(m).day))
                    cat = ACCOUNT_VENDOR_CATEGORY.get(acct)
                    vendor = rng.choice(vendors_by_cat[cat]) if cat else None
                    desc = "Payroll run" if acct == 30 else f"{dict((a[0], a[2]) for a in ACCOUNTS)[acct]} - {m:%b %Y}"
                    status = rng.choices(["approved", "pending", "rejected"], [0.96, 0.025, 0.015])[0]
                    exp_rows.append((len(exp_rows) + 1, dept, acct, vendor, period_id(d), d,
                                     round(amt * w / sum(weights), 2), desc, status))
    rows["fct_expenses"] = exp_rows

    # Budget for FY2025 through FY2027 (full year, including future months)
    bud = []
    for i, m in enumerate(months(START, date(2027, 3, 1))):
        if m < date(2024, 4, 1):
            continue
        for dept, accts in SPEND.items():
            for acct, base in accts.items():
                bud.append((len(bud) + 1, dept, acct, period_id(m), round(base * 1.012 ** i * rng.uniform(0.97, 1.05), -2)))
    rows["fct_budget"] = bud

    rows["stg_invoices_raw"] = [(k, '{"raw": true}', None) for k in range(1, 50)]
    rows["tmp_revenue_backup_2024"] = [(k, round(rng.uniform(100, 9000), 2), "pre-migration copy") for k in range(1, 50)]
    return rows


# --- Noise tables: the rest of a large company's warehouse -----------------------
DOMAINS = {
    "hr": ["employee", "headcount_snapshot", "training_enrollment", "leave_request", "performance_review", "recruiting_pipeline",
           "benefits_enrollment", "org_chart", "compensation_band", "exit_interview"],
    "mkt": ["campaign", "campaign_touch", "web_session", "lead", "email_send", "ad_spend_daily", "event_attendee",
            "attribution_model", "landing_page", "webinar_registration"],
    "ops": ["warehouse_inventory", "shipment", "carrier_rate", "sla_breach", "incident", "work_order", "asset_register",
            "maintenance_log", "fleet_vehicle", "supplier_scorecard"],
    "it": ["ticket", "device", "license_assignment", "access_request", "change_request", "vulnerability_scan",
           "sso_login", "backup_job", "server", "app_catalog"],
    "sales": ["opportunity", "opportunity_history", "quote", "quote_line", "territory", "quota", "activity",
              "partner", "deal_registration", "forecast_snapshot"],
    "prod": ["feature_usage", "event_stream", "experiment", "experiment_assignment", "release", "bug", "nps_response",
             "session_replay_index", "api_request_log", "tenant_config"],
    "legal": ["contract", "contract_clause", "nda", "litigation_matter", "policy_ack", "trademark"],
    "fac": ["desk_booking", "badge_swipe", "room_booking", "site", "energy_meter_reading"],
}
SUFFIXES = ["", "_daily", "_v2", "_snapshot", "_agg", "_history", "_archive", "_detail", "_summary", "_raw_hist"]
COLUMN_VOCAB = [
    ("created_at", "TIMESTAMP"), ("updated_at", "TIMESTAMP"), ("status", "VARCHAR"), ("owner_email", "VARCHAR"),
    ("region", "VARCHAR"), ("country_code", "VARCHAR"), ("amount", "DECIMAL(14,2)"), ("cost_usd", "DECIMAL(14,2)"),
    ("quantity", "INTEGER"), ("score", "DOUBLE"), ("is_active", "BOOLEAN"), ("category", "VARCHAR"), ("notes", "VARCHAR"),
    ("start_date", "DATE"), ("end_date", "DATE"), ("priority", "VARCHAR"), ("source_system", "VARCHAR"),
    ("external_ref", "VARCHAR"), ("duration_minutes", "INTEGER"), ("department_code", "VARCHAR"), ("channel", "VARCHAR"),
    ("revenue_attributed", "DECIMAL(14,2)"), ("budget_code", "VARCHAR"), ("fiscal_week", "INTEGER"), ("currency", "VARCHAR"),
]


def noise_ddl(rng: random.Random, n: int) -> list[str]:
    names = sorted({f"{d}_{noun}{s}" for d, nouns in DOMAINS.items() for noun in nouns for s in SUFFIXES})
    rng.shuffle(names)
    stmts = []
    for k, name in enumerate(names[:n]):
        if k < 6:  # a few very wide tables, like CRM or event snapshots
            cols = [f"{name.split('_')[0]}_field_{j:03d} VARCHAR" for j in range(rng.randint(120, 200))]
        else:
            cols = [f"{c} {t}" for c, t in rng.sample(COLUMN_VOCAB, rng.randint(4, 18))]
        stmts.append(f"CREATE TABLE {name} (id BIGINT PRIMARY KEY, {', '.join(cols)});")
    return stmts


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--noise-tables", type=int, default=600)
    ap.add_argument("--out", default=str(ROOT / "data" / "finance.duckdb"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    rng = random.Random(42)

    con = duckdb.connect(str(out))
    con.execute(DDL)
    q = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731  SQL string literal
    for table, (desc, cols) in COMMENTS.items():
        con.execute(f"COMMENT ON TABLE {table} IS {q(desc)}")
        for col, cdesc in cols.items():
            con.execute(f"COMMENT ON COLUMN {table}.{col} IS {q(cdesc)}")

    with tempfile.TemporaryDirectory() as tmp:
        for table, data in build_rows(rng).items():
            path = Path(tmp) / f"{table}.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(data)
            con.execute(f"COPY {table} FROM '{path.as_posix()}' (HEADER false, NULLSTR '')")
            if not args.quiet:
                print(f"  {table:<26} {len(data):>7,} rows")

    for stmt in noise_ddl(rng, args.noise_tables):
        con.execute(stmt)
    total = con.execute("SELECT count(*) FROM duckdb_tables()").fetchone()[0]
    con.close()
    if not args.quiet:
        print(f"Wrote {out} ({total} tables, {args.noise_tables} of them noise)")


if __name__ == "__main__":
    sys.exit(main())
