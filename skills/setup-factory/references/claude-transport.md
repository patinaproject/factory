# Claude Code transport

`claude-session` can send Claude Code to an Anthropic-compatible endpoint, for
example a local proxy. Claude Code still runs as the
harness; only the model endpoint changes. Hermes' own agent loop can use the
same proxy through the proxy provider in part 4 of the skill. The endpoint is
optional. When
`claude_session` is empty, Claude Code uses whatever its user configuration
says, which is its own login unless `~/.claude/settings.json` has an `env`
block.

Configure the transport only through these settings in
`plugins.entries.linear-agent-session.settings.claude_session`:

| Setting | Effect in the Claude Code process |
| --- | --- |
| `base_url` | Sets `ANTHROPIC_BASE_URL` |
| `model` | Passes `--model` and sets `ANTHROPIC_MODEL` and `ANTHROPIC_DEFAULT_OPUS_MODEL`. The main agent runs on it. Any tier without a `tier_models` entry, and the subagent default, also use it |
| `tier_models` | Optional map from `sonnet`, `haiku`, and `fable` to model ids the endpoint serves. Each sets that `ANTHROPIC_DEFAULT_<TIER>_MODEL`. When `sonnet` is set, `CLAUDE_CODE_SUBAGENT_MODEL` is the alias `sonnet`, so a subagent that names no model runs on the Sonnet model and an explicit `opus` still runs on `model`. Any other key or a non-string value refuses the run |
| `auth_token_env` | Names the variable that holds the proxy's token, `CLAUDE_PROXY_TOKEN`. Only the gateway holds its value. `claude-session` sets Claude Code's `apiKeyHelper` to `<worker>/scripts/proxy-token`, which asks the gateway's broker for the token. Claude Code keeps it in memory and sends it as the `Authorization` and `x-api-key` headers, so neither Claude Code's environment nor any process it starts holds it |
| `mcp_servers` | Optional map from an MCP server name to the factory's own server on that service: its `url`, the `token_env` variable that only the gateway holds, and an optional `scheme` and fixed `headers`. Each server gets its token through a `headersHelper`, `<worker>/scripts/mcp-headers`, which asks the broker for it. See [MCP server identities](mcp-identities.md) |
| `disabled_mcp_servers` | Optional list of repository MCP server names. Sets `disabledMcpjsonServers` |

`claude-session` writes these into a per-run `0600` file and passes it with
`--settings`. Claude Code lets an `env` block in the user's
`~/.claude/settings.json` override the process environment, so plain
environment variables would lose to it; `--settings` wins over user settings.
The file is deleted when the run ends. Never put an `ANTHROPIC_*` variable in a
`.env` file or in the gateway's service unit. The DirectSDK provider refuses to
start when it finds one.

`claude_session.model`, `model.default`, and each `model` under
`platforms.slack.channel_overrides` must be an alias the endpoint serves. A
proxy answers `unknown provider for model ...` for any other model, and Claude
Code retries until the run times out. Mapping every tier matters for the same reason:
Claude Code sends background and subagent requests to its Haiku and subagent
models. A tier with no `tier_models` entry falls back to `model`,
which keeps a one-model proxy working but sends every subagent to the main
agent's model. `check-install` reports a `model` with no `tier_models.sonnet`.

## Check

When the operator uses no proxy, check that `base_url`, `model`, and
`auth_token_env` are empty in both homes and skip this part.

When the operator uses a proxy, all of these must hold:

- The proxy runs as a service that starts at login or boot. A GUI proxy app
  with its own launch-at-login setting counts when that setting is on and
  nothing else starts a second copy on the same port.
- No `ANTHROPIC_*` setting exists in Claude Code's `settings.json` or in
  either Hermes `config.yaml`. The check in part 4 of the skill reads them.
- It listens on `127.0.0.1` only:
  `lsof -nP -iTCP:<proxy port> -sTCP:LISTEN` shows `127.0.0.1:<proxy port>`.
