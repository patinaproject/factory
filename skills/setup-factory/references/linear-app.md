# Linear agent app

The factory acts in Linear as an OAuth application with agent session events.
Every comment and activity comes from the app. The `linear-agent-session`
plugin accepts one of two credentials:

- `LINEAR_ACCESS_TOKEN`: an OAuth access token that Linear issued to an
  existing agent app with `actor=app`. The plugin uses it as it is and never
  mints a new one. When Linear rejects it with `401`, the plugin reports the
  error and does not retry.
- `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET`: the plugin mints client
  credentials tokens with the scopes `read,write,app:assignable,app:mentionable`.

When `LINEAR_ACCESS_TOKEN` has a value, the plugin uses it and ignores the
client credentials. Put the credential you use in both `<home>/.env` and
`<worker>/.env`.

## Check

All of these must hold:

- In both `<home>/.env` and `<worker>/.env`, either
  `env_has <file> LINEAR_ACCESS_TOKEN` prints `1`, or
  `env_has <file> LINEAR_CLIENT_ID` and `env_has <file> LINEAR_CLIENT_SECRET`
  each print `1`.
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
> `read -rs` line from the skill, once per key, into both `<home>/.env` and
> `<worker>/.env`.

**Existing app.** If the operator already has an agent app and an access token
that Linear issued to it with `actor=app`, use that token in place of the
checkpoint above. The app's webhook URL and events must still match the
values above.

> **HUMAN CHECKPOINT.** Ask the operator to store the token as
> `LINEAR_ACCESS_TOKEN` with the `read -rs` line from the skill, into both
> `<home>/.env` and `<worker>/.env`. Skip the install below.

Check both files with `env_has` afterwards.

## Store the signing secret

The webhook signing secret is `LINEAR_WEBHOOK_SECRET` in `<home>/.env`. If the
Linear app form shows a signing secret of its own, ask the operator to store
that value with the `read -rs` line. If the form asks for a secret instead,
ask the operator to generate it with the clipboard line from the skill and
paste it into the form:

```sh
openssl rand -hex 32 | tee >(pbcopy) | env_set <home>/.env LINEAR_WEBHOOK_SECRET
```

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

Run it again with `<home>/.env` and `HERMES_HOME=<home>`.

Expect `"app": true`. `viewer.id` is the app user ID, and it equals
`app_user_id` in the settings. `"app": false` means the token acts as a person,
not the app. Replace it with a token that Linear issued with `actor=app`.

## Live test

Run this after part 14 has restarted the gateway. Delegate a new test issue to
the app. Expect these in the issue's agent session:

1. An acknowledgement `thought` within 10 seconds.
2. An `action` activity that says the issue was queued, with the card link.

If the acknowledgement does not arrive, check that the public checks in
[`cloudflare-sst.md`](cloudflare-sst.md) pass, then check the gateway log for
the delivery's status. `401` means the signing secret in `<home>/.env` does not
match the app's.
