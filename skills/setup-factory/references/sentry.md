# Sentry access for workers

A repository can list a `sentry` MCP server, but Sentry's hosted server signs
in through a browser, which a headless worker run cannot do. When
`claude_session.sentry_token_env` names a variable, `claude-session` gives each
run its own `sentry` server instead:

- The server is `https://mcp.sentry.dev/mcp?skills=inspect`. The `inspect`
  skill exposes only Sentry's read-only tools, so `update_issue` and the
  project-management tools are not available in the run.
- Its `Authorization` header is `Sentry-Bearer ${<variable>}`. Claude Code
  expands the name from the per-run `0600` settings file, so the token is never
  in argv or in Claude Code's process environment.
- When the variable is unset or empty, the run is refused before Claude Code
  starts. When the key is empty or absent, the run has only the Linear server.

The access is optional. Skip this part when no repository needs Sentry.

## Check

- `claude_session.sentry_token_env` holds the variable's name in both homes.
- `env_has <worker>/.env <variable>` prints `1`, or, with Infisical, the
  variable is in the factory's folder and the worker's identity may read it.
- `hermes -p claude-worker config get terminal.env_passthrough --json` lists
  the variable.

## Create the token

> **HUMAN CHECKPOINT.** Ask the operator to create a Sentry user auth token in
> **User Settings → Personal Tokens** with read scopes only: `org:read`,
> `project:read`, `team:read`, and `event:read`. The `inspect` skill already
> hides write tools; the read-only scopes keep the token harmless if a later
> server version exposes one. Ask the operator to store it in their own
> terminal, never in chat: with `infisical_set` in the factory's folder when
> it uses [Infisical](infisical.md), otherwise with `env_set` in
> `<worker>/.env`. Use a name such as `SENTRY_ACCESS_TOKEN`.

With Infisical, add the variable to the worker identity's `secretName`
condition in [Create the machine identities](infisical.md#create-the-machine-identities).
`worker-secrets` prints the variable that `sentry_token_env` names and still
drops every other secret.

## Configure the run

1. Set `claude_session.sentry_token_env` to the variable's name in the settings
   JSON of both homes.
2. Add the variable to the worker's `terminal.env_passthrough` list.
3. When a repository's `.mcp.json` lists a server a worker must not load, such
   as a production database reader, put its name in
   `claude_session.disabled_mcp_servers`. `claude-session` writes the list to
   the settings file's `disabledMcpjsonServers`. A value that is not a list of
   names refuses the run.

Start a card on a repository that lists `sentry`, and ask the run to call
`find_organizations`. It answers with the operator's organizations.
