#!/usr/bin/env bash
# install-machinery.sh — install Code's repo-level machinery for
# skills-only consumers. Managed files are copied on every run so an existing
# install converges on the bundled version; unrelated files and hook entries
# are preserved.
#
# Usage:
#   install-machinery.sh [--repo <dir>] [--instructions <file>] [--codex-agents <file>]
#                        [--codex] [--codex-config <file>]
#
# --codex-agents names the Codex AGENTS.md whose legacy managed mandate block the
# installer removes on every run. The Codex plugin hook delivers the mandate.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ASSETS_DIR="$(cd "${SCRIPT_DIR}/../assets" && pwd)"
SKILLS_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SETUP_PSTACK_SKILL="${SKILLS_DIR}/setup-pstack/SKILL.md"

BEGIN_MARKER='<!-- BEGIN engineering:patina-mode (managed by setup-engineering; re-running overwrites this block) -->'
END_MARKER='<!-- END engineering:patina-mode -->'
SESSION_MATCHER='startup|resume|clear|compact'

repo=""
instructions=""
do_codex=0
codex_config=""
codex_agents=""

fail() {
  echo "install-machinery.sh: $1" >&2
  exit 1
}

require_file() {
  local path="$1"
  [ -f "$path" ] || fail "missing required file: $path. Install the full Code skill catalog, including setup-pstack and patina-mode references, then rerun setup-code-skills."
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    --instructions) instructions="$2"; shift 2 ;;
    --codex) do_codex=1; shift ;;
    --codex-config) codex_config="$2"; shift 2 ;;
    --codex-agents) codex_agents="$2"; shift 2 ;;
    *) echo "install-machinery.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$repo" ]; then
  repo="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi
[ -n "$instructions" ] || instructions="${repo}/CLAUDE.md"
[ -n "$codex_config" ] || codex_config="${repo}/.codex/config.toml"
[ -n "$codex_agents" ] || codex_agents="${repo}/AGENTS.md"

command -v jq >/dev/null || fail "jq is required to update .claude/settings.json"
require_file "$SETUP_PSTACK_SKILL"
require_file "${SKILLS_DIR}/patina-mode/references/codex-tools.md"
require_file "${SKILLS_DIR}/setup-pstack/scripts/read-sheet.sh"
require_file "${ASSETS_DIR}/hooks/session-start.sh"
require_file "${ASSETS_DIR}/hooks/session-start-context.md"

# Rewrite through the destination path so a symlink such as CLAUDE.md ->
# AGENTS.md keeps pointing at its original target.
write_in_place() {
  local dest="$1" src="$2"
  cat "$src" > "$dest"
  rm -f "$src"
}

# Remove every complete managed mandate block and leave all other instructions
# byte-for-byte intact. An unclosed marker is preserved rather than swallowing
# content beneath it.
remove_managed_blocks() {
  local file="$1" tmp
  [ -f "$file" ] || return 0
  grep -qF "$BEGIN_MARKER" "$file" || return 0
  grep -qF "$END_MARKER" "$file" || return 0
  tmp="$(mktemp)"
  awk -v b="$BEGIN_MARKER" -v e="$END_MARKER" '
    function flush(   i) { for (i = 1; i <= nbuf; i++) print buf[i]; nbuf = 0 }
    $0 == b { flush(); inblock = 1; buf[++nbuf] = $0; next }
    inblock && $0 == e { inblock = 0; nbuf = 0; next }
    inblock { buf[++nbuf] = $0; next }
    { print }
    END { flush() }
  ' "$file" > "$tmp" || { rm -f "$tmp"; return 0; }
  if cmp -s "$file" "$tmp"; then rm -f "$tmp"; return 0; fi
  write_in_place "$file" "$tmp"
  echo "mandate block removed: ${file}"
}

