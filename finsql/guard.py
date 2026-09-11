"""SQL guard: the deterministic "tester" between the LLM and the warehouse.

Every generated query is parsed into an AST with sqlglot, and all checks run on
the tree, never on the raw text: regex blocklists are defeated by comments,
casing, or a DELETE hidden inside a CTE. The SQL that finally runs is
re-generated from the validated tree, so what was checked is exactly what
executes.

Two failure types:
  GuardError        - the LLM made a fixable mistake (bad column, unknown
                      table). The message is written for the LLM and fed back
                      into the repair loop, with suggestions.
  SecurityViolation - the query tried to do something not allowed. Never
                      retried; the request is refused.
"""
import fnmatch
import re
from dataclasses import dataclass

import sqlglot
from rapidfuzz import fuzz, process
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from . import config


class GuardError(Exception):
    """Fixable problem; message goes back to the SQL writer."""


class SecurityViolation(GuardError):
    """Disallowed operation; the request is refused, not retried."""


# Read-only allowlist for the statement root...
SAFE_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
# ...and anything below it that could write, change state or escape the sandbox.
# Walking the whole tree catches `WITH x AS (DELETE ...) SELECT` and `SELECT ... INTO new_table`.
_FORBIDDEN_NAMES = [
    "DDL", "DML", "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
    "Into", "Copy", "Grant", "Revoke", "Command", "Attach", "Detach", "Install", "Pragma", "Set", "Use",
    "Transaction", "Commit", "Rollback", "Lock", "Execute", "Describe", "Show", "Summarize",
]
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))
# Functions that read files/URLs, other queries' results, or account metadata.
FORBIDDEN_FUNCTIONS = [
    "read_*", "*_scan", "glob", "sniff_csv", "system$*", "query_history*", "result_scan", "getvariable",
    "current_setting", "pg_*", "load_extension", "sleep*", "*http*", "external_*", "generate_series",
]
_UNKNOWN_COLUMN = [re.compile(r"Unknown column: (\w+)"), re.compile(r"Column '([^']+)' could not be resolved")]


@dataclass
class CheckedQuery:
    sql: str  # regenerated from the validated AST, with the row limit applied
    tables: list[str]


def check(sql: str, catalog: dict, max_rows: int = config.MAX_ROWS) -> CheckedQuery:
    dialect = catalog["dialect"]
    tree = _parse_single(sql, dialect)
    _check_read_only(tree)
    tables = _check_tables(tree, catalog)
    _check_columns(tree, catalog, tables)
    tree = _apply_limit(tree, max_rows + 1)  # +1 so the executor can tell the result was truncated
    return CheckedQuery(sql=tree.sql(dialect=dialect, pretty=True), tables=sorted(tables))


def _parse_single(sql: str, dialect: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except ParseError as e:
        raise GuardError(f"SQL syntax error: {str(e).splitlines()[0]}") from e
    if len(statements) != 1:
        raise SecurityViolation(f"Exactly one statement is allowed, got {len(statements)}.")
    return statements[0]


def _check_read_only(tree: exp.Expression) -> None:
    if not isinstance(tree, SAFE_ROOTS):
        raise SecurityViolation(f"Only read-only SELECT queries are allowed (got {tree.key.upper()}).")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SecurityViolation(f"Forbidden operation in query: {node.key.upper()}.")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if any(fnmatch.fnmatch(name, p) for p in FORBIDDEN_FUNCTIONS):
                raise SecurityViolation(f"Function {name.upper()} is not allowed.")
        if isinstance(node, exp.Table) and (not isinstance(node.this, exp.Identifier)
                                            or any(ch in node.name for ch in "./\\:")):
            # FROM read_csv(...), FROM TABLE(RESULT_SCAN(...)), FROM 'file.parquet', FROM 's3://...'
            raise SecurityViolation("Table functions and file/URL sources are not allowed; query catalog tables only.")


def _check_tables(tree: exp.Expression, catalog: dict) -> set[str]:
    """Every table must exist in the curated catalog. This is the core defence
    against hallucinated tables, and also enforces the exclude list: staging
    tables and system schemas are absent from the catalog, so they're rejected."""
    known = catalog["tables"]
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    used = set()
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if name in ctes and not t.db:
            continue
        if name not in known:
            raise GuardError(_unknown_table_message(name, catalog))
        if t.db and t.db.lower() != known[name]["schema"].lower():
            raise GuardError(f"Table `{t.db}.{name}` is not available; use `{name}` (schema {known[name]['schema']}).")
        used.add(name)
    if not used:
        raise GuardError("The query does not read from any catalog table.")
    return used


def _unknown_table_message(name: str, catalog: dict) -> str:
    metric = next((m for m in catalog.get("metrics", []) if m["name"] == name), None)
    if metric:
        return (f"`{name}` is a metric, not a table. Compute it from {', '.join(metric['tables'])}: "
                f"{metric['definition']}")
    from .retriever import suggest_tables

    return f"Table `{name}` does not exist. Closest existing tables: {', '.join(suggest_tables(name, catalog))}."


def _check_columns(tree: exp.Expression, catalog: dict, tables: set[str]) -> None:
    """sqlglot's qualifier resolves every column against the real schema and
    raises on unknown ones. If it fails for any *other* reason (dialect syntax
    it doesn't model), we don't block: EXPLAIN in the warehouse is the backstop."""
    schema = {t: {c["name"]: c["type"] for c in catalog["tables"][t]["columns"]} for t in tables}
    try:
        qualify(tree.copy(), schema=schema, dialect=catalog["dialect"], validate_qualify_columns=True,
                quote_identifiers=False)
    except OptimizeError as e:
        for pattern in _UNKNOWN_COLUMN:
            m = pattern.search(str(e))
            if m:
                raise GuardError(_unknown_column_message(m.group(1).split(".")[-1], catalog, tables)) from e
    except Exception:
        pass


def _unknown_column_message(col: str, catalog: dict, tables: set[str]) -> str:
    in_query = [f"{t}.{c['name']}" for t in tables for c in catalog["tables"][t]["columns"]]
    close = [m[0] for m in process.extract(col, in_query, scorer=fuzz.WRatio, limit=4)]
    msg = f"Column `{col}` does not exist in the tables used ({', '.join(sorted(tables))}). Closest: {', '.join(close)}."
    elsewhere = [t for t, meta in catalog["tables"].items() if t not in tables
                 and any(c["name"].lower() == col.lower() for c in meta["columns"])]
    if elsewhere:
        msg += f" `{col}` exists in {', '.join(elsewhere[:3])} (needs a join)."
    return msg


def _apply_limit(tree: exp.Expression, n: int) -> exp.Expression:
    if isinstance(tree, exp.Select):
        limit = tree.args.get("limit")
        current = limit.expression if limit else None
        if isinstance(current, exp.Literal) and current.is_int and int(current.this) <= n:
            return tree
        return tree.limit(n, copy=False)
    return exp.select("*").from_(tree.subquery("q")).limit(n)
