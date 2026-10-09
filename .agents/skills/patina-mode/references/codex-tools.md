# Codex tool mapping for pstack

pstack skills are written in Claude Code tool language (the `Skill` tool, the `Agent` tool, `AskUserQuestion`, `claude-*` model slugs). On Codex the skills are the same files; only the tool names resolve differently. Read this when a pstack skill names a Claude tool, a Claude built-in skill, or a `claude-*` model. This file is Codex-specific. Gemini CLI, opencode, Prime Agent, and other runtimes must use their own concrete tools, model names, and configuration paths.

## Tool actions

| pstack / Claude action | Codex equivalent |
|------------------------|------------------|
| Read a file | `shell` (`cat`, `head`, `tail`) |
| Create / edit / delete a file | `apply_patch` |
| Run a shell command | `shell` |
| Search file contents / find files | `shell` (`rg`, `grep`, `find`, `ls`) |
| Fetch a URL | `shell` with `curl` / `wget` |
| Search the web | `web_search` |
| Invoke a skill (the `Skill` tool, `/command`) | Skills load natively. Follow the instructions presented. |
| Invoke a skill by its `engineering:` name | Codex documents no `plugin:skill` syntax. Use the bare skill name. `@` addresses this plugin and its bundled skills as `engineering`. |
| Dispatch a subagent (the `Agent`/`Task` tool) | `spawn_agent` |
| Dispatch N parallel subagents in one turn | N `spawn_agent` calls in one response |
| Wait for a subagent result | `wait_agent` |
| Free a finished subagent slot | `close_agent` |
| Track tasks (the todolist / `TodoWrite`) | `update_plan` |
| Ask the human a fixed-choice question (`AskUserQuestion`) | Ask in plain text and let the user answer. Codex has no structured-choice tool. |

Subagent dispatch needs `multi_agent` enabled. Add to `~/.codex/config.toml`:

```toml
[features]
multi_agent = true
```

Without it, `spawn_agent` is unavailable and the fan-out skills (`interrogate`, `why`, `how`, `arena`, `reflect`) degrade to a single sequential pass.

## Subagent policy

patina-mode's Subagents section sets Claude-specific defaults (`subagent_type: "engineering:patina-agent"`, `run_in_background: true`). On Codex:

- There is no `patina-agent` subagent type. Route an ad-hoc subagent through patina-mode's style by dispatching a `spawn_agent` whose instructions tell it to read the `patina-mode` skill in full first.
- `spawn_agent` calls already run concurrently with your turn, so `run_in_background: true` has no separate flag. Issue the dispatch and continue.
- There is no `comment-sicko` subagent type either. The **no-comments** skill spawns it on Claude Code; on Codex dispatch a `spawn_agent` whose instructions tell it to read `patina-mode/references/agents/comment-sicko.md` in full first.
- Claude Code runs every subagent on this machine, so the **swarm** skill's workers and the fan-out playbooks (`orchestrate`, `autopilot-full`, `autopilot-stack`) isolate writers with worktrees. The same holds on Codex.
- Keep the rest of the policy unchanged. Pass file pointers not inlined context, give each worker its own worktree or branch when they write, review every subagent's diff yourself.

## Model names

Skills name Claude defaults (a single-role default for code/prose/judgment plus a diverse-model panel for diverse-model panels; each model-consuming skill lists its own in a Models section). These slugs do not resolve on Codex. Substitute your configured Codex models:

- Single-model roles: your primary Codex model (for example `gpt-5.6-sol`).
- Roles that default to the strongest Claude model (`bug-fix`, `perf-issue`, `hillclimb`, `strongest judgment`): your strongest Codex model (for example `gpt-6-astra`).
- Diverse-model panels (`arena`, `architect`, `interrogate`, `how` critics, `reflect`): the adversarial signal comes from model diversity, so use the distinct Codex models available to you. A good default quad on ChatGPT is `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`. If only one model family is reachable, vary reasoning effort and note in the verdict that diversity was reduced.

`/setup-pstack` writes the configured model list. On Codex, set it to your Codex model slugs.

## Session routing hook

