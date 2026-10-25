# MCP server identities

A worker's Claude Code loads every server in its repository's `.mcp.json`. No
person is signed in to a worker run, so a server that needs a credential
reports `needs-auth` or `failed`. Signing a server in as a person would make
every worker act as that person. Instead, give the factory an identity of its
own on each service, as it already has for GitHub and Linear. When a service
offers no identity the factory can hold, disable the server and record why.

## How a token reaches a worker

`claude_session.mcp_servers` in the factory settings maps a server name to the
factory's server on that service:

| Key | Meaning |
| --- | --- |
| `url` | The service's HTTP MCP endpoint, an `https` URL |
| `token_env` | The variable that holds the factory's token. Only the gateway holds its value |
| `scheme` | Optional. The `Authorization` scheme word, `Bearer` by default |
| `headers` | Optional. Fixed headers that hold no secret, such as a read-only flag. They reach argv, so `claude-session` refuses a header name, or a name in the `url`'s query string, with `auth`, `key`, `token`, `secret`, `cookie`, `password`, `cred`, `session`, `sig`, `pass`, `pwd`, `jwt`, `bearer`, or `access` in it, and a header or query value that starts with `Bearer`, `Basic`, `Token`, or `Sentry-Bearer` |

For each server, `claude-session` passes a server of the same name with
`--mcp-config`, so it replaces the repository's server of that name in each
run. A repository that does not list the server still gets it. Its
`headersHelper` is `<worker>/scripts/mcp-headers <name> <scheme>`, which asks
the gateway's broker for the server's token and prints the `Authorization`
header. The request names the server. The broker reads the variable from the
gateway's own settings, never from the request, and never serves the upload
token, the Linear app credentials, the Infisical identity, or a webhook
secret. Claude Code keeps the token in memory, so it is in no environment,
settings file, or argv, and the worker home never stores it.

`claude-session` refuses a run before Claude Code starts when the gateway's
broker holds no token for a server. It sends the card back to the queue as
`transient` when the broker is busy or unreachable. It refuses the factory's
own `linear` server, a key other than the four above, and the old
`claude_session.sentry_token_env`.

Only HTTP servers get a factory identity this way. A stdio server that needs a
secret would hold it in its own environment, which other processes of the
worker user can read. A service that takes its token only in a header other
than `Authorization` cannot get one either. Disable such a server.

`claude_session.disabled_mcp_servers` lists the repository servers a worker
does not load. `claude-session` writes it to the settings file's
`disabledMcpjsonServers`. A value that is not a list of names refuses the run.

```json
"claude_session": {
  "mcp_servers": {
    "sentry": {"url": "https://mcp.sentry.dev/mcp?skills=inspect", "token_env": "SENTRY_ACCESS_TOKEN", "scheme": "Sentry-Bearer"},
    "posthog": {"url": "https://mcp.posthog.com/mcp", "token_env": "<NAME>_TOKEN", "headers": {"x-posthog-read-only": "true"}}
  },
  "disabled_mcp_servers": ["figma", "vercel"]
}
```

The access is optional. With an empty `mcp_servers`, a run has only the
factory's Linear server and the repository's own servers.

## Add a service

> **HUMAN CHECKPOINT.** Ask the operator to create the factory's account or
> service token on the service, with the least access the factory's work needs.
> Prefer an API token or a service account to an OAuth session. Ask the
> operator to store the token in their own terminal, never in chat: with
> `infisical_set` in the factory's folder when it uses
> [Infisical](infisical.md), otherwise with `env_set` in `<home>/.env`.
> Record the account, its owner, its access scope, and the variable's name in
> the factory's private configuration record.

1. Name the variable with a suffix that Hermes treats as a secret: `_TOKEN`,
   `_KEY`, `_SECRET`, or `_PASSWORD`. Declare the name in
   `plugins/factory-secret-names/plugin.yaml` in a factory pull request, so
   Hermes strips it from every process a home starts. `SENTRY_ACCESS_TOKEN`
   is already declared. The broker serves only a declared name, so a
   `token_env` the manifest does not declare gets no token and every run that
   uses it refuses. `check-install` reports such a name.
2. After the pull request merges and deploys, add the server to
   `claude_session.mcp_servers` in the settings JSON of both homes.
3. Restart the gateway with
   `python3 <repo>/skills/setup-factory/scripts/restart-gateway` from a shell
   outside Hermes, so its broker reads the new settings.
4. Run the check below.

## Each service

