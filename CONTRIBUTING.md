# Contributing

Thanks for contributing to `patinaproject/factory`. Start here for repo rules; everything else lives in [`AGENTS.md`](./AGENTS.md), and the rules that code enforces, with their enforcers, live in [`docs/rules.md`](./docs/rules.md).

## Setup

```bash
pnpm install
```

This installs dev tooling and registers the Husky hooks (`commit-msg` + `pre-commit`).

## Commit messages

Commits must follow Conventional Commits with no scope and a required GitHub
issue reference:

```text
type: #123 short description
```

Examples:

- `feat: #42 add a feature`
- `docs: #17 clarify install steps`

The `commit-msg` hook enforces this. PR titles follow the same format so the squash commit can be reused verbatim.

## Markdown

All Markdown is linted with `markdownlint-cli2`. The `pre-commit` hook runs it on staged files. To lint the whole repo:

```bash
pnpm lint:md
```

## Vendored skills and agents

This repository vendors skills from `patinaproject/skills` into `.agents/skills/`,
pins them in `skills-lock.json`, and keeps the agents that `setup-code-skills`
bundles in `.claude/agents/`.

The `Skills Sync` workflow keeps them equal to the latest stable
`patinaproject/skills` release. It runs daily, on `repository_dispatch` of type
`skills_stable_release`, and by hand with an optional release tag. When the
vendored tree differs from the release, it runs `pnpm test` and `pnpm lint:md`,
then opens or refreshes one pull request from `bot/bump-skills`. A person reviews
and merges it. The pull request names the release and lists the skills and agents
added, changed, and removed.

`.agents/skills-sync.json` holds the sync's choices. `source` is the repository,
`plugins` vendors every skill of those plugins, and `skills` vendors named skills
from any plugin. People edit those keys. The sync writes `release` (tag, commit,
and URL) and refuses a release older than that one.

To try a sync without publishing, run
`python3 scripts/skills-sync --target <checkout> --dry-run`. It reconciles the
checkout and leaves the changes uncommitted.

A person must set the workflow up once. Install a GitHub App on this repository
with `Contents` and `Pull requests` write access, then add its ID and private key
as the repository secrets `SKILLS_SYNC_APP_ID` and `SKILLS_SYNC_APP_PRIVATE_KEY`.
The workflow mints a short-lived token from them, so the sync commit is signed by
the App.

## Pull requests

- Keep the PR title in commitlint format.
- The body structure is up to you. Include one `Closes #N`, `Fixes #N`, or
  `Resolves #N` line for each completed issue, with its corresponding Linear
  reference on the same line and its own closing keyword.
- Never include a factory's real values (repositories, app IDs, hostnames,
  tokens) in code, templates, examples, or PR text.
- Bot-generated release bump PRs from `bot/bump-*` branches are the only no-issue exception.

## Further reading

- [`AGENTS.md`](./AGENTS.md) — shared workflow contract.
- [`CLAUDE.md`](./CLAUDE.md) — Claude Code–specific guidance.
- [`docs/file-structure.md`](./docs/file-structure.md) — repository layout reference.
