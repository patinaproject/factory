# factory

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) software factory:
Linear issues delegated to an agent become Hermes Kanban cards, and Claude Code
works each card until its pull request is ready to merge.

> **Status:** pre-release. The components below are implemented and have unit
> tests.

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

## Components

| Path | Kind | Purpose |
| --- | --- | --- |
| `plugins/linear-agent-session/` | Hermes plugin | Acknowledges Linear agent sessions within 10 seconds, and provides the `linear_agent_activity` and `linear_issue` tools as the Linear agent app |
| `plugins/pr-ready-gate/` | Hermes plugin | Blocks `kanban_complete` until the pull request is out of draft, `CLEAN`, and at the worker's commit |
| `profiles/claude-worker/` | Hermes profile | The Kanban worker that drives Claude Code, with the `claude-session` launcher, the `refresh-checkouts` cron script, and the `push-signed` publisher for GitHub App signed commits |
| `templates/` | Templates | `render.py` builds the `linear` and `github` webhook routes and their triage prompts from settings |
| `skills/setup-factory/` | Agent skill | Sets up and checks a factory on a machine |

Every factory-specific value, such as repositories, apps, and model transport,
lives in that factory's own Hermes settings and `.env`, never in this
repository.

## Set up a factory

Ask a coding agent to run the `setup-factory` skill from a checkout of this
repository. The skill is in [`skills/setup-factory/`](./skills/setup-factory/SKILL.md).
The agent asks for the factory's values, runs every scriptable step, and stops
where you must act in a browser or type a secret into your own terminal. A
second run checks each part and changes nothing that is already correct.

## Run the tests

```sh
pnpm test
```

`pnpm test` runs `scripts/test.sh`, which runs the `unittest` suites under
`plugins/`, `profiles/`, and `templates/` with `python3`. Set `PYTHON` to use
another interpreter.

## Conventions

- Commits and pull request titles use `type: #123 short description`.
- Markdown is linted with `markdownlint-cli2` (`pnpm lint:md`).
- See [`AGENTS.md`](./AGENTS.md) and [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

[MIT](./LICENSE)
