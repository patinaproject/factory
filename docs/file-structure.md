# File structure

```text
/
├── .agents/skills/            # Vendored skills, plus a setup-factory symlink
├── .claude/
│   ├── settings.json          # Claude Code project settings and SessionStart hook
│   └── skills/                # Skill symlinks for Claude Code, including setup-factory
├── .codex/                    # Codex configuration and environment setup
├── .github/
│   └── workflows/
│       └── test.yml           # Runs scripts/test.sh on pull requests
├── .husky/                    # commit-msg and pre-commit hooks
├── docs/
│   ├── agents/                # Agent-facing tracker adapter and domain settings
│   ├── issue-publishing.md    # Issue-writing rules
│   ├── issue-tracker.md       # Symlink to docs/agents/issue-tracker.md
│   ├── release-flow.md        # How releases are cut
│   └── wiki-index.md          # Planned wiki pages
├── plugins/
│   ├── linear-agent-session/  # Linear acknowledgement hook, activity, issue, and session tools, cli.py
│   └── pr-ready-gate/         # pre_tool_call hook that gates kanban_complete
├── profiles/
│   └── claude-worker/         # Kanban worker profile distribution
│       ├── SOUL.md            # Worker instructions
│       ├── config.yaml        # Toolsets, skills, plugins, and placeholder settings
│       ├── distribution.yaml  # Profile distribution manifest
│       └── scripts/           # claude-session, push-signed, and the refresh cron scripts
├── scripts/
│   ├── test.sh                # Runs every component's unittest suite
│   └── ...                    # Repository maintenance scripts
├── skills/
│   └── setup-factory/         # Agent runbook that sets up a factory on a machine
│       ├── SKILL.md
│       └── references/        # Cloudflare and SST, Linear, GitHub, Slack, Claude transport, Infisical
├── templates/
│   ├── render.py              # Builds the linear and github webhook routes from settings
│   ├── triage-linear.md       # Linear triage prompt template
│   └── triage-github.md       # GitHub triage prompt template
├── AGENTS.md                  # Shared agent and contributor workflow contract
├── CLAUDE.md                  # Imports AGENTS.md
├── CONTRIBUTING.md
├── README.md
├── SECURITY.md
├── commitizen.config.json
├── commitlint.config.js
├── package.json
└── skills-lock.json           # Locked vendored skills
```

Each component keeps its unit tests in a `tests/` directory next to its code.
