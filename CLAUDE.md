@AGENTS.md

## Claude Code

- Keep the shared workflow contract in `AGENTS.md`; put Claude-only guidance below this import instead of duplicating the repo rules.
- Project-level Claude Code configuration lives in `.claude/settings.json`. Plugins enabled for this repo are declared under `enabledPlugins`.
- Never write tests that assert on the prose content of documentation files; test code behavior and machine-consumed contracts only (see `AGENTS.md` → Testing Guidelines).
