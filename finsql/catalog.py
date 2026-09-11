"""Context store: the warehouse schema + business semantics, as one JSON file.

Built offline by `scripts/extract_schema.py` (nightly in CI) and read by the
agent at runtime. Keeping this out of the request path means a Slack question
never waits on INFORMATION_SCHEMA queries, and the agent reads a curated view
of the warehouse instead of the raw one."""
import fnmatch
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml

from . import config
from .warehouse import Warehouse

SAMPLE_K = 12  # distinct values kept for low-cardinality text columns
SAMPLE_K_DIMENSION = 50  # small lookup tables: list every label ('Hosting Costs', 'Engineering')
DIMENSION_MAX_ROWS = 100
_TEXT_TYPES = ("VARCHAR", "TEXT", "STRING", "CHAR")
# High-cardinality columns (customer names, emails) are dropped automatically by
# the distinct-count check; these are skipped outright as free text or identifiers.
_SKIP_SAMPLE = ("email", "description", "notes", "number", "payload", "_ref")


def load_semantic(path: str = config.SEMANTIC_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_catalog(wh: Warehouse, semantic: dict, sample_values: bool = True) -> dict:
    excluded = semantic.get("exclude_tables", [])
    extra = semantic.get("tables", {}) or {}
    tables = {}
    n_excluded = 0
    for t in wh.extract_metadata(semantic.get("include_schemas", ["main"])):
        if any(fnmatch.fnmatch(t["name"].lower(), p.lower()) for p in excluded):
            n_excluded += 1
            continue
        meta = extra.get(t["name"].lower(), {})
        t["grain"] = meta.get("grain", "")
        t["synonyms"] = meta.get("synonyms", [])
        if meta.get("description"):
            t["description"] = meta["description"]
        for col in t["columns"]:
            col["samples"] = _samples(wh, t, col) if sample_values else []
        tables[t["name"].lower()] = t

    # Declared relationships fill in joins the warehouse doesn't know about (views).
    for rel in semantic.get("relationships", []) or []:
        child, parent = (s.strip() for s in rel.split("->"))
        ct, cc = child.lower().split(".")
        for col in tables.get(ct, {}).get("columns", []):
            if col["name"].lower() == cc:
                col["fk"] = parent

    catalog = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": wh.name,
        "dialect": wh.dialect,
        "fiscal_year_start_month": int(semantic.get("fiscal_year_start_month", 1)),
        "tables": tables,
        "metrics": semantic.get("metrics", []) or [],
        "rules": semantic.get("rules", []) or [],
        "examples": semantic.get("examples", []) or [],
    }
    from .retriever import render_table  # local import: retriever depends on catalog loading

    full = "\n".join(render_table(t) for t in tables.values())
    catalog["stats"] = {
        "tables": len(tables),
        "columns": sum(len(t["columns"]) for t in tables.values()),
        "excluded_tables": n_excluded,
        "full_schema_tokens": estimate_tokens(full),
    }
    return catalog


def _samples(wh: Warehouse, table: dict, col: dict) -> list:
    """Real values for categorical columns ('overdue', 'EMEA') stop the LLM from
    guessing filter literals like status = 'OVERDUE' or region = 'Europe'."""
    name = col["name"].lower()
    if (not col["type"].upper().startswith(_TEXT_TYPES) or col["pk"] or col["fk"] or name.endswith("_id")
            or any(s in name for s in _SKIP_SAMPLE) or not table.get("row_count")):
        return []
    k = SAMPLE_K_DIMENSION if table["row_count"] <= DIMENSION_MAX_ROWS else SAMPLE_K
    try:
        values = wh.top_values(table["schema"], table["name"], col["name"], k)
    except Exception:
        return []
    return [str(v) for v in values] if len(values) <= k else []


def estimate_tokens(text: str) -> int:
    """~4 chars per token for English/SQL; close enough for budgeting, no tokenizer dependency."""
    return len(text) // 4


def diff_catalogs(old: dict, new: dict) -> list[str]:
    """Human-readable schema drift report for the nightly pipeline log."""
    changes = []
    ot, nt = old.get("tables", {}), new.get("tables", {})
    changes += [f"+ table {t}" for t in sorted(nt.keys() - ot.keys())]
    changes += [f"- table {t}" for t in sorted(ot.keys() - nt.keys())]
    for t in sorted(nt.keys() & ot.keys()):
        oc = {c["name"]: c["type"] for c in ot[t]["columns"]}
        nc = {c["name"]: c["type"] for c in nt[t]["columns"]}
        changes += [f"+ column {t}.{c}" for c in sorted(nc.keys() - oc.keys())]
        changes += [f"- column {t}.{c}" for c in sorted(oc.keys() - nc.keys())]
        changes += [f"~ column {t}.{c}: {oc[c]} -> {nc[c]}" for c in sorted(nc.keys() & oc.keys()) if oc[c] != nc[c]]
    return changes


def save_catalog(catalog: dict, path: str = config.CATALOG_PATH) -> None:
    body = json.dumps(catalog, indent=1, default=str)
    if path.startswith("s3://"):
        import boto3

        bucket, key = path[5:].split("/", 1)
        boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="application/json")
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(body, encoding="utf-8")


def read_catalog(path: str = config.CATALOG_PATH) -> dict | None:
    if path.startswith("s3://"):
        import boto3

        bucket, key = path[5:].split("/", 1)
        try:
            return json.loads(boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read())
        except Exception:
            return None
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


@lru_cache(maxsize=4)
def load_catalog(path: str = config.CATALOG_PATH) -> dict:
    """Cached per process, so warm Lambda invocations skip the S3 read."""
    catalog = read_catalog(path)
    if catalog is None:
        raise FileNotFoundError(f"No catalog at {path}. Run: python scripts/extract_schema.py")
    return catalog
