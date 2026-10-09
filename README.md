# factory

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) software factory:
Linear issues delegated to an agent become Hermes Kanban cards, and Claude Code
works each card until its pull request is ready to merge.

> **Status:** scaffolding. The components below are planned and not yet
> implemented.

## How it works

1. An issue is delegated to the factory's Linear agent app.
2. Hermes acknowledges the Linear agent session, triages the issue, and queues a
   Kanban card for the right repository.
3. A Hermes worker profile drives the Claude Code CLI in a Kanban-owned git
   worktree, resuming the same Claude session for each issue.
4. The card completes only when its pull request is out of draft, mergeable,
   and passing every check. Progress is reported back to the Linear session.

The factory uses Hermes' supported extension points only (plugins, profiles,
skills, webhook routes, and cron). It never modifies Hermes source.

## Planned components

| Path | Kind | Purpose |
| --- | --- | --- |
| `plugins/linear-agent-session/` | Hermes plugin | Fast Linear agent-session acknowledgement and an activity tool |
| `plugins/pr-ready-gate/` | Hermes plugin | Blocks `kanban_complete` until the pull request is ready to merge |
| `profiles/claude-worker/` | Hermes profile | Kanban worker that drives Claude Code, plus the `claude-session` launcher |
| `templates/` | Templates | Webhook routes, triage prompts, and cron jobs rendered from settings |

Every factory-specific value, such as repositories, apps, and model transport,
lives in that factory's own Hermes settings and `.env`, never in this
repository.

## Conventions

- Commits and pull request titles use `type: #123 short description`.
- Markdown is linted with `markdownlint-cli2` (`pnpm lint:md`).
- See [`AGENTS.md`](./AGENTS.md) and [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

[MIT](./LICENSE)
