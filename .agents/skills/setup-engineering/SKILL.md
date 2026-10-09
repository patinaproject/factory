---
name: setup-engineering
description: Install Engineering's repo-level machinery and configure model routing for skills-only consumers. Use for /setup-engineering or setting Engineering up in a repository.
menu-description: install machinery and configure model routing
---

# Setup Engineering

Engineering ships as a full plugin (hook, subagents) and also as a vendored
skill catalog. Vendoring carries the skills but not the hook or the agents, so a
skills-only consumer loses the default-on entry point and the Claude-native
targets that provider dispatch names. This skill installs that machinery into
the target repo and, on Codex, the repo-scoped Codex config.

The machinery install is non-interactive and idempotent.

## Install repo-level machinery

`scripts/install-machinery.sh` (in this skill's base directory) owns the
idempotency and no-clobber contract. Run it rather than editing the target files
by hand. It copies managed hook and agent files on every run, upserts one
Engineering `SessionStart` entry while preserving unrelated hooks, and removes
the old Claude mandate block. Re-running refreshes managed files without
duplicating configuration.

The installer first checks that the sibling `setup-pstack` skill and the
`patina-mode` Codex tool reference are present. If any required file is
missing, the install fails and tells the reader to install the full Engineering
skill catalog.

What it materializes from `assets/`:

- The **Engineering hook** and its context (from `assets/hooks/`) into the
  repo's `.claude/hooks/`. The skills-only context uses bare skill names, while
  the plugin source keeps its `engineering:` namespace.
- The **Engineering agents** (from `assets/agents/`) into the repo's
  `.claude/agents/`, so `subagent_type: "patina-agent"` resolves and
  no-comments can reach `comment-sicko`. Model choice per role comes from the
  `setup-pstack` sheet at runtime, not from an agent definition.
- One `SessionStart` entry matching `startup|resume|clear|compact` in
  `.claude/settings.json`; existing hooks remain in place.

Run it from the target repo:

```bash
bash "<this-skill>/scripts/install-machinery.sh"
```

The script defaults `--repo` to the current Git toplevel and the instructions
file to `<repo>/CLAUDE.md`. Pass `--repo <dir>` or `--instructions <file>` to
choose the legacy instructions file whose managed block should be removed.

### Codex

Codex has no `SessionStart` hook and no subagent registry. There the default-on
trigger is the same mandate block in the repo's `AGENTS.md`, which Codex reads
per project, and delegates route through the spawn-prompt convention in the
patina-mode `references/codex-tools.md` (dispatch a `spawn_agent` told to read
the `patina-mode` skill, or `agents/comment-sicko.md`, first). Subagent dispatch
also needs the `multi_agent` feature. Add `--codex` to install both:

```bash
bash "<this-skill>/scripts/install-machinery.sh" --codex
```

That upserts the mandate block into `<repo>/AGENTS.md` and enables `multi_agent`
under `[features]` in `<repo>/.codex/config.toml`, both idempotently. When
`CLAUDE.md` is a symlink to `AGENTS.md`, Claude sees the existing managed block
through the link and the installed hook suppresses duplicate output; Codex
still reads the same block. Writing the repo-scoped config rather than the
user's global `~/.codex/config.toml` keeps the machinery committed and shared
across contributors. Codex honors a repo-scoped
`.codex/config.toml` only for a project the user has marked trusted, so the flag
takes effect after Codex's one-time trust prompt. Override the paths with
`--codex-agents <file>` and `--codex-config <file>`.

Skills load natively by name on Codex. The `/command` stubs under
`.codex-plugin/prompts/*` come from the pstack-claude base; the installer does
not write or edit them.

## Configure model routing

After the installer succeeds, read its final `next skill:` line. Load the skill
at that path and run it. Let `setup-pstack` own its model choices, probes,
confirmation, and writes.

## Confirm

Tell the user what the machinery install wrote: the mandate block, the
Engineering agents, and on Codex the `multi_agent` flag. Relay `setup-pstack`'s
final report. Report setup-engineering complete only when `setup-pstack`
succeeds. Otherwise, report it incomplete and include the reason
`setup-pstack` reported when it stopped.
