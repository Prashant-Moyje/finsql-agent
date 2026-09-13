#!/usr/bin/env bash
# Rebuild the `hf-space` branch from main, for Hugging Face Spaces:
#   - drops docs/screenshots (HF rejects binaries outside Xet/LFS storage)
#   - switches the README front-matter to the Gradio SDK (app.py), because
#     ZeroGPU hardware is Gradio-only
#   - swaps in the slim requirements (no streamlit/selenium)
# Usage: bash scripts/make_hf_branch.sh   then: git push hf hf-space:main --force
set -euo pipefail
cd "$(dirname "$0")/.."

start_branch=$(git rev-parse --abbrev-ref HEAD)
git checkout -q -B hf-space main

rm -rf docs/screenshots
sed -i 's/^sdk: streamlit$/sdk: gradio/; s/^app_file: streamlit_app.py$/app_file: app.py/' README.md
sed -i '/docs\/screenshots/d' README.md          # image links to files that aren't on this branch
cp requirements-hf.txt requirements.txt

git add -A
git commit -q -m "Gradio demo for Hugging Face Spaces"
echo "hf-space branch built at $(git rev-parse --short HEAD)"
echo "push it with:  git push hf hf-space:main --force"
git checkout -q "$start_branch"
echo "back on $start_branch"
