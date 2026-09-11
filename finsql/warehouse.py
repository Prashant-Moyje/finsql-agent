"""Warehouse connectors. The agent only ever talks to a `Warehouse`, so the
same pipeline runs on local DuckDB (free demo) or Snowflake (production).

Security note: the connection itself is the most important guardrail. Both
connectors open read-only: DuckDB via read_only=True with external file and
network access disabled, Snowflake via a role that only has SELECT on
curated reporting views (see deploy/snowflake_setup.sql)."""
import threading
from abc import ABC, abstractmethod

from . import config


class QueryError(Exception):
    """The warehouse rejected or failed to run a query."""


class Warehouse(ABC):
    dialect: str
    name: str

    @abstractmethod
    def extract_metadata(self, schemas: list[str]) -> list[dict]:
        """Tables with columns, types, comments, PKs and FKs:
        [{schema, name, description, row_count, columns: [{name, type, description, pk, fk}]}]"""

    @abstractmethod
    def explain(self, sql: str) -> None:
        """Compiles the query without running it. Raises QueryError."""

    @abstractmethod
    def execute(self, sql: str, timeout_s: int = config.QUERY_TIMEOUT_S, tag: str = "") -> tuple[list[str], list[tuple]]:
        """Runs a (pre-validated) query. Returns (column_names, rows)."""

    def top_values(self, schema: str, table: str, column: str, k: int) -> list:
        """Up to k+1 most frequent values. More than k back means high-cardinality."""
        sql = (f'SELECT "{column}" FROM "{schema}"."{table}" WHERE "{column}" IS NOT NULL '
               f'GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT {k + 1}')
        return [r[0] for r in self.execute(sql)[1]]


class DuckDBWarehouse(Warehouse):
    dialect = "duckdb"

    def __init__(self, path: str = config.DUCKDB_PATH):
        import duckdb

        self.name = f"duckdb:{path}"
        # read_only: writes fail at the engine level no matter what SQL arrives.
        # enable_external_access=False: blocks read_csv('/etc/passwd'), httpfs, ATTACH, COPY TO.
        self.con = duckdb.connect(path, read_only=True, config={"enable_external_access": False})
        self.con.execute("SET lock_configuration = true")  # nothing can SET its way back out

    def extract_metadata(self, schemas: list[str]) -> list[dict]:
        schemas = [s.lower() for s in schemas]
        ph = ",".join("?" * len(schemas))
        tables = {
            (s, t): {"schema": s, "name": t, "description": c or "", "row_count": n, "columns": []}
            for s, t, c, n in self.con.execute(
                f"SELECT schema_name, table_name, comment, estimated_size FROM duckdb_tables() "
                f"WHERE schema_name IN ({ph}) AND NOT internal ORDER BY table_name", schemas).fetchall()
        }
        for s, v, c in self.con.execute(
                f"SELECT schema_name, view_name, comment FROM duckdb_views() WHERE schema_name IN ({ph}) "
                f"AND NOT internal", schemas).fetchall():
            tables[(s, v)] = {"schema": s, "name": v, "description": c or "", "row_count": None, "columns": []}

        for s, t, col, typ, c in self.con.execute(
                f"SELECT schema_name, table_name, column_name, data_type, comment FROM duckdb_columns() "
                f"WHERE schema_name IN ({ph}) ORDER BY table_name, column_index", schemas).fetchall():
            if (s, t) in tables:
                tables[(s, t)]["columns"].append(
                    {"name": col, "type": typ, "description": c or "", "pk": False, "fk": None})

        for s, t, ctype, cols, ref_table, ref_cols in self.con.execute(
                f"SELECT schema_name, table_name, constraint_type, constraint_column_names, referenced_table, "
                f"referenced_column_names FROM duckdb_constraints() WHERE schema_name IN ({ph}) "
                f"AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')", schemas).fetchall():
            by_name = {c["name"]: c for c in tables.get((s, t), {}).get("columns", [])}
            for i, col in enumerate(cols):
                if col not in by_name:
                    continue
                if ctype == "PRIMARY KEY":
                    by_name[col]["pk"] = True
                else:
                    by_name[col]["fk"] = f"{ref_table}.{ref_cols[i]}"
        return list(tables.values())

    def explain(self, sql: str) -> None:
        try:
            self.con.execute(f"EXPLAIN {sql}")
        except Exception as e:  # duckdb raises several error classes
            raise QueryError(_first_line(e)) from e

    def execute(self, sql, timeout_s=config.QUERY_TIMEOUT_S, tag=""):
        # DuckDB has no statement timeout, so interrupt from a watchdog thread.
        timer = threading.Timer(timeout_s, self.con.interrupt)
        timer.start()
        try:
            cur = self.con.execute(sql)
            return [d[0] for d in cur.description], cur.fetchall()
        except Exception as e:
            msg = _first_line(e)
            raise QueryError(f"Query exceeded {timeout_s}s timeout" if "nterrupt" in msg else msg) from e
        finally:
            timer.cancel()


