# Contributing

Thanks for contributing to `patinaproject/factory`. Start here for repo rules; everything else lives in [`AGENTS.md`](./AGENTS.md).

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
