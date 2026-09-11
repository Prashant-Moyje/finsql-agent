#!/usr/bin/env bash
# Assembles build/ for the Lambda zip: code + context store (+ demo DuckDB).
# Usage: bash deploy/build.sh [--snowflake]
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf build && mkdir -p build/data
cp -r finsql semantic.yaml context_store build/
if [[ "${1:-}" == "--snowflake" ]]; then
  cp requirements-snowflake.txt build/requirements.txt
  cp requirements.txt build/  # referenced by -r
else
  cp requirements.txt build/requirements.txt
  cp data/finance.duckdb build/data/  # demo warehouse, opened read-only
fi
echo "build/ ready. Next:"
echo "  aws ssm put-parameter --type SecureString --name /finsql/SLACK_BOT_TOKEN --value xoxb-..."
echo "  aws ssm put-parameter --type SecureString --name /finsql/SLACK_SIGNING_SECRET --value ..."
echo "  aws ssm put-parameter --type SecureString --name /finsql/GROQ_API_KEY --value gsk_..."
echo "  sam build -t deploy/template.yaml --use-container && sam deploy --guided"