The native Engineering plugin bundles the same `SessionStart` routing instruction as the Claude Code plugin, through `session-start.sh` on macOS and Linux and `session-start.ps1` on Windows. Codex runs the hook on startup, resume, clear, and compact after the user trusts the hook through `/hooks`. The hook reads `session hook` from the Codex sheet, at the path in [setup-pstack's runtime table](../../setup-pstack/SKILL.md#other-runtimes); `session hook: off` disables injection.

A skills-only installation does not include plugin hooks. Request `patina-mode` explicitly or add a standing instruction to `AGENTS.md` in that case.

## Claude built-in skills pstack references

Some triggers name skills that ship with Claude Code, not pstack. They do not exist on Codex. Substitute the behavior:

| Claude built-in named in pstack | On Codex |
|---------------------------------|----------|
| `run` (drive a CLI/TUI to see a change work) | Run the app yourself via `shell` and observe the real output. |
| `patinaproject-verify` (drive a UI to confirm a fix) | Load the repository-local `patinaproject-verify` skill first, then drive the UI with its instructions and available automation. Do not claim done without observing the artifact. |
| `plugin-dev:skill-development` (Claude's SKILL.md authoring guidance) | Follow your platform's skill-authoring guidance; the `writing-skills` skill if present. Keep `name` + `description` frontmatter and progressive disclosure. |
| `loop` (recurring/self-paced re-invocation, used by `babysit`) | Codex has no `loop` skill. Re-run the step yourself on a cadence, or use a Codex scheduled task if available. |

## Per-skill notes

Affected skill entry points and the optional Codex slash stubs point here. Most skills need only the tables above. These need one more mapping:

| Skill | On Codex |
|-------|----------|
| `interrogate` | The `subagent_type`/`model`/`readonly` dispatch fields map to `spawn_agent`; substitute your configured Codex models and keep the reviewer panel model-diverse. |
| `setup-pstack` | The override sheet is `~/.codex/pstack-models.md`, the slugs are your Codex models (see Model names above), and you load it by adding the sheet's contents to `~/.codex/AGENTS.md`; Codex has no `@`-include into a rules file. The role rows in step 5 are identical. |
| `no-comments` | There is no `comment-sicko` subagent type; see Subagent policy above. |
| `teach` | Running `how` and `why` in parallel maps to `spawn_agent` fan-out; image generation uses the configured Codex equivalent. |
| `create-verification-skill` | The generated skill lands under `.claude/skills/patinaproject-verify/` on Claude Code; write it to Codex's project-skill location instead. The app-driving harness is platform-neutral. |
| `maintain-verification-skill` | The parallel per-feature source readers map to `spawn_agent` fan-out; the project-local skill lives under Codex's skills location, not `.claude/skills/`. |
| `babysit` | `loop` and `AskUserQuestion` resolve through the tables above. |
| `automate-me` | `plugin-dev:skill-development` resolves through the built-in skills table above. |

### Code-review presentation

The shared `code-review` contract stays runtime-neutral: both axes use ordinary
Markdown findings with exact file and line references, and both runtimes write
the same typed review record for readiness. Codex may add one inline directive
for each discrete, actionable finding that belongs to a changed line:

```text
::code-comment{title="Short issue" body="Actionable explanation" file="/absolute/worktree/src/example.ts" start=12 end=13 priority=1}
```

Keep the Markdown finding beside the directive. Use the active worktree path
for `file`, keep `start` and `end` tight, and omit directives when there are no
actionable changed-line findings. A directive is UI presentation metadata; it
does not enter the review record or replace its head pinning, separate
Standards and Spec results, finding dispositions, or current-head validation.
Claude emits the Markdown finding without Codex directives.

## Vendored scripts

`scripts/` in this skill's base directory ships the `watch-pr` PR watcher, the `orch` store CLI, the issue handoff route entries under `issue-routes/`, and `worktree-audit.mjs`. Join that base directory to the path before invoking them. They are plain bun, Node.js, and bash, so they run the same on Codex; invoke them through `shell`. They need `bun`, `node`, `gh`, and (for stack work) `gt`. `worktree-audit.mjs` scans Codex sessions under `$CODEX_HOME/sessions` and `$CODEX_HOME/archived_sessions` (default `~/.codex`), along with any Claude Code or Pi transcript directory that exists. It imports the transcript walker from `skills/reflect/scripts/find-transcript.mjs`, so keep the `reflect` skill installed beside `patina-mode`.

## Instructions file

Where a pstack skill says "your instructions file", on Codex that is `AGENTS.md` (project root, plus `~/.codex/AGENTS.md` global). On Claude Code it is `CLAUDE.md`.