# Ensure [features] multi_agent = true exactly once, preserving all unrelated
# TOML sections and keys.
enable_multi_agent() {
  local config="$1" tmp
  mkdir -p "$(dirname "$config")"
  if [ ! -f "$config" ]; then
    printf '[features]\nmulti_agent = true\n' > "$config"
    return
  fi
  tmp="$(mktemp)"
  awk '
    /^\[features\][ \t]*$/ { have_features = 1; in_features = 1; print; next }
    /^\[/ {
      if (in_features && !set) { print "multi_agent = true"; set = 1 }
      in_features = 0; print; next
    }
    {
      if (in_features && $0 ~ /^[ \t]*multi_agent[ \t]*=/) {
        if (!set) { print "multi_agent = true"; set = 1 }
        next
      }
      print
    }
    END {
      if (in_features && !set) { print "multi_agent = true"; set = 1 }
      if (!have_features) { print ""; print "[features]"; print "multi_agent = true" }
    }
  ' "$config" > "$tmp"
  write_in_place "$config" "$tmp"
}

install_managed_file() {
  local source="$1" destination="$2"
  mkdir -p "$(dirname "$destination")"
  cp "$source" "$destination"
  echo "installed: ${destination}"
}

upsert_claude_session_hook() {
  local settings="${repo}/.claude/settings.json"
  local command='"$CLAUDE_PROJECT_DIR/.claude/hooks/session-start.sh" claude'
  mkdir -p "$(dirname "$settings")"
  if [ ! -f "$settings" ]; then
    jq -n --arg matcher "$SESSION_MATCHER" --arg command "$command" \
      '{hooks:{SessionStart:[{matcher:$matcher,hooks:[{type:"command",command:$command}]}]}}' > "$settings"
    return
  fi
  local tmp
  tmp="$(mktemp)"
  jq --arg matcher "$SESSION_MATCHER" --arg command "$command" '
    def managed_command:
      (.type? == "command") and
      (((.command? // "") | contains(".claude/hooks/session-start.sh")) or
       ((.command? // "") | contains("plugins/code/hooks/session-start.sh")) or
       ((.command? // "") | contains("plugins/engineering/hooks/session-start.sh")));
    .hooks = (.hooks // {}) |
    .hooks.SessionStart = [
      (.hooks.SessionStart // [])[] as $entry |
      [($entry.hooks // [])[] | select(managed_command | not)] as $kept |
      if ($kept | length) > 0 then $entry | .hooks = $kept
      elif (($entry.hooks // []) | length) == 0 then $entry
      else empty end
    ] + [{matcher:$matcher,hooks:[{type:"command",command:$command}]}]
  ' "$settings" > "$tmp"
  write_in_place "$settings" "$tmp"
}

agents_dir="${repo}/.claude/agents"
for agent_asset in "${ASSETS_DIR}"/agents/*.md; do
  [ -f "$agent_asset" ] || fail "missing required file: ${ASSETS_DIR}/agents/*.md. Install the full Code skill catalog, including setup-pstack and patina-mode references, then rerun setup-code-skills."
  install_managed_file "$agent_asset" "${agents_dir}/$(basename "$agent_asset")"
done
install_managed_file "${ASSETS_DIR}/hooks/session-start.sh" "${repo}/.claude/hooks/session-start.sh"
chmod +x "${repo}/.claude/hooks/session-start.sh"
install_managed_file "${SKILLS_DIR}/setup-pstack/scripts/read-sheet.sh" "${repo}/.claude/hooks/read-sheet.sh"
install_managed_file "${ASSETS_DIR}/hooks/session-start-context.md" "${repo}/.claude/hooks/session-start-context.md"

# Claude receives the mandate from the project hook and Codex from the plugin
# hook, so no instructions file carries it. Remove managed blocks that an
# earlier install wrote to CLAUDE.md or AGENTS.md, or either file would deliver
# the mandate a second time.
remove_managed_blocks "$instructions"
if ! [ "$codex_agents" -ef "$instructions" ]; then
  remove_managed_blocks "$codex_agents"
fi
upsert_claude_session_hook

if [ "$do_codex" -eq 1 ]; then
  enable_multi_agent "$codex_config"
  echo "multi_agent enabled: ${codex_config}"
fi

echo "next skill: ${SETUP_PSTACK_SKILL}"
