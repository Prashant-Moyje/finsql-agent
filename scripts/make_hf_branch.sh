#!/usr/bin/env bash
# Rebuild the `hf-space` branch from main, for Hugging Face Spaces:
#   - drops docs/screenshots (HF rejects binaries outside Xet/LFS storage)
#   - prepends the Space's YAML front-matter (Gradio SDK, app.py), because
#     ZeroGPU hardware is Gradio-only
#   - swaps in the slim requirements (no streamlit/selenium)
# Usage: bash scripts/make_hf_branch.sh   then: git push hf hf-space:main --force
set -euo pipefail
cd "$(dirname "$0")/.."

# Refuse to run with uncommitted changes: `git add -A` on the hf-space branch
# would sweep them into the Space commit and leave main without them.
if [ -n "$(git status --porcelain)" ]; then
  echo "Commit or stash your changes first:" >&2; git status --short >&2; exit 1
fi

start_branch=$(git rev-parse --abbrev-ref HEAD)
git checkout -q -B hf-space main

rm -rf docs/screenshots
{ cat <<'FRONTMATTER'
---
title: FinSQL - Text-to-SQL for Finance
emoji: 📊
colorFrom: blue
colorTo: indigo
sdk: gradio
app_file: app.py
pinned: false
license: mit
---

FRONTMATTER
cat README.md; } > README.hf && mv README.hf README.md   # HF reads its config from this block
sed -i '/docs\/screenshots/d' README.md          # image links to files that aren't on this branch
cp requirements-hf.txt requirements.txt

git add -A
git commit -q -m "Gradio demo for Hugging Face Spaces"
# HF's pre-receive hook scans every pushed commit, not just the tip, so earlier
# commits that still contain the screenshots would get the push rejected.
# Re-root the branch as a single commit holding only the final tree.
tree=$(git rev-parse HEAD^{tree})
git checkout -q "$start_branch"
root=$(git commit-tree "$tree" -m "FinSQL Gradio demo for Hugging Face Spaces")
git branch -f hf-space "$root"
echo "hf-space branch built at $(git rev-parse --short hf-space) (single commit, no history)"
echo "push it with:  git push hf hf-space:main --force"
echo "back on $start_branch"
