#!/bin/sh
set -eu

# Each runtime's hooks file passes its own name.
case "${1:-}" in
  claude) sheet="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/pstack-models.md" ;;
  codex) sheet="${CODEX_HOME:-$HOME/.codex}/pstack-models.md" ;;
  *)
    echo "session-start.sh: unknown runtime '${1:-}' (expected claude or codex)" >&2
    exit 2
    ;;
esac

# Plugin hooks provide CLAUDE_PLUGIN_ROOT. A project-level installation keeps
# this script beside its context file instead, so make the neighboring
# directory the fallback when the plugin variable is absent.
hook_root="${CLAUDE_PLUGIN_ROOT:+${CLAUDE_PLUGIN_ROOT}/hooks}"
if [ -z "$hook_root" ]; then
  hook_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
fi

bom=$(printf '\357\273\277')
if [ -f "$sheet" ] && [ -r "$sheet" ] && sed "1s/^$bom//" "$sheet" | tr -d '\r' | grep -qx 'session hook: off'; then
  exit 0
fi

# A CLAUDE.md symlink to AGENTS.md is shared by Claude and Codex. When Codex
# setup owns the mandate in that shared file, Claude already receives it from
# the instructions file; avoid injecting a second copy through the hook.
if [ "$1" = claude ]; then
  project_dir="${CLAUDE_PROJECT_DIR:-}"
  case "$hook_root" in
    */.claude/hooks) [ -n "$project_dir" ] || project_dir=$(CDPATH= cd -- "$hook_root/../.." && pwd) ;;
  esac
  if [ -n "$project_dir" ]; then
    instructions_file="${project_dir}/CLAUDE.md"
  fi
  if [ -n "${instructions_file:-}" ] && [ -f "$instructions_file" ]; then
    if grep -qF '<!-- BEGIN engineering:patina-mode' "$instructions_file" &&
       grep -qF '<!-- END engineering:patina-mode -->' "$instructions_file"; then
      exit 0
    fi

  fi
fi

cat "${hook_root}/session-start-context.md"
