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
edit or close public-repository issues in Linear. There are two exceptions.
Delegating the Linear copy to the factory's agent starts a card, and triage sets
the Linear copy's priority from the adapter's Priority rubric (see
`docs/agents/issue-tracker.md`). Do not add committed design
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

## Rules agents must know

When the operator corrects an agent, fix the mistake, add its enforcer, and
record the rule and its enforcer in [`docs/rules.md`](docs/rules.md). Edit
`AGENTS.md` only for a rule that no code enforces. `docs/rules.md` is this
repository's rule table for the `correct` skill.

Read these before you act. No code enforces the first five, and `push-signed`
refuses a break of the last one only when you publish:

- Setup discovers each input it can read on the machine and never asks the operator to sign in
- When `gh issue develop` fails with `createLinkedBranch`, derive the branch name from the adapter's rule
- Commit type follows the changed paths
- Tests never assert on documentation prose
- A privileged step runs as one script in the operator's terminal and refuses to lock anyone out
- A new script is committed without the executable bit and run with `python3`

## Build, Test, and Development Commands

- `pnpm install` (or `pnpm env:setup`): install dev tooling and register Husky.
- `pnpm skills:install`: re-vendor locked project-local skills from
  `skills-lock.json`, then commit the refreshed `.agents/skills/**` and
  `.claude/skills/**` overlays.
- `pnpm clean`: remove generated dependencies and temporary install files
  (`node_modules`, `.skills-install.lock*`); never prunes committed skill overlays.
- `pnpm test`: run every component's Python tests from the repository root.
  `scripts/test.sh` runs `python3 -m unittest discover` in each `tests`
  directory under `plugins/`, `profiles/`, `skills/`, and `templates/`, with
  the component as the top-level directory.
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