class SnowflakeWarehouse(Warehouse):
    dialect = "snowflake"

    def __init__(self):
        import snowflake.connector  # pip install -r requirements-snowflake.txt

        self.name = f"snowflake:{config.SNOWFLAKE_ACCOUNT}/{config.SNOWFLAKE_DATABASE}"
        self.con = snowflake.connector.connect(
            account=config.SNOWFLAKE_ACCOUNT,
            user=config.SNOWFLAKE_USER,
            private_key=_load_private_key(),
            role=config.SNOWFLAKE_ROLE,  # SELECT-only role; the real destructive-command guarantee
            warehouse=config.SNOWFLAKE_WAREHOUSE,  # dedicated XS warehouse behind a resource monitor
            database=config.SNOWFLAKE_DATABASE,
            schema=config.SNOWFLAKE_SCHEMA,
            session_parameters={
                "STATEMENT_TIMEOUT_IN_SECONDS": config.QUERY_TIMEOUT_S,
                "QUERY_TAG": "finsql",
            },
        )

    def _rows(self, sql: str, params=None) -> list[dict]:
        cur = self.con.cursor()
        cur.execute(sql, params)
        names = [d[0].lower() for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def extract_metadata(self, schemas: list[str]) -> list[dict]:
        out = []
        for schema in schemas:
            tables = {
                r["table_name"]: {"schema": schema, "name": r["table_name"], "description": r["comment"] or "",
                                  "row_count": r["row_count"], "columns": []}
                for r in self._rows(
                    "SELECT table_name, comment, row_count FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW')", (schema.upper(),))
            }
            for r in self._rows(
                    "SELECT table_name, column_name, data_type, comment FROM information_schema.columns "
                    "WHERE table_schema = %s ORDER BY table_name, ordinal_position", (schema.upper(),)):
                if r["table_name"] in tables:
                    tables[r["table_name"]]["columns"].append(
                        {"name": r["column_name"], "type": r["data_type"], "description": r["comment"] or "",
                         "pk": False, "fk": None})
            fq = f'"{config.SNOWFLAKE_DATABASE}"."{schema.upper()}"'
            for r in self._rows(f"SHOW PRIMARY KEYS IN SCHEMA {fq}"):
                for c in tables.get(r["table_name"], {}).get("columns", []):
                    if c["name"] == r["column_name"]:
                        c["pk"] = True
            for r in self._rows(f"SHOW IMPORTED KEYS IN SCHEMA {fq}"):
                for c in tables.get(r["fk_table_name"], {}).get("columns", []):
                    if c["name"] == r["fk_column_name"]:
                        c["fk"] = f'{r["pk_table_name"]}.{r["pk_column_name"]}'
            out.extend(tables.values())
        return out

    def explain(self, sql: str) -> None:
        try:
            self.con.cursor().execute(f"EXPLAIN USING TEXT {sql}")  # compiles; does not run the query
        except Exception as e:
            raise QueryError(_first_line(e)) from e

    def execute(self, sql, timeout_s=config.QUERY_TIMEOUT_S, tag=""):
        cur = self.con.cursor()
        try:
            if tag:  # every query is attributable to the Slack user who asked (QUERY_HISTORY audit)
                cur.execute("ALTER SESSION SET QUERY_TAG = %s", (f"finsql:{tag}"[:2000],))
            cur.execute(sql, timeout=timeout_s)
            return [d[0] for d in cur.description], cur.fetchall()
        except Exception as e:
            raise QueryError(_first_line(e)) from e


def _load_private_key() -> bytes:
    from cryptography.hazmat.primitives import serialization

    if config.SNOWFLAKE_PRIVATE_KEY:
        pem = config.SNOWFLAKE_PRIVATE_KEY.encode()
    else:
        with open(config.SNOWFLAKE_PRIVATE_KEY_PATH, "rb") as f:
            pem = f.read()
    key = serialization.load_pem_private_key(pem, password=None)
    return key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0][:500] if str(e).strip() else type(e).__name__


_instance: Warehouse | None = None


def get_warehouse() -> Warehouse:
    """One connection per process; reused across warm Lambda invocations."""
    global _instance
    if _instance is None:
        _instance = SnowflakeWarehouse() if config.WAREHOUSE == "snowflake" else DuckDBWarehouse()
    return _instance
