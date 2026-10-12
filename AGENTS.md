# Repository Guidelines

## Project Structure & Module Organization

This repository holds a reusable [Hermes Agent](https://github.com/NousResearch/hermes-agent)
software factory. It contains no factory-specific values: repositories, apps,
credentials, and model transport belong to each factory's own Hermes settings
and `.env`.

- `plugins/<name>/`: Hermes plugins, one directory per plugin
- `profiles/<name>/`: Hermes worker profiles and their scripts
- `templates/`: webhook route and triage prompt templates rendered from
  settings
- `skills/setup-factory/`: the agent runbook that sets up a factory on a
  machine
- `docs/`: contributor docs such as `docs/file-structure.md` and
  `docs/release-flow.md`
- `CLAUDE.md` imports this file
- root config: `package.json`, `commitizen.config.json`, `commitlint.config.js`, and `.husky/`

Never modify Hermes source to make a component work. Use Hermes' documented
extension points: plugins, hooks, profiles, skills, webhook routes, and cron.

Never commit a factory's real values: repository lists, Linear or GitHub app
identifiers, hostnames, model aliases, tokens, or secrets. Use placeholders in
templates and examples.

GitHub Issues are the canonical tracker for this public repository. Linear
receives issues through one-way GitHub-to-Linear intake for team visibility
and is not authoritative. Synced property updates are bidirectional, so do not
edit or close public-repository issues in Linear. Do not add committed design
or plan artifacts for routine issue work; put durable context on the GitHub
issue or in normal docs when it is broadly useful beyond one issue.

## Agent skills

### Issue tracker

Tracker operations are defined in the sole adapter. The real file is
`docs/agents/issue-tracker.md`; `docs/issue-tracker.md` is a compatibility
symlink to it, so either path reaches the same adapter.
Follow it directly for claiming, labels, lifecycle, relationships, and closure.
`docs/issue-publishing.md` governs issue body framing, and the adapter owns
readiness and priority.

### GitHub CLI on a factory machine

On a machine that runs a factory, act on GitHub as the factory's GitHub App,
never as a personal login. Run `gh` with `GH_CONFIG_DIR` set to the worker
profile's `gh` directory, and never run `gh auth login`. See
"Use `gh` on a factory machine" in
`skills/setup-factory/references/github.md`.

### Working an issue

When you begin or resume issue-linked work, run the `working-on-issues` skill
first, before branching, editing, or opening a pull request, when it is
available. It resolves the issue, lands you on the tracker-provided branch, and
marks it started after its gates pass.

### Triage labels

Triage roles map through the tracker adapter and the repository's configured
labels. See `docs/issue-tracker.md`.

### Domain docs

This is a single-context repository; domain docs are optional and created lazily when useful. See `docs/agents/domain.md`.

### Durable context capture

`CONTEXT.md` and `docs/adr/**` are in-force truth and change only on the branch
that publishes them: the branch implementing the decision, or a docs-only branch
when the repository already reflects it. A session anywhere else captures the
exact proposed doc text on the GitHub issue that will implement the decision
instead of editing the tree, creating that issue if none exists. The branch
implementing such an issue applies the captured text verbatim in its pull
request.

## Rules and what enforces them

When the operator corrects an agent, fix the mistake and add its rule here. A
rule that only docs enforce is a candidate for a script, check, or test.

| Rule | Enforced by |
| --- | --- |
| A factory machine's live install matches this repository's default branch after a merge | `profiles/claude-worker/scripts/check-install`, run hourly as a cron job and in setup part 5 and "Deploy a merged factory change" |
| Agents on a factory machine act on GitHub only as the App and publish only with `push-signed` | The machine-wide block from `skills/setup-factory/references/agent-instructions.md`; branch rules that require signed commits |
| A plain `git push` to GitHub fails on a factory machine, in any clone | `profiles/claude-worker/scripts/guard-git-push`, a global `pushInsteadOf` rewrite that setup installs and checks with `--check`; the per-checkout `pushurl` |
| `worker_entry` is a prefix, and the task prompt with the issue, URL, repository, and branch is always sent | `build_prompt` in `profiles/claude-worker/scripts/claude-session`; its `worker_entry` tests |
| A worker run reaches the toolchain that its repository's `toolchain_checks` name, or it does not start | `check_toolchain` in `claude-session`, run in the environment Claude Code gets; its toolchain tests |
| A worker's main agent runs on `claude_session.model` while its subagents run on `claude_session.tier_models`, and an invalid map refuses the run | `model_environment` and `tier_models` in `profiles/claude-worker/scripts/claude-session`, with their tests; `check_model_tiers` in `check-install`, which reports a `model` with no `tier_models.sonnet`; docs only for the ids, which stay in the private record |
| A card's Claude session resumes only while the prompt cache is warm (5 minutes) or its last context is under `claude_session.fresh_after_tokens`; otherwise a fresh session starts with a deterministic handoff and a new session ID | `plan_session`, `fresh_after_tokens`, and `handoff_text` in `claude-session`, which refuses any other threshold before the run starts; the `reason` in the run's JSON line; their tests |
| A worker run has no turn limit unless `claude_session.max_turns` sets a positive one | `max_turns_args` in `claude-session`, which refuses any other value before the run starts |
| The GitHub App has every permission that completion reads, such as Checks and Commit statuses, before the first card runs | `profiles/claude-worker/scripts/check-github-access`, run in setup's GitHub part |
| No setup check prints a secret; compare webhook routes only with `render.py --check` | `templates/render.py --check`, whose tests fail when a secret value reaches its output; docs only for other `hermes config get --raw` reads, in "Collect the inputs" and the machine-wide block, because the read runs inside Hermes' own CLI and a command-text guard in one agent runtime would not cover the others |
| Claude Code in a worker starts with an allowlisted environment, and its credentials reach it only through the per-run mode `0600` settings file | `child_env` and `claude_settings` in `profiles/claude-worker/scripts/claude-session`; the tests that prove `LINEAR_CLIENT_*`, `*_WEBHOOK_SECRET`, `FACTORY_INFISICAL_*`, `SLACK_*`, and `OP_*` never reach Claude |
| A worker's Claude Code reaches Linear only with the run's `LINEAR_API_KEY`, issued through the plugin's client with the plugin's scope set | `linear_api_key` in `claude-session`, which refuses to start Claude without one; the `LINEAR_RULE` in its system prompt; `skills/setup-factory/scripts/linear-mcp-headers`, which sends that token first |
| The worker home loads only `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET`, and the proxy token, and signs in to Infisical as an identity that can read nothing else | `profiles/claude-worker/scripts/worker-secrets` and its tests; `check_worker_secrets` in `check-install`, which reports any other worker `secrets.command`; docs only for the identity's scope, in `references/infisical.md` |
| A secret helper prints secrets only to a pipe and never takes arguments | `skills/setup-factory/scripts/factory-secrets` and `worker-secrets`, whose tests prove the terminal and argument refusals |
| A webhook delivery never starts an agent run while its route's secret is an unresolved `${NAME}` reference | The `pre_gateway_dispatch` hook in `plugins/linear-agent-session`, whose tests cover a resolved and an unresolved secret; `render.py --check` and the part 15 forged-signature lines report the state |
| Setup discovers each input it can read on the machine and never asks the operator to sign in | Docs only: "Collect the inputs" in `skills/setup-factory/SKILL.md`; the machine-wide block |
| Hermes' own agent knows that credentials live in the gateway and how to check GitHub, Linear, and Slack | `templates/memory.py --check` in setup part 17 |
| A factory's dashboard is reachable through its public hostname and keeps a signed-in user signed in past the first access token | `check_dashboard` in `profiles/claude-worker/scripts/check-install`: `dashboard.public_url` set, scopes include `offline_access`, and the issuer offers `refresh_tokens` |
| No webhook delivery is lost to the gateway's rate limit | `templates/render.py --check`, which reports a missing or different `platforms.webhook.extra.rate_limit`; the HTTP 429 line in `profiles/claude-worker/scripts/check-install` |
| A worker completes a card only when its pull request is ready to merge | `plugins/pr-ready-gate` |
| Unblocking a card whose pull request exists ends with `reopen-review`, the handoff that lifts Hermes' `active_pr` hold, so the card respawns after a Linear reply | Docs only: the unblock rule in `templates/triage-linear.md` and end-to-end check 18 in setup part 15 |
| Resuming a card parked in `triage` unassigns it before `specify` and assigns it again only after `edit` restores its title and body, because `specify` moves the card to `ready` and the dispatcher skips a `ready` card with no assignee while `kanban.default_assignee` is unset | `check_config` in `profiles/claude-worker/scripts/check-install`, whose `CONFIG` row reports a set `kanban.default_assignee`; docs only for the resume steps: the triage-resume rule in `templates/triage-linear.md` and end-to-end check 19 in setup part 15 |
| A worker refreshes a stale pull request head with `gh pr update-branch` before it judges the ready gate again, because a finished check never re-runs on its own | `CHECKS_RULE` in `profiles/claude-worker/scripts/claude-session`, appended to every run's system prompt; its tests in `profiles/claude-worker/tests/test_claude_session.py` |
| A takeover never publishes to another person's branch | The `-takeover` branch rule in `templates/triage-linear.md`; adoption tests in `profiles/claude-worker/tests/test_claude_session.py` |
| A worker never publishes to a branch the factory did not create | `adopt_existing_work` in `profiles/claude-worker/scripts/claude-session`, which refuses a card branch on GitHub whose first commit is not the App's, counted after the takeover pull request's head when the card has one, before Claude starts; the own-pull-request rule in `templates/triage-linear.md`, which keeps a pull request on a branch the App did not start out of the own row and sends it to the `-takeover` branch rule, while the App's own `-takeover` pull request keeps the person's pull request as `takeover_pr`; adoption tests in `profiles/claude-worker/tests/test_claude_session.py` |
| Every configured checkout has `origin/HEAD`, which `claude-session`, `push-signed`, and `refresh-checkouts` read | `check_checkouts` in `profiles/claude-worker/scripts/check-install`, run hourly and in setup part 5 and "Deploy a merged factory change" |
| When `gh issue develop` fails with `createLinkedBranch`, derive the branch name from the adapter's rule | Docs only: `docs/agents/issue-tracker.md` and the machine-wide block |
| A repository with the Engineering plugin has Bun for its issue-handoff gate | Setup part 10 check |
| A new script is committed without the executable bit and run with `python3` | `push-signed` refuses a commit that adds an executable file, because GitHub's commit API cannot set file modes |
| Commit type follows the changed paths | Docs only: "Commit type selection" below; commitlint checks only the format |
| Tests never assert on documentation prose | Docs only: "Testing Guidelines" below |
| The pstack model sheet's include appears once in `CLAUDE.md`, outside the factory block, and the block's marker pair stays intact | `skills/setup-factory/scripts/pstack-include`, run in setup part 17, with tests in `skills/setup-factory/tests/`; docs only for the sheet's values, which stay in the private record |
| Outside Access, a factory machine exposes only SSH, and SSH never accepts a password | `skills/setup-factory/scripts/check-host-exposure` in setup part 16, with tests in `skills/setup-factory/tests/` |
| No sharing service is reachable, and the macOS firewall is on | `check-host-exposure` |
| The SSH allowlist is one list of emails, never a domain; the sshd principals file mirrors it | `check-host-exposure --ssh-users`, which fails on drift and refuses a domain |
| A privileged step runs as one script in the operator's terminal and refuses to lock anyone out | Docs only: the runbook's rule for privileged steps and the machine-wide block |
| Agents on a factory machine know who owns ingress, how people reach the machine, and how to handle `sudo` | The machine-wide block, which setup part 17 compares with the template |
| The `github` route filter and the triage prompt's events-that-matter list come from one table | `GITHUB_EVENT_TABLE` in `templates/render.py`; `test_prompt_list_and_filter_cover_the_same_event_actions` in `templates/tests/test_render.py` compares the rendered prompt with the filter |
| Factory pull requests are always opened by the App, so the `github` route keeps only the App's own pull requests | The takeover rule in `templates/triage-linear.md` (`takeover_pr`: the worker always opens its own pull request) and the adoption tests in `profiles/claude-worker/tests/test_claude_session.py`; docs only for the route's dependency on it |
| A cron job's output reaches a human: its `--deliver` platform has a home channel in the profile that owns the job | `unresolved_deliveries` and the root `check-install-alert` wrapper check in `profiles/claude-worker/scripts/check-install`; the `check-install` job lives in the root profile (setup part 12) |
| Every change to a factory environment lands in this repository in the same change, as a runbook step, template, script, or check, so a fresh install reproduces it. Real values go only to the private record | The machine-wide block; `templates/memory.py --check` in setup part 17; the `CONFIG`, `CRON`, and `SERVICES` tables in `profiles/claude-worker/scripts/check-install`, which report config keys, cron jobs, and launchd services that differ from the runbook |

## Build, Test, and Development Commands

- `pnpm install` (or `pnpm env:setup`): install dev tooling and register Husky.
- `pnpm skills:install`: re-vendor locked project-local skills from
  `skills-lock.json`, then commit the refreshed `.agents/skills/**` and
  `.claude/skills/**` overlays.
- `pnpm clean`: remove generated dependencies and temporary install files
  (`node_modules`, `.skills-install.lock*`); never prunes committed skill overlays.
- `pnpm lint:md`: lint all Markdown.
- `pnpm exec commitlint --edit <file>`: check a commit message.

## Coding Style & Naming Conventions

- Use lowercase, hyphenated directory names for plugins and profiles
- Keep a plugin's directory name aligned with its plugin ID
- Use Markdown for docs, YAML for Hermes manifests and templates, JSON for package metadata
- Issue titles use plain language, not conventional commit formatting. Example:
  `Add the pr-ready-gate plugin`

## Testing Guidelines

- **Tests must not assert on the prose content of documentation files.** Tests
  validate code behavior and machine-consumed contracts only: plugin and script
  behavior, valid JSON/YAML config, manifest schemas, symlink resolution, and
  required-file existence. A documentation file's prose body must be freely
  editable without breaking a test. Markdown *linting* (`pnpm lint:md`) is
  unaffected — linting is not testing.
- Add a repository `test` script once a component has automated checks.

## Pull request labels

Use `gh label list` to see the repository's pull-request label set. Each label's
`description` documents when to apply it. Issue labels are live tracker data and
must be resolved through `docs/issue-tracker.md`.

Verify every label has a non-empty description:

```bash
gh label list --json name,description --jq '.[] | select(.description == "")'
```

## Writing pull requests

This repository defines no pull request body structure; the author chooses it.
`.github/pull_request_template.md` only reminds authors about closing
references: one closing line for each completed issue, with both the GitHub
issue and its corresponding Linear issue, each with its own closing keyword.

For issues, use the tracker-agnostic issue skills. They consult
`docs/issue-tracker.md`.

## GitHub Actions pinning

Pin every action reference to a full 40-character commit SHA, not a tag. Tags are mutable;
SHAs are not. Above each `uses:` line, leave a comment naming the action and version the SHA
corresponds to, so updates remain reviewable.

```yaml
# actions/checkout@v4.3.1
- uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
```

`actionlint` runs in CI on `.github/workflows/**` changes and enforces workflow hygiene as
part of its other checks.

## Commit type selection

Product files are `plugins/**`, `profiles/**`, and `templates/**`. Choose the
commit type from the paths the change touches, not from how the change feels.

| Changed paths | Type |
| --- | --- |
| Product files, new behavior | `feat` |
| Product files, corrected behavior | `fix` |
| Product files, same behavior, restructured | `refactor` |
| Only `docs/**`, `README.md`, `AGENTS.md`, `CONTRIBUTING.md`, or other prose | `docs` |
| Only tests | `test` |
| Only `.github/workflows/**` | `ci` |
| Tooling, dependencies, or repository configuration | `chore` |

| Excuse | Correction |
| --- | --- |
| "It's small, so it's a chore." | Size does not set the type. A product-file behavior change is `feat` or `fix`. |
| "I also updated the README." | The product-file change decides the type; docs ride along. |
| "It only changes a template." | Templates are product files. Use `feat` or `fix`. |

Stop and re-check the type when a `chore` or `docs` commit touches
`plugins/**`, `profiles/**`, or `templates/**`.

- WRONG: `chore: #12 block draft PRs in the gate` (changes `plugins/pr-ready-gate/`)
- RIGHT: `feat: #12 block draft PRs in the gate`

## Commit & Pull Request Guidelines

Commits must use conventional commit types, no scopes, and a current GitHub
issue reference:

`type: #123 short description`

Examples:

- `chore: #1 bootstrap repository`
- `feat: #12 add the pr-ready-gate plugin`

For squash-and-merge workflows, PR titles must match the commitlint commit format:

`type: #123 short description`

Bot-generated release-please PRs from `release-please--*` branches and bot-generated release
bump PRs from `bot/bump-*` branches are the only no-issue PR exceptions.

<!-- BEGIN engineering:patina-mode (managed by setup-engineering; re-running overwrites this block) -->
<EXTREMELY_IMPORTANT>
You have the Patina Project Engineering plugin, forked from pstack.

Before responding to any non-trivial engineering task — a feature, bug fix, refactor, debugging, performance work, or any multi-step code change — invoke the `engineering:patina-mode` skill with the Skill tool and follow it. It is the default entry point and routes to the specific Engineering skills from there. Pure questions and trivial one-line edits don't need it.

When the intent is already specific, enter directly: `engineering:tdd` (bug with a reproducible failure), `engineering:architect` (types and module shape before code that crosses a function boundary), `engineering:how` (how a subsystem works), `engineering:why` (why it was built this way), `engineering:arena` (N parallel attempts at one task), `engineering:interrogate` (multi-model diff review).

If you were dispatched as a subagent to execute a specific task, ignore this block — patina-mode governs the orchestrating session, and it already shaped your dispatch.

User instructions (CLAUDE.md, AGENTS.md, direct requests) take precedence over this mandate. Other session-start mandates (such as superpowers) compose with it: their skill-check discipline stands, and patina-mode is the implementation entry point they route to for non-trivial code work.
</EXTREMELY_IMPORTANT>
<!-- END engineering:patina-mode -->
