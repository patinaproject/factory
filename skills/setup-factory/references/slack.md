# Slack

The factory talks to the operator in Slack through Hermes Agent's own Slack
integration. This repository adds no code to it, only display settings and an
optional conversation model.
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
- When the operator chose a conversation model, `platforms.slack.channel_overrides`
  holds an entry for each channel they named, with the chosen `model` and,
  when they gave one, `provider` (see
  [Choose the conversation model](#choose-the-conversation-model)).
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

## Choose the conversation model

By default the agent answers in Slack with `model.default`. The operator may
choose a different model for Slack conversations. Ask whether to use one. If
so, ask for:

- a model id, `<model id>`;
- optionally a provider, `<provider>`, in the form of `model.provider`, for
  example `custom:<proxy provider>`;
- the channels it covers, defaulting to the home channel.

Hermes reads one `platforms.slack.channel_overrides` entry per channel ID. A
thread uses its channel's entry. A direct message matches only when its `D...`
ID is listed. A session's `/model` choice wins over the entry, and the entry
wins over `model.default`. Webhook turns and the worker profile's Kanban runs
keep `model.default`.

Check, in the root home:

```sh
hermes config get platforms.slack.channel_overrides
```

For each channel `<channel id>` that lacks the choice, set it. Set `provider`
only when the operator gave one:

```sh
hermes config set platforms.slack.channel_overrides.<channel id>.model <model id> </dev/null
hermes config set platforms.slack.channel_overrides.<channel id>.provider <provider> </dev/null
```

`hermes config set` splits the key at each `.`, and a channel ID has no dot.
Leave existing entries and their other keys, such as `system_prompt`, as they
are. Each `model` must be a model id the endpoint serves (part 4). Record the
model id, the provider, and the channel IDs in the private record only, so a fresh
install can reproduce the choice.

Hermes silently ignores an entry that is not a mapping. A top-level `slack:`
block in `<home>/config.yaml` replaces `platforms.slack` and hides the
entries, so the choice must not sit beside one.

The gateway reads the entries at startup. A change on a running factory needs
the operator to restart the gateway. For setup, part 15's restart applies it.

## Verify

Run this after part 15 has restarted the gateway.

> **HUMAN CHECKPOINT.** Ask the operator to mention the app in one of the
> channels with a short question.

Expect a reply in the same thread that streams in as it is written. When the
question makes the agent use a tool, expect a task card for each tool call. If
no reply arrives, check the gateway log for a Slack connection error. If the
reply arrives only when it is complete, or shows no task cards, recheck
[Show progress](#show-progress).