- `claude_session.base_url` is `http://127.0.0.1:<proxy port>` in both homes.
- `claude_session.model`, each `claude_session.tier_models` value, and each
  `model` under `platforms.slack.channel_overrides` is listed by the proxy:
  `curl -s http://127.0.0.1:<proxy port>/v1/models -H "x-api-key: <token or placeholder>"`.
- The proxy listens only on `127.0.0.1`. If it has a management API, that API
  does not accept remote connections.
- If the proxy requires a token, `auth_token_env` is `CLAUDE_PROXY_TOKEN`, and
  `env_has <home>/.env CLAUDE_PROXY_TOKEN` prints `1` or the Infisical folder
  holds it. `env_has <worker>/.env CLAUDE_PROXY_TOKEN` prints `0`: the worker
  gets the token from the gateway, as part 5 of the skill sets up. The
  `factory-secret-names` plugin declares only that name, so Hermes strips it
  from every process a home starts. `check-install` reports any other name.

## Install the proxy

1. Install the proxy with its own documented method. Ask the operator which
   proxy and version to use.
2. Configure it to bind `127.0.0.1` only.
3. Run it as a service. On macOS, use a launchd user agent. On Linux, use a
   systemd user unit. A GUI proxy app can use its own launch-at-login setting
   instead. Use one of the two, never both, because two copies compete for
   the port. Keep the proxy's own credentials in the proxy's configuration,
   not in a Hermes `.env`.
4. Do not use the proxy's "configure Claude Code" or "configure Hermes"
   action. It writes `ANTHROPIC_*` into Claude Code's `settings.json` or into
   Hermes' `config.yaml`, where it overrides `claude_session` and stops the
   DirectSDK provider.
5. If the proxy issues a client token, store it with `env_set` in
   `<home>/.env` under the name `CLAUDE_PROXY_TOKEN`. When the Infisical
   folder already exists, store it there with `infisical_set` instead. Never
   store it in `<worker>/.env`.
6. If the proxy has a management UI, record its paths. The tunnel routes them
   to the proxy behind Access in
   [`cloudflare-sst.md`](cloudflare-sst.md).

## Verify

Run Claude Code with the same `--settings` override that `claude-session`
passes. Run it from a scratch directory, never from a repository checkout, and
write the settings file with mode `0600`:

```sh
cd "$(mktemp -d)"
umask 077
python3 -c 'import json,os,sys; m=sys.argv[2]; print(json.dumps({"env": {"ANTHROPIC_BASE_URL": sys.argv[1], "ANTHROPIC_AUTH_TOKEN": os.environ.get(sys.argv[3], "placeholder"), **{k: m for k in ("ANTHROPIC_MODEL","ANTHROPIC_DEFAULT_OPUS_MODEL","ANTHROPIC_DEFAULT_SONNET_MODEL","ANTHROPIC_DEFAULT_HAIKU_MODEL","ANTHROPIC_DEFAULT_FABLE_MODEL","CLAUDE_CODE_SUBAGENT_MODEL")}}}))' \
  <base_url> <model> <token variable> > settings.json
claude -p "Reply with exactly: PONG" --model <model> --settings settings.json --output-format json
```

Expect `"result": "PONG"` and `modelUsage` naming `<model>`. As a control,
repeat with `base_url` set to `http://127.0.0.1:9`: the run must not succeed.
If it does, something other than `--settings` is choosing the endpoint. The full
`claude-session` path runs in the end-to-end checks of part 15, because it
needs a Kanban worktree.

When `tier_models` is set, also prove the routing. Write a settings file whose
`env` holds `ANTHROPIC_MODEL` and `ANTHROPIC_DEFAULT_OPUS_MODEL` as `<model>`,
`ANTHROPIC_DEFAULT_SONNET_MODEL` as `<sonnet id>`, and
`CLAUDE_CODE_SUBAGENT_MODEL` as `sonnet`. Run `claude -p` with `--model <model>`
and a prompt that spawns one `Agent` with `model: "sonnet"`, one with no
`model`, and one with `model: "opus"`. In the session's
`subagents/agent-*.jsonl`, `message.model` must be `<sonnet id>`,
`<sonnet id>`, and `<model>`.
