#!/usr/bin/env bash
# Commit everything and push to main. If main moved while this job ran (the admin page published, another workflow
# pushed, or a local push), rebase onto it, rebuild docs/ from the combined inputs, and try again.
# Usage: bash .github/scripts/push.sh "<commit message>"
set -euo pipefail
git config user.name "github-actions[bot]"
git config user.email "41898283+github-actions[bot]@users.noreply.github.com"

git add -A
git diff --cached --quiet && { echo "Nothing changed."; exit 0; }
git commit -q -m "$1"

for attempt in 1 2 3 4; do
  git push origin HEAD:main && exit 0
  echo "main moved; rebasing and rebuilding (attempt $attempt)"
  git fetch -q origin main
  # On a conflict keep this job's version (fresh data); docs/ is regenerated right after anyway.
  git rebase -q -X theirs origin/main
  python ffr.py build
  git add -A
  git diff --cached --quiet || git commit -q -m "$1 (rebuild)"
  sleep $((attempt * 5))
done
echo "Couldn't push after several tries." >&2
exit 1
