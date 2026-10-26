---
name: setup-code-skills
description: Install Code's repo-level machinery and configure model routing for skills-only consumers. Use for /setup-code-skills or setting Code up in a repository.
menu-description: install machinery and configure model routing
---

# Set Up Code Skills

Code ships as a full plugin (hook, subagents) and also as a vendored
skill catalog. Vendoring carries the skills but not the hook or the agents, so a
skills-only consumer loses the default-on entry point and the Claude-native
targets that provider dispatch names. This skill installs that machinery into
the target repo and, on Codex, the repo-scoped Codex config.

The machinery install is non-interactive and idempotent.

## Install repo-level machinery

`scripts/install-machinery.sh` (in this skill's base directory) owns the
idempotency and no-clobber contract. Run it rather than editing the target files
by hand. It copies managed hook and agent files on every run, upserts one
Code `SessionStart` entry while preserving unrelated hooks, and removes
the legacy mandate blocks. Re-running refreshes managed files without
duplicating configuration.

The installer first checks that the sibling `setup-pstack` skill and the
`patina-mode` Codex tool reference are present. If any required file is
missing, the install fails and tells the reader to install the full Code
skill catalog.

What it materializes from `assets/`:

- The **Code hook** and its context (from `assets/hooks/`), plus the
  `setup-pstack` sheet decoder the hook runs, into the repo's `.claude/hooks/`.
  The context names skills by their bare names.
- The **Code agents** (from `assets/agents/`) into the repo's
  `.claude/agents/`, so `subagent_type: "patina-agent"` resolves, the effort
  agents `patina-agent-<level>` and `effort-<level>` resolve, and no-comments
  can reach `comment-sicko`. The agents keep their bare names, so the same
  names resolve when Code is also installed as a plugin. Model choice
  per role comes from the `setup-pstack` sheet at runtime, not from an agent
  definition.
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

The Code plugin's Codex `SessionStart` hook, registered in
`.codex-plugin/plugin.json`, delivers the mandate on Codex. Codex asks the user
to trust plugin hooks through `/hooks` before it runs them. This installer
writes no mandate block to `AGENTS.md`, so Codex never receives the mandate
twice. A skills-only Codex install carries no plugin hook, so there the user
requests `patina-mode` explicitly. Codex has no subagent registry, so delegates
route through the spawn-prompt convention in the patina-mode
`references/codex-tools.md` (dispatch a `spawn_agent` told to read the
`patina-mode` skill, or `agents/comment-sicko.md`, first). Subagent dispatch
also needs the `multi_agent` feature. Add `--codex` to enable it:

```bash
bash "<this-skill>/scripts/install-machinery.sh" --codex
```

That enables `multi_agent` under `[features]` in `<repo>/.codex/config.toml`,
idempotently. Writing the repo-scoped config rather than the user's global
`~/.codex/config.toml` keeps the machinery committed and shared across
contributors. Codex honors a repo-scoped `.codex/config.toml` only for a project
the user has marked trusted, so the flag takes effect after Codex's one-time
trust prompt. Override the path with `--codex-config <file>`.

Every run, with or without `--codex`, removes the managed mandate block that an
earlier install wrote to `CLAUDE.md` or `AGENTS.md`, and leaves all other
content in those files untouched. `--codex-agents <file>` names a different
`AGENTS.md` to clean. A block without its closing marker stays in place.

Skills load natively by name on Codex. The `/command` stubs under
`.codex-plugin/prompts/*` come from the pstack-claude base; the installer does
not write or edit them.

## Configure model routing

After the installer succeeds, read its final `next skill:` line. Load the skill
at that path and run it. Let `setup-pstack` own its model choices, probes,
confirmation, and writes.

## Confirm

Tell the user what the machinery install wrote: the session hook, the
Code agents, any legacy mandate block it removed, and on Codex the
`multi_agent` flag. Relay `setup-pstack`'s
final report. Report setup-code-skills complete only when `setup-pstack`
succeeds. Otherwise, report it incomplete and include the reason
`setup-pstack` reported when it stopped.
