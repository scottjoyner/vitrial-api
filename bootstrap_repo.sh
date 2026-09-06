#!/usr/bin/env bash
set -euo pipefail

OWNER="${GITHUB_OWNER:-scottjoyner}"
NAME="${1:-vitrial-api}"
VISIBILITY="${GITHUB_VISIBILITY:-public}"

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v gh >/dev/null || { echo "GitHub CLI (gh) is required" >&2; exit 1; }
gh auth status >/dev/null

if [[ ! -d .git ]]; then
  git init
  git add .
  git commit -m "Bootstrap Vitrial Connected Operations backend"
fi

if gh repo view "$OWNER/$NAME" >/dev/null 2>&1; then
  echo "Repository $OWNER/$NAME already exists."
  if ! git remote get-url origin >/dev/null 2>&1; then
    git remote add origin "git@github.com:$OWNER/$NAME.git"
  fi
  git push -u origin HEAD:main
else
  gh repo create "$OWNER/$NAME" "--$VISIBILITY" --source=. --remote=origin --push
fi

echo "Backend repository ready: https://github.com/$OWNER/$NAME"
