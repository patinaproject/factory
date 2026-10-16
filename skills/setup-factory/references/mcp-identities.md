# MCP server identities

A worker's Claude Code loads every server in its repository's `.mcp.json`.
In a worker run, no person is signed in, so a server that needs a credential
reports `needs-auth` or `failed`. Signing a server in as a person would make
every worker act as that person. Instead, give the factory an identity of its
own on each service, as it already has for GitHub and Linear. When a service
offers no identity the factory can hold, leave the server out and record why.

## How a credential reaches a worker

Each repository entry in the factory settings takes two optional keys:

- `mcp_servers` maps a server name to a Claude Code MCP server definition.
  `claude-session` passes it with `--mcp-config`, so it replaces the
  repository's server of the same name in each run. A `${NAME}` reference in it
  must name a `FACTORY_MCP_` variable. `claude-session` copies each value into
  the run's mode `0600` settings file, never argv, and refuses the run when a
  variable is unset or a reference names another variable. The factory owns
  `linear`, so the key cannot name it.
- `mcp_left_out` maps a server name to the reason the factory does not connect
  it. `claude-session` disables those servers in each run.

```json
{
  "full_name": "<owner>/<name>",
  "path": "/abs/path/to/checkout",
  "routing": "default",
  "mcp_servers": {
    "posthog": {
      "type": "http",
      "url": "https://mcp.posthog.com/mcp",
      "headers": {"Authorization": "Bearer ${FACTORY_MCP_POSTHOG_KEY}", "x-posthog-read-only": "true"}
    }
  },
  "mcp_left_out": {
    "figma": "Figma's MCP server accepts only a person's OAuth sign-in"
  }
}
```

Keep each credential in the factory's secret source:

1. Ask the operator to store the value in Infisical, environment
   `development`, folder `/factory`, under its `FACTORY_MCP_` name. Without
   Infisical, the operator adds the line to `<worker>/.env` in their own
   terminal. Never ask for the value in chat.
2. Give the worker's Infisical identity read access to the new secret. See
   [`infisical.md`](infisical.md). `worker-secrets` passes every
   `FACTORY_MCP_` key and drops the rest.
3. Add the name to `terminal.env_passthrough` in the worker home, keeping the
   names already there:

   ```sh
   hermes -p claude-worker config get terminal.env_passthrough --json
   hermes -p claude-worker config set terminal.env_passthrough '[<existing names>, "FACTORY_MCP_<NAME>"]' </dev/null
   ```

4. Add the server to the repository's `mcp_servers` in the part 10 settings,
   and set the settings in both homes.

Record the account name, its owner, and each secret name in the factory's
private configuration record. Never commit them.

## Each service

Create each account in the factory's own name, such as a mailbox the
operator's organization owns for the factory. Give it the least access the
factory's work needs. Prefer an API token or service account to an OAuth
session.

| Server | Factory identity | Header |
| --- | --- | --- |
| `posthog` | A PostHog user for the factory, with a personal API key from the "MCP Server" preset, pinned to one project | `Authorization: Bearer ${FACTORY_MCP_POSTHOG_KEY}`, plus `x-posthog-read-only: true` unless the work writes |
| `revenuecat` | A dedicated API v2 secret key on the project. Choose read-only unless the work changes resources | `Authorization: Bearer ${FACTORY_MCP_REVENUECAT_KEY}` |
| `sentry` | An internal integration on the organization, with read scopes such as `org:read`, `project:read`, and `event:read` | `Authorization: Sentry-Bearer ${FACTORY_MCP_SENTRY_TOKEN}`. Plain `Bearer` is for Sentry's OAuth tokens |
| `dataforseo` | A DataForSEO account for the factory. Use its API login and password, not the dashboard sign-in | `Authorization: Basic ${FACTORY_MCP_DATAFORSEO_BASIC}`, where the value is `base64(<API login>:<API password>)` |
| `cloudflare-bindings` | An account API token scoped to the bindings the work reads | Point the server at `https://mcp.cloudflare.com/mcp`, the successor that accepts a token, with `Authorization: Bearer ${FACTORY_MCP_CLOUDFLARE_TOKEN}`. The bindings server accepts only OAuth |
| `featurebase` | An API key from Settings, then API, on a factory account | Try `Authorization: Bearer ${FACTORY_MCP_FEATUREBASE_KEY}`. Featurebase does not document a token for its MCP server. If the check reports it not connected, leave it out |

A repository's own server that reads its key through a person's secret
session, such as a `headersHelper` that signs in to Infisical as a person,
gets a factory credential the same way. Store the key under a `FACTORY_MCP_`
name and define the server in `mcp_servers` with a static header. Give the
factory the development credential only.

Leave these servers out, with the reason in `mcp_left_out`:

| Server | Reason |
| --- | --- |
| `figma` | Figma's MCP server accepts only a person's OAuth sign-in. Personal access tokens are not supported |
| `vercel` | Vercel's MCP server accepts only OAuth, which acts with the signed-in person's access |
| `claude-design` | Claude Design needs a claude.ai sign-in with Claude Design access. Anthropic documents no service credential |
| A production data or content server | The factory holds no production access |

Revisit a left-out server when its service adds a token or service-account
form.

## Check

```sh
HERMES_HOME=<worker> python3 <worker>/scripts/check-install
```

For each configured repository, `check-install` runs
`claude-session --mcp-status`. It starts Claude Code in the repository's
checkout with the run's environment, settings, and MCP config, reads the
status of each server, and stops before Claude Code reaches the model. It
prints an `mcp <server> in <owner>/<name>` line for each server that is not
`connected` and not in `mcp_left_out`, and a line for each `FACTORY_MCP_` name
missing from `terminal.env_passthrough`. It prints no value. The environment
that runs `check-install` must hold each `FACTORY_MCP_` value, as a worker
does. Otherwise the probe reports the unset name instead of the statuses.

To read every status for one repository:

```sh
HERMES_HOME=<worker> python3 <worker>/scripts/claude-session --mcp-status --repo <owner>/<name>
```