| Server | Factory identity | `scheme` and `headers` |
| --- | --- | --- |
| `sentry` | A user auth token with read scopes only: `org:read`, `project:read`, `team:read`, and `event:read`. Use the URL with `?skills=inspect`, which exposes only Sentry's read-only tools | `Sentry-Bearer`. Plain `Bearer` is for Sentry's OAuth tokens |
| `posthog` | A PostHog user for the factory, with a personal API key from the "MCP Server" preset, pinned to one project | `Bearer`, with the header `x-posthog-read-only: true` unless the work writes |
| `revenuecat` | A dedicated API v2 secret key on the project. Choose read-only unless the work changes resources | `Bearer` |
| `dataforseo` | A DataForSEO account for the factory. Use its API login and password, not the dashboard sign-in. Store `base64(<API login>:<API password>)` as the value | `Basic` |
| `cloudflare-bindings` | An account API token scoped to the bindings the work reads. Point the server at `https://mcp.cloudflare.com/mcp`, which accepts a token. The bindings server accepts only OAuth | `Bearer` |
| `featurebase` | An API key from Settings, then API, on a factory account. Featurebase does not document a token for its MCP server. If the check reports it not connected, disable it | `Bearer` |

A repository server that reads its key through a person's secret session,
such as a `headersHelper` that signs in to Infisical as a person, gets a
factory token the same way when it is an HTTP server. Give the factory the
development credential only.

Disable these servers with `disabled_mcp_servers`, and record why in the
private record:

| Server | Reason |
| --- | --- |
| `figma` | Figma's MCP server accepts only a person's OAuth sign-in |
| `vercel` | Vercel's MCP server accepts only OAuth, which acts with the signed-in person's access |
| `claude-design` | Claude Design needs a claude.ai sign-in with Claude Design access. Anthropic documents no service credential |
| A stdio server that needs a secret | Its environment would hold the secret |
| A production data or content server | The factory holds no production access |

Revisit a disabled server when its service adds a token or a service account.

## Root agent

The root agent answers in chat and Linear. It does not run `claude-session`,
so it gets none of the servers above. Give it a static-token HTTP server
through native Hermes `mcp_servers` in the root home's `config.yaml`:

```yaml
mcp_servers:
  sentry:
    url: https://mcp.sentry.dev/mcp/<org>?skills=inspect
    headers:
      Authorization: Sentry-Bearer ${SENTRY_ACCESS_TOKEN}
    sampling:
      enabled: false
```

- Use the `token_env` of the matching `claude_session.mcp_servers` entry, and
  the same scheme word. The header holds only a `${VAR}` reference. Hermes
  fills it from the home's secret source, the `factory-secrets` helper, and
  fails closed when the variable is unset. `config.yaml` never holds a token.
- Set `sampling.enabled` to `false`, so the server cannot ask the factory's
  model for inference.
- The `<org>` in the Sentry URL limits the server to one organization. That URL
  hides the `whoami` and `find_organizations` tools. This is expected.
- OAuth servers, and stdio servers that need a secret, are out of scope.

Set and check it from a shell outside Hermes:

```sh
HERMES_HOME=<home> hermes config set mcp_servers.<server> '<json>' </dev/null
HERMES_HOME=<home> hermes mcp test <server>
python3 <repo>/skills/setup-factory/scripts/restart-gateway
```

`<json>` is the server entry as JSON, with the same keys as the YAML above.

## Check

- `check-install` prints no `holds no token for the <name> MCP server` line
  for the upload broker, so the gateway holds each token.
- `env_has <worker>/.env <variable>` prints `0` for each variable. The worker
  never stores one.
- `check-install` prints no `mcp` line. For each configured repository, it
  runs the installed `claude-session --mcp-status`, which starts Claude Code
  in the repository's main checkout with a run's settings and MCP config,
  reads each server's status from its `init` event, and stops before Claude
  Code reaches the model. Each server that is not `connected` and not in
  `disabled_mcp_servers` gets an `mcp <server> in <owner>/<name>` line. It
  prints no value.

- `check-install` prints no `root mcp` line. For each root-home `mcp_servers`
  entry that is not `enabled: false`, it runs `hermes mcp test <server>` with
  the root home. It reports a server that does not connect, a server whose
  `sampling.enabled` is not `false`, and a header whose value is anything but
  one `${VAR}` reference, after at most one scheme word. It reports a
  `config.yaml` it cannot read as one `root mcp` line. It prints no header
  value.

`claude mcp list` shows a person's interactive view, not a worker's. To read
every status a worker run sees for one repository:

```sh
HERMES_HOME=<worker> python3 <worker>/scripts/claude-session --mcp-status --repo <owner>/<name>
```
