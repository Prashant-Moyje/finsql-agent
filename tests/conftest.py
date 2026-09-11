import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from finsql.catalog import build_catalog, load_semantic  # noqa: E402
from finsql.warehouse import DuckDBWarehouse  # noqa: E402


@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> str:
    import seed_duckdb

    path = tmp_path_factory.mktemp("wh") / "test.duckdb"
    seed_duckdb.main(["--noise-tables", "40", "--out", str(path), "--quiet"])
    return str(path)


@pytest.fixture(scope="session")
def warehouse(db_path) -> DuckDBWarehouse:
    return DuckDBWarehouse(db_path)


@pytest.fixture(scope="session")
def catalog(warehouse) -> dict:
    return build_catalog(warehouse, load_semantic(str(ROOT / "semantic.yaml")))


class ScriptedLLM:
    """Returns canned responses in order, and records every prompt it was sent."""
    name = "scripted"

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        return self.responses.pop(0)


@pytest.fixture
def scripted():
    return ScriptedLLM
