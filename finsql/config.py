"""Central settings. Everything comes from env vars (or `.env` locally). On
Lambda, secrets are pulled from SSM Parameter Store at cold start, because
SSM standard parameters are free and Secrets Manager is not."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _load_ssm(prefix: str) -> None:
    """Copies every parameter under `prefix` (e.g. /finsql/) into os.environ,
    so /finsql/SLACK_BOT_TOKEN becomes $SLACK_BOT_TOKEN."""
    import boto3  # available in the Lambda runtime; not needed locally

    ssm = boto3.client("ssm")
    for page in ssm.get_paginator("get_parameters_by_path").paginate(
        Path=prefix, WithDecryption=True, Recursive=True
    ):
        for p in page["Parameters"]:
            os.environ.setdefault(p["Name"].rsplit("/", 1)[-1], p["Value"])


if os.getenv("SSM_PREFIX"):
    _load_ssm(os.environ["SSM_PREFIX"])


def env(key: str, default: str = "") -> str:
    return os.getenv(key) or default


# --- Warehouse ---------------------------------------------------------------
WAREHOUSE = env("WAREHOUSE", "duckdb")  # "duckdb" (local demo) or "snowflake"
DUCKDB_PATH = env("DUCKDB_PATH", str(ROOT / "data" / "finance.duckdb"))

SNOWFLAKE_ACCOUNT = env("SNOWFLAKE_ACCOUNT")
SNOWFLAKE_USER = env("SNOWFLAKE_USER")
SNOWFLAKE_PRIVATE_KEY_PATH = env("SNOWFLAKE_PRIVATE_KEY_PATH")  # key-pair auth, no passwords
SNOWFLAKE_PRIVATE_KEY = env("SNOWFLAKE_PRIVATE_KEY")  # PEM contents (Lambda/CI alternative to a path)
SNOWFLAKE_ROLE = env("SNOWFLAKE_ROLE", "AI_READER")
SNOWFLAKE_WAREHOUSE = env("SNOWFLAKE_WAREHOUSE", "AI_WH")
SNOWFLAKE_DATABASE = env("SNOWFLAKE_DATABASE", "FINANCE")
SNOWFLAKE_SCHEMA = env("SNOWFLAKE_SCHEMA", "REPORTING")

# --- Context store -------------------------------------------------------------
# Local path or s3://bucket/key. The nightly pipeline writes it; the agent reads it.
CATALOG_PATH = env("CATALOG_PATH", str(ROOT / "context_store" / "catalog.json"))
SEMANTIC_PATH = env("SEMANTIC_PATH", str(ROOT / "semantic.yaml"))

# --- LLM -----------------------------------------------------------------------
LLM_PROVIDER = env("LLM_PROVIDER", "ollama")  # "ollama" (local) or "groq" (hosted free tier)
OLLAMA_URL = env("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = env("OLLAMA_MODEL", "qwen3:8b")
GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-120b")

# --- Agent limits --------------------------------------------------------------
MAX_ATTEMPTS = int(env("MAX_ATTEMPTS", "3"))  # writer -> validator repair loop cap
MAX_ROWS = int(env("MAX_ROWS", "1000"))  # hard LIMIT injected into every query
QUERY_TIMEOUT_S = int(env("QUERY_TIMEOUT_S", "60"))
TOP_K_TABLES = int(env("TOP_K_TABLES", "6"))
SCHEMA_TOKEN_BUDGET = int(env("SCHEMA_TOKEN_BUDGET", "3500"))  # schema slice of the prompt
MAX_COLUMNS_PER_TABLE = int(env("MAX_COLUMNS_PER_TABLE", "25"))

# --- Slack ---------------------------------------------------------------------
SLACK_BOT_TOKEN = env("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = env("SLACK_SIGNING_SECRET")
SLACK_APP_TOKEN = env("SLACK_APP_TOKEN")  # xapp-... only for local Socket Mode
# Comma-separated Slack IDs. Empty = everyone in the workspace may ask.
ALLOWED_SLACK_USERS = {u for u in env("ALLOWED_SLACK_USERS").split(",") if u}
ALLOWED_SLACK_CHANNELS = {c for c in env("ALLOWED_SLACK_CHANNELS").split(",") if c}
