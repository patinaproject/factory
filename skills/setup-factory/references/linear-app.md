# Linear agent app

The factory acts in Linear as an OAuth application with agent session events.
Every comment and activity comes from the app. The `linear-agent-session`
plugin authenticates with the app's `LINEAR_CLIENT_ID` and
`LINEAR_CLIENT_SECRET`. It mints client credentials tokens with the scopes
`read,write,app:assignable,app:mentionable`, and mints a new one when a token
expires or Linear rejects it. Put both values in `<home>/.env` only. The
worker home holds neither: its copy of the plugin asks the gateway's broker,
which part 5 of the skill installs, for a token the gateway minted.

## Check

All of these must hold:

- `env_has <home>/.env LINEAR_CLIENT_ID` and
  `env_has <home>/.env LINEAR_CLIENT_SECRET` each print `1`, and the same
  lines for `<worker>/.env` each print `0`.
- The token acts as the app (see [Verify](#verify)).
- `env_has <home>/.env LINEAR_WEBHOOK_SECRET` prints `1`.
- The plugin can read an issue as the app (see
  [Find the app user ID](#find-the-app-user-id)).
- `app_user_id` in the settings is the app's user ID.

If all of them hold, skip to the live test.

## Create the app

> **HUMAN CHECKPOINT.** Ask the operator to create an OAuth application in
> Linear's API settings for the workspace, with these values:
>
> - **Name:** the operator's choice for the agent's display name.
> - **Webhooks:** enabled, with the URL `https://<hostname>/webhooks/linear`.
> - **Events:** agent session events, plus issue events.
> - **Client credentials:** enabled.
>
> Then ask the operator to store the client ID and client secret with the
> `read -rs` line from the skill, once per key, into `<home>/.env`.

**Existing app.** If the operator already has an agent app, keep it. Ask
the operator to turn on **Client credentials** in the app's settings, confirm
that its webhook URL and events match the values above, and store its client
ID and client secret the same way.

Check `<home>/.env` with `env_has` afterwards.

## Store the signing secret

The webhook signing secret is `LINEAR_WEBHOOK_SECRET` in `<home>/.env`. If the
Linear app form shows a signing secret of its own, ask the operator to store
that value with the `read -rs` line. If the form asks for a secret instead,
ask the operator to generate it with the clipboard line from the skill and
paste it into the form:

```sh
openssl rand -hex 32 | tee >(pbcopy) | env_set <home>/.env LINEAR_WEBHOOK_SECRET
```

When the Infisical folder already exists, the Linear credentials and this
secret go there. Replace each `env_set <file> <KEY>` with `infisical_set <KEY>`,
once per key.

## Install the app in the workspace

> **HUMAN CHECKPOINT.** Ask the operator to install the app through Linear's
> OAuth consent screen with `actor=app` and exactly these scopes:
> `read,write,app:assignable,app:mentionable`.

The plugin requests the same scope set on every client credentials token.
Linear revokes all of an app's client credentials tokens when a request asks
for a different scope set, so never change the scopes in one place only.

## Find the app user ID

Ask the operator to delegate a test issue to the app. Then read the issue as
the app with the plugin's command-line entry point. `HERMES_HOME` places the
token cache under `<home>/plugin-data/linear-agent-session/`.

```sh
( set -a; . <home>/.env; set +a
  HERMES_HOME=<home> python3 <repo>/plugins/linear-agent-session/cli.py issue <ABC-123> )
```

The command exits `0` and prints the issue's `gitBranchName`. The JSON
output's `delegate.id` is the app user ID. Use it as `app_user_id` in part 10.

## Verify

Check that the token acts as the app with a read-only `viewer` query. The
plugin's client picks the credential the same way the plugin does, and the
command never prints the token:

```sh
( set -a; . <worker>/.env; set +a
  HERMES_HOME=<worker> python3 -c 'import json, sys; sys.path.insert(0, sys.argv[1]); import linear; print(json.dumps(linear.client_from_env()._graphql("{ viewer { id name app } }", {})))' \
    <repo>/plugins/linear-agent-session )
```

Run it again with `<home>/.env` and `HERMES_HOME=<home>`. In the worker home
the client asks the gateway's broker for its token, so that run passes only
once the gateway runs the broker from part 5 of the skill.

Expect `"app": true`. `viewer.id` is the app user ID, and it equals
`app_user_id` in the settings. `"app": false` means the token acts as a person,
not the app. Replace it with a token that Linear issued with `actor=app`.

## Give Claude Code sessions Linear

Claude Code sessions in a checkout of this repository reach Linear through
the app, so an agent never asks the operator to sign in. The `linear` server
in the repository's `.mcp.json` has a `headersHelper` that runs
[`scripts/linear-mcp-headers`](../scripts/linear-mcp-headers). The script
sends the app's token from the plugin's client, which shares the gateway's
token cache in `~/.hermes`. Never get the token another way. Linear revokes
all of an app's client credentials tokens when a request asks for a different
scope set, and the gateway then loses its token.

On a machine without the plugin, the script prints no headers, and Claude Code
asks for the usual OAuth sign-in.

A worker run gets the Linear server in every repository, including one whose
`.mcp.json` has no `headersHelper`. `claude-session` passes Claude Code a
`--mcp-config` with a `linear` server whose `Authorization` header is
`Bearer ${LINEAR_API_KEY}`. Claude Code fills in the run's token from the
settings file. That entry replaces the repository's own `linear` server for
the run only, so the repository needs no change, and a person's interactive
session keeps the repository's own sign-in.

Everything written through this server shows the app as its author.

**Check.** In a checkout of this repository, run `claude mcp list` as the
gateway's user. Expect `linear: https://mcp.linear.app/mcp (HTTP) - ✔ Connected`.

There is nothing to install. If the check reports `Needs authentication`,
confirm that `<home>/plugins/linear-agent-session` exists and that
`python3 -I skills/setup-factory/scripts/linear-mcp-headers` prints an
`Authorization` key. Do not print its value.

## Live test

Run this after part 15 has restarted the gateway. Delegate a new test issue to
the app. Expect these in the issue's agent session:

1. An acknowledgement `thought` within 10 seconds.
2. An `action` activity that says the issue was queued, with the card link.

If the acknowledgement does not arrive, check that the public checks in
[`cloudflare-sst.md`](cloudflare-sst.md) pass, then check the gateway log for
the delivery's status. `401` means the signing secret in `<home>/.env` does not
match the app's.
