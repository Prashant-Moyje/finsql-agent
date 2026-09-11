"""Schema pipeline: warehouse metadata + semantic.yaml -> context store.

Runs nightly in GitHub Actions (.github/workflows/schema-refresh.yml) and
prints a drift report, so a renamed column shows up in the log before
finance users hit it.

Usage:  python scripts/extract_schema.py [--no-samples] [--out context_store/catalog.json]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finsql import config  # noqa: E402
from finsql.catalog import build_catalog, diff_catalogs, load_semantic, read_catalog, save_catalog  # noqa: E402
from finsql.warehouse import get_warehouse  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=config.CATALOG_PATH)
    ap.add_argument("--no-samples", action="store_true", help="skip sampling categorical values (saves warehouse credits)")
    args = ap.parse_args()

    wh = get_warehouse()
    catalog = build_catalog(wh, load_semantic(), sample_values=not args.no_samples)
    old = read_catalog(args.out)
    save_catalog(catalog, args.out)

    s = catalog["stats"]
    print(f"Catalog written to {args.out} from {wh.name}")
    print(f"  {s['tables']} tables, {s['columns']} columns ({s['excluded_tables']} tables excluded by semantic.yaml)")
    print(f"  full schema ~{s['full_schema_tokens']:,} tokens if pasted into a prompt")
    if old:
        changes = diff_catalogs(old, catalog)
        print(f"  schema drift since {old.get('generated_at')}: {len(changes)} change(s)")
        for c in changes[:50]:
            print("   ", c)


if __name__ == "__main__":
    main()
