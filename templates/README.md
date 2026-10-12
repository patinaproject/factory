# Webhook route templates

`render.py` builds the Hermes `linear` and `github` webhook routes from the
`linear-agent-session` plugin settings. It prints one JSON object whose keys are
route names and whose values are route configs for
`platforms.webhook.extra.routes`. The Linear route takes its name from the
`webhook_route` setting.

The triage prompts are `triage-linear.md` and `triage-github.md`. In them,
`<<name>>` marks a value the renderer fills from settings, and `{dot.path}` is a
Hermes payload placeholder that the webhook adapter fills per event.

Each route reads its secret from `.env` through a `${LINEAR_WEBHOOK_SECRET}` or
`${GITHUB_WEBHOOK_SECRET}` reference, which Hermes expands when it loads
`config.yaml`.

## Install the routes

```sh
python3 templates/render.py > routes.json
for name in $(python3 -c 'import json, sys; print(*json.load(sys.stdin))' < routes.json); do
  hermes config set "platforms.webhook.extra.routes.$name" \
    "$(python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]]))' "$name" < routes.json)"
done
```

Run it again after changing the settings, then restart the gateway. Pass
`--settings-json <file>` to render from a file instead of `hermes config get`.

## Check the installed routes

```sh
python3 templates/render.py --check
```

It prints one line per route that says whether the installed route matches
the rendered one, and whether its secret is the `${...}` reference. It never
prints a secret value. It exits `1` when a route differs, is missing, or holds
a literal secret.

## Hermes agent memory

`hermes-memory.txt` holds the entries that `memory.py` installs in the memory
file of Hermes' own agent, `<home>/memories/MEMORY.md`. Entries are separated
by a line that holds only `§`, and each starts with a label, such as
`GitHub:`. `<<github_login>>` and `<<gh_config_dir>>` are filled from the
settings and from `--gh-config-dir`. Keep the entries short: Hermes limits the
whole file to 2,200 characters, and the agent adds its own entries.
