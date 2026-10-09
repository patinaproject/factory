#!/usr/bin/env bash
# worktree-setup.sh
#
# Prepare a fresh checkout or worktree for work: sync the default branch and
# install dependencies. Shared by Claude Code (a SessionStart hook in
# .claude/settings.json) and Codex (the [setup] block in
# .codex/environments/environment.toml) so both agents run identical setup.
#
# Safe to re-run: the fast-forward merge only advances when HEAD is already an
# ancestor of origin/main (so feature branches with their own commits are left
# untouched), and `pnpm env:setup` is idempotent.
#
# Branch sync is best-effort: it runs as a SessionStart hook, so a network or
# remote failure (e.g. offline) must warn rather than abort, otherwise the
# essential `pnpm env:setup` step below would never run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if git fetch --prune origin main; then
  if git merge-base --is-ancestor HEAD origin/main; then
    git merge --ff-only origin/main ||
      echo "worktree-setup: warning: fast-forward failed; skipping branch sync" >&2
  fi
else
  echo "worktree-setup: warning: could not fetch origin/main; skipping branch sync" >&2
fi

pnpm env:setup
