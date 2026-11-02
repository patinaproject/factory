# Slack

The factory talks to the operator in Slack through Hermes Agent's own Slack
integration. This repository adds no code to it, only display settings and a
Slack profile that answers every Slack conversation on the operator's Slack
model.
Follow the Slack page in the Hermes install's messaging documentation for the
app manifest, the scopes, and the token names that the pinned release expects.

## Check

All of these must hold:

- The Slack platform is enabled in `<home>/config.yaml`, and its tokens exist
  in `<home>/.env`. Check each token with `env_has`, using the names from the
  Hermes Slack documentation.
- The Slack app is a member of each channel the operator listed.
- `streaming.enabled` and `platforms.slack.extra.native_task_cards` are `true`
  in `<home>/config.yaml` (see [Show progress](#show-progress)).
- `check-install` prints no `slack profile` line. Every Slack message reaches
  the Slack profile on the operator's Slack model (see
  [Route Slack to the Slack profile](#route-slack-to-the-slack-profile)).
- A message round-trip works (see [Verify](#verify)).

If all of them hold, report Slack as already configured.

## Create the Slack app

> **HUMAN CHECKPOINT.** Ask the operator to create a Slack app in their
> workspace from the manifest or scope list in the Hermes Slack documentation,
> then install it to the workspace.

## Store the tokens

> **HUMAN CHECKPOINT.** Ask the operator to run `hermes gateway setup` in their
> own terminal and choose Slack. The command asks for the tokens and writes
> them to `<home>/.env`, so they never pass through you.

Afterwards, check each token with `env_has` and check that the platform is
enabled.

## Join the channels

> **HUMAN CHECKPOINT.** Ask the operator to invite the app to each channel
> with `/invite @<app name>`.

## Show progress

Hermes defaults Slack to show only the final answer. It turns Slack's text
tool progress off, because bot posts cannot be edited in place and each step
would stay in the channel as its own line. The operator then cannot see what
the agent is doing. Two settings fix this:

- `streaming.enabled` edits the reply in place while it is written. It is a
  top-level key, so it also turns on streaming for every other gateway
  platform that supports it.
- `platforms.slack.extra.native_task_cards` shows each tool call as a Slack
  plan or task card, which updates in place.

Check both:

```sh
hermes config get streaming.enabled                          # true
hermes config get platforms.slack.extra.native_task_cards    # true
```

Set any that differ:

```sh
hermes config set streaming.enabled true </dev/null
hermes config set platforms.slack.extra.native_task_cards true </dev/null
```

The gateway reads both at startup, so part 15's restart applies them.

## Route Slack to the Slack profile

The factory answers every Slack conversation in its own Hermes profile, the
Slack profile `<slack profile>`. Its `model.default` is the operator's Slack
model, `<slack model>`. One profile route in the default home sends every
Slack channel, thread, and direct message there, so no channel list exists.
Webhook turns stay in the default profile on its `model.default`. The worker
profile's Kanban runs keep their own model.

Hermes has no Slack-only model setting. `platforms.slack.channel_overrides`
matches one channel ID at a time, and a direct message matches only when its
`D...` ID is listed, so the factory does not use it.

Ask the operator for:

- the profile name, `<slack profile>`. Use a lowercase name such as
  `slack-chat`. It must not be `default` or `claude-worker`.
- the Slack model, `<slack model>`. It must be a model id the endpoint serves
  (part 4).

Record both in the private record only.

### What stays in the default profile

The default profile's Slack adapter receives and authorizes every Slack
message before Hermes routes it. So the default profile keeps:

- the Slack bot, its `SLACK_*` tokens in `<home>/.env`, and
  `platforms.slack.extra`;
- Slack authorization: `GATEWAY_ALLOWED_USERS` and the Slack allowlists.

The Slack profile has no `platforms.slack` block and no `SLACK_*` token. Hermes
parks a profile that repeats another profile's bot token.

### Create the profile

`--clone` copies the default profile's `config.yaml`, `.env`, `SOUL.md`, and
memory files, and leaves its messaging channels and their tokens behind:

```sh
hermes profile create <slack profile> --clone </dev/null
```

Then, with `<slack>` as `<home>/profiles/<slack profile>`:

1. Set the model and turn the copied secret helper off:

   ```sh
   HERMES_HOME=<slack> hermes config set model.default <slack model> </dev/null
   HERMES_HOME=<slack> hermes config set secrets.command.enabled false </dev/null
   ```

   `model.provider` and `providers.*` stay the copied values, so Slack turns
   use the same transport as the default profile.

2. Remove every `platforms.slack` key, and any top-level `slack:` block, from
   `<slack>/config.yaml`.
3. Install the factory plugins a chat turn needs into the profile the way
   part 5 installs them into the default home. Copy the default home's skills
   directory, and render `templates/hermes-memory.txt` into its memory as
   part 17 does for the default home. The Kanban board stays shared.

### Give the profile its own secrets

Hermes never shares credentials across profiles. A multiplexed turn resolves
each `${VAR}` reference and provider key from the routed profile's own `.env`.
So `<slack>/.env` holds exactly these, at mode `0600`:

- the proxy token or Console API key that `providers.<provider>.key_env`
  names, `CLAUDE_PROXY_TOKEN`, when the provider needs one;
- each token that a header in the profile's `mcp_servers` references, such as
  `SENTRY_ACCESS_TOKEN`.

Delete every other line that `--clone` copied, such as the Linear app's
credentials, the webhook secrets, and the Infisical identity. The default
home keeps its copy of each token, because the gateway's broker serves the
proxy token and each MCP token to worker runs from there.

> **HUMAN CHECKPOINT.** Ask the operator to enter each value in their own
> terminal with `env_set <slack>/.env <NAME>`, as "Where each value lives"
> describes. The values never pass through you.

Check each name with `env_has <slack>/.env <NAME>`.

### Move the root agent's MCP servers

The default profile's native `mcp_servers` serve chat turns only. Move each
entry into the Slack profile and remove it from the default home, as
[MCP server identities](mcp-identities.md#root-agent) describes. MCP servers
connect per profile, so a webhook turn in the default profile no longer sees
them.

### Add the route

Check, in the default home:

```sh
hermes config get gateway.multiplex_profiles   # true
hermes config get gateway.profile_routes
```

Set multiplexing and the one Slack route when they differ. The route names
only `platform` and `profile`. A `chat_id`, `thread_id`, `guild_id`,
`user_id`, or `bot_profile` would narrow it to part of Slack:

```sh
hermes config set gateway.multiplex_profiles true </dev/null
value="$(python3 -c 'import json, sys; print(json.dumps([{"platform": "slack", "profile": sys.argv[1]}]))' "<slack profile>")"
hermes config set gateway.profile_routes "$value" </dev/null
```

When `gateway.profile_routes` already holds routes for other platforms, keep
them and add the Slack route to the list. Remove any
`platforms.slack.channel_overrides` from the default home:

```sh
hermes config unset platforms.slack.channel_overrides </dev/null
```

The gateway reads routes and profiles at startup. A change on a running
factory needs the operator to restart the gateway. For setup, part 15's
restart applies it.

### Known costs

- A session's `/model` choice still wins over the Slack profile's
  `model.default`.
- While the Slack profile is missing or parked, the gateway drops every Slack
  message. The default profile does not answer in its place.
- A Slack turn cannot post to an unrouted channel or deliver a cron job there
  through the shared bot.

`check-install` reports each of these as a `slack profile` line: a missing,
disabled, narrowed, or second Slack route; a route to the default or worker
profile; `gateway.multiplex_profiles` not `true`; a Slack profile that is
missing, parked, or `gateway.standalone`; a Slack profile with a
`platforms.slack` block or a `SLACK_*` variable in its `.env`; a variable that
its `config.yaml` references and its `.env` lacks; a `model.provider` that
differs from the default home's; a `model.default` that the provider's
`/v1/models` does not list; any `mcp_servers` entry in the default home; and
any `platforms.slack.channel_overrides`. It prints no secret value.

## Verify

Run this after part 15 has restarted the gateway.

> **HUMAN CHECKPOINT.** Ask the operator to mention the app in one of the
> channels with a short question.

Expect a reply in the same thread that streams in as it is written. Then ask
the operator to send the app a direct message. Expect a reply there too. The
Slack profile keeps its own sessions, so this prints `<slack model>`:

```sh
sqlite3 <slack>/state.db "select model from sessions order by started_at desc limit 1"
```

When the
question makes the agent use a tool, expect a task card for each tool call. If
no reply arrives, check the gateway log for a Slack connection error. If the
reply arrives only when it is complete, or shows no task cards, recheck
[Show progress](#show-progress).
