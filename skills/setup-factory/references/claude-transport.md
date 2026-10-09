# Claude Code transport

`claude-session` can send Claude Code to an Anthropic-compatible endpoint, for
example a local proxy such as EasyCLIProxyAPI. Claude Code still runs as the
harness; only the model endpoint changes. The endpoint is optional. When
`claude_session` is empty, Claude Code uses whatever its user configuration
says, which is its own login unless `~/.claude/settings.json` has an `env`
block.

Configure the transport only through these settings in
`plugins.entries.linear-agent-session.settings.claude_session`:

| Setting | Effect in the Claude Code process |
| --- | --- |
| `base_url` | Sets `ANTHROPIC_BASE_URL` |
| `model` | Passes `--model` and sets `ANTHROPIC_MODEL`, every `ANTHROPIC_DEFAULT_*_MODEL` tier, and `CLAUDE_CODE_SUBAGENT_MODEL` to the same alias |
| `auth_token_env` | Names a variable in `<worker>/.env`. `claude-session` copies its value into `ANTHROPIC_AUTH_TOKEN` |

`claude-session` writes these into a per-run `0600` file and passes it with
`--settings`. Claude Code lets an `env` block in the user's
`~/.claude/settings.json` override the process environment, so plain
environment variables would lose to it; `--settings` wins over user settings.
The file is deleted when the run ends. Never put an `ANTHROPIC_*` variable in a
`.env` file or in the gateway's service unit. The DirectSDK provider refuses to
start when it finds one.

`model` must be an alias the endpoint serves. A proxy answers
`unknown provider for model ...` for any other model, and Claude Code retries
until the run times out. Mapping every tier matters for the same reason:
Claude Code sends background and subagent requests to its Haiku and subagent
models.

## Check

When the operator uses no proxy, check that `base_url`, `model`, and
`auth_token_env` are empty in both homes and skip this part.

When the operator uses a proxy, all of these must hold:

- The proxy runs as a service that starts at login or boot.
- It listens on `127.0.0.1` only:
  `lsof -nP -iTCP:<proxy port> -sTCP:LISTEN` shows `127.0.0.1:<proxy port>`.
- `claude_session.base_url` is `http://127.0.0.1:<proxy port>` in both homes.
- `claude_session.model` is listed by the proxy:
  `curl -s http://127.0.0.1:<proxy port>/v1/models -H "x-api-key: <token or placeholder>"`.
- The proxy's management API does not accept remote connections. For
  EasyCLIProxyAPI, `management.allow-remote` is `false` and `server.host` is
  `127.0.0.1`.
- If the proxy requires a token, `env_has <worker>/.env <token variable>`
  prints `1`, and `auth_token_env` holds the variable's name. The name does not
  start with `ANTHROPIC_`.

## Install the proxy

1. Install the proxy with its own documented method. Ask the operator which
   proxy and version to use.
2. Configure it to bind `127.0.0.1` only.
3. Run it as a service. On macOS, use a launchd user agent. On Linux, use a
   systemd user unit. Keep the proxy's own credentials in the proxy's
   configuration, not in a Hermes `.env`.
4. If the proxy issues a client token, store it with `env_set` in
   `<worker>/.env` under a name such as `CLAUDE_PROXY_TOKEN`.
5. If the proxy has a management UI, record its paths. The tunnel routes them
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
