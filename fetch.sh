#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

python3 starters.py --json
python3 prospects.py --json

git add docs/data.json docs/prospects.json
git diff --cached --quiet && echo "No changes to commit." && exit 0
git commit -m "Update starters $(date -u +%Y-%m-%d)"
git push
