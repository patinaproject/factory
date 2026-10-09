# Slack

The factory talks to the operator in Slack through Hermes Agent's own Slack
integration. This repository adds nothing to it. Follow the Slack page in the
Hermes install's messaging documentation for the app manifest, the scopes, and
the token names that the pinned release expects.

## Check

All of these must hold:

- The Slack platform is enabled in `<home>/config.yaml`, and its tokens exist
  in `<home>/.env`. Check each token with `env_has`, using the names from the
  Hermes Slack documentation.
- The Slack app is a member of each channel the operator listed.
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

## Verify

Run this after part 15 has restarted the gateway.

> **HUMAN CHECKPOINT.** Ask the operator to mention the app in one of the
> channels with a short question.

Expect a reply in the same thread. If no reply arrives, check the gateway log
for a Slack connection error.
