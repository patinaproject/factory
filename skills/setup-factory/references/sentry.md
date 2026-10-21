# Sentry access for workers

A repository can list a `sentry` MCP server, but Sentry's hosted server signs
in through a browser, which a headless worker run cannot do. When
`claude_session.sentry_token_env` names a variable, `claude-session` gives each
run its own `sentry` server instead:

- The server is `https://mcp.sentry.dev/mcp?skills=inspect`. The `inspect`
  skill exposes only Sentry's read-only tools, so `update_issue` and the
  project-management tools are not available in the run.
- Its `headersHelper` is `<worker>/scripts/sentry-headers`, which asks the
  gateway's broker for the token and prints a `Sentry-Bearer` `Authorization`
  header. Claude Code keeps the token in memory, so it is in no environment,
  settings file, or argv, and the worker home never stores it.
- When `claude_session.sentry_token_env` is set and the gateway holds no
  token, the run is refused before Claude Code starts. When it is empty
  or absent, the run has only the Linear server.

The access is optional. Skip this part when no repository needs Sentry.

## Check

- `claude_session.sentry_token_env` holds the variable's name in both homes.
- `check-install` prints no `sentry_token_present false` line for the upload
  broker, so the gateway holds the token.
- `env_has <worker>/.env <variable>` prints `0`. The worker never stores it.

## Create the token

> **HUMAN CHECKPOINT.** Ask the operator to create a Sentry user auth token in
> **User Settings → Personal Tokens** with read scopes only: `org:read`,
> `project:read`, `team:read`, and `event:read`. The `inspect` skill already
> hides write tools; the read-only scopes keep the token harmless if a later
> server version exposes one. Ask the operator to store it in their own
> terminal, never in chat: with `infisical_set` in the factory's folder when
> it uses [Infisical](infisical.md), otherwise with `env_set` in
> `<home>/.env`. Name it `SENTRY_ACCESS_TOKEN`, the name that the
> `factory-secret-names` plugin declares, so Hermes strips it from every
> process a home starts. `check-install` reports any other name.

## Configure the run

1. Set `claude_session.sentry_token_env` to the variable's name in the settings
   JSON of both homes.
2. When a repository's `.mcp.json` lists a server a worker must not load, such
   as a production database reader, put its name in
   `claude_session.disabled_mcp_servers`. `claude-session` writes the list to
   the settings file's `disabledMcpjsonServers`. A value that is not a list of
   names refuses the run.
3. Restart the gateway with
   `python3 <repo>/skills/setup-factory/scripts/restart-gateway` from a shell
   outside Hermes, so its broker reads the name and loads the token.

Start a card on a repository that lists `sentry`, and ask the run to call
`find_organizations`. It answers with the operator's organizations.
