#!/usr/bin/env bash
set -euo pipefail

canonical_remote='git@github-cyhuh428:cyhuh428-sinsan/CGA-alone.git'
: "${HEAD_BRANCH:?HEAD_BRANCH is required}"
: "${HEAD_SHA:?HEAD_SHA is required}"

test "$(git rev-parse HEAD)" = "$HEAD_SHA"

if git remote get-url development >/dev/null 2>&1; then
  test "$(git remote get-url development)" = "$canonical_remote"
else
  git remote add development "$canonical_remote"
fi

git update-ref refs/remotes/development/main refs/remotes/origin/main
git update-ref "refs/remotes/development/$HEAD_BRANCH" "$HEAD_SHA"
git branch --set-upstream-to="development/$HEAD_BRANCH" "$HEAD_BRANCH"

git diff --check refs/remotes/origin/main...HEAD
