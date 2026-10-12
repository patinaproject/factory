# Webhook route templates

`render.py` builds the Hermes webhook config from the `linear-agent-session`
plugin settings. It prints one JSON object whose keys are Hermes config keys and
whose values are what `hermes config set` installs at them:

- `platforms.webhook.extra.routes.<name>`: the `linear` and `github` route
  configs. The Linear route takes its name from the `webhook_route` setting.
- `platforms.webhook.extra.rate_limit`: the deliveries each route accepts per
  minute. Hermes counts every signed delivery before it applies the route's
  events and filters, and answers `429` past the limit. GitHub does not
  redeliver a rejected event. The default of 30 dropped review events, so the
  factory sets 600.

The triage prompts are `triage-linear.md` and `triage-github.md`. In them,
`<<name>>` marks a value the renderer fills from settings, and `{dot.path}` is a
Hermes payload placeholder that the webhook adapter fills per event.

Each route reads its secret from `.env` through a `${LINEAR_WEBHOOK_SECRET}` or
`${GITHUB_WEBHOOK_SECRET}` reference, which Hermes expands when it loads
`config.yaml`.

## Install the webhook config

```sh
python3 templates/render.py > webhook-config.json
for key in $(python3 -c 'import json, sys; print(*json.load(sys.stdin))' < webhook-config.json); do
  hermes config set "$key" \
    "$(python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]]))' "$key" < webhook-config.json)"
done
```

Run it again after changing the settings, then restart the gateway. Pass
`--settings-json <file>` to render from a file instead of `hermes config get`.

## Check the installed webhook config

```sh
python3 templates/render.py --check
```

It prints one line per route that says whether the installed route matches
the rendered one, and whether its secret is the `${...}` reference. A last
line says whether the installed rate limit matches. It never prints a secret
value. It exits `1` when a route or the rate limit differs or is missing, or a
route holds a literal secret.

## Hermes agent memory

`hermes-memory.txt` holds the entries that `memory.py` installs in the memory
file of Hermes' own agent, `<home>/memories/MEMORY.md`. Entries are separated
by a line that holds only `§`, and each starts with a label, such as
`GitHub:`. `<<github_login>>` and `<<gh_config_dir>>` are filled from the
settings and from `--gh-config-dir`. Keep the entries short: Hermes limits the
whole file to 2,200 characters, and the agent adds its own entries.
