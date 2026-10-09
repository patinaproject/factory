# Claude Code transport

`claude-session` can send Claude Code to an Anthropic-compatible endpoint, for
example a local proxy such as EasyCLIProxyAPI. The endpoint is optional. When
`claude_session.base_url` is empty, the CLI uses its own login.

Configure the transport only through these settings in
`plugins.entries.linear-agent-session.settings.claude_session`:

| Setting | Effect in the Claude Code process |
| --- | --- |
| `base_url` | Sets `ANTHROPIC_BASE_URL` |
| `model` | Sets `ANTHROPIC_MODEL` and passes `--model` |
| `auth_token_env` | Names a variable in `<worker>/.env`. `claude-session` copies its value into `ANTHROPIC_AUTH_TOKEN` |

`claude-session` sets these variables only in the Claude Code process it
starts. Never put an `ANTHROPIC_*` variable in a `.env` file or in the gateway's
service unit. The DirectSDK provider refuses to start when it finds one.

## Check

When the operator uses no proxy, check that `base_url`, `model`, and
`auth_token_env` are empty in both homes and skip this part.

When the operator uses a proxy, all of these must hold:

- The proxy runs as a service that starts at login or boot.
- It listens on `127.0.0.1` only:
  `lsof -nP -iTCP:<proxy port> -sTCP:LISTEN` shows `127.0.0.1:<proxy port>`.
- `claude_session.base_url` is `http://127.0.0.1:<proxy port>` in both homes.
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

Run Claude Code with the same three variables that `claude-session` sets. Run
it from a scratch directory, never from a repository checkout:

```sh
cd "$(mktemp -d)"
set -a; . <worker>/.env; set +a
ANTHROPIC_BASE_URL=<base_url> ANTHROPIC_MODEL=<model> ANTHROPIC_AUTH_TOKEN="${<token variable>}" \
  claude -p "Reply with exactly: PONG" --model <model>
```

Expect `PONG`, and expect a matching request in the proxy's log. The full
`claude-session` path runs in the end-to-end checks of part 14, because it
needs a Kanban worktree.
