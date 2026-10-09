# File structure

```text
/
├── .claude/settings.json      # Claude Code project settings and SessionStart hook
├── .codex/                    # Codex configuration and environment setup
├── .github/                   # Issue and PR templates, CODEOWNERS, workflows
├── .husky/                    # commit-msg and pre-commit hooks
├── docs/
│   ├── agents/                # Agent-facing tracker adapter and domain settings
│   ├── issue-publishing.md    # Issue-writing rules
│   ├── issue-tracker.md       # Symlink to docs/agents/issue-tracker.md
│   ├── release-flow.md        # How releases are cut
│   └── wiki-index.md          # Planned wiki pages
├── plugins/                   # Hermes plugins (planned)
├── profiles/                  # Hermes worker profiles (planned)
├── scripts/                   # Repository maintenance scripts
├── templates/                 # Setup templates rendered from settings (planned)
├── AGENTS.md                  # Shared agent and contributor workflow contract
├── CLAUDE.md                  # Imports AGENTS.md
├── CONTRIBUTING.md
├── README.md
├── SECURITY.md
├── commitizen.config.json
├── commitlint.config.js
├── package.json
└── skills-lock.json           # Locked project-local skills (none yet)
```
