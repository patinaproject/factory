# Infisical secret source

This part is optional. Run it when the operator keeps secrets in
[Infisical](https://infisical.com). Hermes then loads the factory's secrets
from one Infisical folder each time a Hermes process starts, instead of from
`.env`.

Hermes documents this mechanism as the command helper secret source, in
`website/docs/user-guide/secrets/command.md` of the Hermes install. At startup,
after it loads `.env`, Hermes runs one configured command and reads its
`KEY=VALUE` output into the environment. A value that `.env` also sets keeps
the `.env` value, so this part removes each moved value from `.env` last.

These values move to Infisical:

| Value | Read by |
| --- | --- |
| `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` | Both homes |
| `LINEAR_WEBHOOK_SECRET`, `GITHUB_WEBHOOK_SECRET` | `<home>` |
| The token variable that `claude_session.auth_token_env` names | `<worker>` |

These values stay in `.env`:

- `FACTORY_INFISICAL_CLIENT_ID` and `FACTORY_INFISICAL_CLIENT_SECRET`, the
  machine identity credential that the command uses to sign in.
- Settings that are not secrets: `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` and
  `GH_CONFIG_DIR`.

The GitHub App private key stays a file at `github.private_key_path`.

## Inputs

Ask the operator for these. None of them is a secret.

| Input | Example |
| --- | --- |
| Infisical project ID | `<project id>` |
| Environment slug | `development` |
| Folder for the factory's secrets | `/factory` |
| The machine identity the factory signs in as | `<identity name>` |
| Infisical API URL, when it is not US Cloud | `https://eu.infisical.com` |

## Check

All of these must hold:

- `env_has <file> FACTORY_INFISICAL_CLIENT_ID` and
  `env_has <file> FACTORY_INFISICAL_CLIENT_SECRET` print `1` for both
  `<home>/.env` and `<worker>/.env`.
- `hermes config get secrets.command --json` and
  `hermes -p claude-worker config get secrets.command --json` each show
  `"enabled": true` and the command from
  [Configure both homes](#configure-both-homes).
- Each of those commands prints `Command helper: applied <n> secrets` on its
  first line, where `<n>` is the number of values in the folder.
- `hermes -p claude-worker config get terminal.env_passthrough --json` lists
  the token variable that `claude_session.auth_token_env` names.
- No moved value remains in a `.env` file: `env_has` prints `0` for each value
  in the first table, in both files.

If all of them hold, skip to [Verify](#verify).

## Create the machine identity

The factory signs in as one machine identity through Universal Auth. Give it
read access to the factory's environment and nothing else. Infisical adds
permissions together, so a role such as `Viewer` would widen any narrower
grant.

> **HUMAN CHECKPOINT.** Ask the operator to create the identity, or reuse an
> existing one, in **Organization → Access Control → Identities**. In the
> project, set these values:
>
> - **Project role:** `No Access`.
> - **Additional privilege:** permanent, on subject `secrets`, with the actions
>   `describeSecret` and `readValue`, and the condition `environment` equals
>   `<environment>`.
> - **Authentication:** Universal Auth.
>
> Then ask the operator to create a Universal Auth client secret for this
> machine, and store the client ID and the client secret with the `read -rs`
> line from the skill. Run it once per key, into both `<home>/.env` and
> `<worker>/.env`, under the names `FACTORY_INFISICAL_CLIENT_ID` and
> `FACTORY_INFISICAL_CLIENT_SECRET`.

Use the `FACTORY_INFISICAL_` names, not `INFISICAL_UNIVERSAL_AUTH_CLIENT_ID`
and `INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET`. Hermes passes both names to the
processes a worker starts, which include Claude Code. A repository's own
Infisical tooling can change how it signs in when it sees the Universal Auth
names.

The worker's processes can still read the `FACTORY_INFISICAL_` values. Keep
the identity's access to one environment, so a worker can read no other
secrets with it.

## Store the secrets

Create the folder under the operator's own Infisical login, which can write to
the project:

```sh
infisical secrets folders create --projectId <project id> --env <environment> \
  --path / --name <folder name>
```

For each value in the first table, put it into the folder without printing
it. A value that already exists in a `.env` file moves with this line. It
passes the value through a mode `0600` file that the line deletes afterwards:

```sh
( umask 077; f="$(mktemp)"; grep '^<KEY>=' <file> > "$f"
  infisical secrets set --projectId <project id> --env <environment> \
    --path <folder> --file "$f" >/dev/null; rm -f "$f" )
```

A value that does not exist yet, such as a new Linear client secret, goes in
through the operator's own terminal:

> **HUMAN CHECKPOINT.** Ask the operator to add the value to the folder in the
> Infisical web app, or to run this in their own terminal:
>
> ```sh
> read -rs VALUE && ( umask 077; f="$(mktemp)"; printf '%s=%s\n' <KEY> "$VALUE" > "$f"
>   infisical secrets set --projectId <project id> --env <environment> \
>     --path <folder> --file "$f" >/dev/null; rm -f "$f" ); unset VALUE
> ```

Check the folder by key names only:

```sh
infisical export --silent --projectId <project id> --env <environment> \
  --path <folder> --format dotenv | sed 's/=.*//'
```

## Configure both homes

The command signs in as the machine identity, then exports the folder. When
sign-in fails, it exports nothing, so it never falls back to the operator's
own Infisical login. Add `--domain <api url>` to both `infisical` calls when
the operator does not use US Cloud.

```sh
cmd='t=$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID="$FACTORY_INFISICAL_CLIENT_ID" INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET="$FACTORY_INFISICAL_CLIENT_SECRET" infisical login --method=universal-auth --silent --plain) && [ -n "$t" ] && INFISICAL_TOKEN="$t" infisical export --silent --projectId=<project id> --env=<environment> --path=<folder> --format=dotenv'
value="$(python3 -c 'import json, sys; print(json.dumps({"enabled": True, "helper_timeout_seconds": 20, "command": sys.argv[1]}))' "$cmd")"
hermes config set secrets.command "$value" </dev/null
hermes -p claude-worker config set secrets.command "$value" </dev/null
hermes -p claude-worker config set terminal.env_passthrough '["<token variable>"]' </dev/null
```

The sign-in and the export take about two seconds. The default timeout of
three seconds is too short, so the command sets 20.

`infisical` must be on the `PATH` that the gateway's service definition sets.
Check with `command -v infisical`, and compare the directory with that `PATH`.

`terminal.env_passthrough` keeps the proxy token reaching `claude-session`.
Hermes documents that the processes a worker starts receive only declared
credentials. Current Hermes also forwards an undeclared token, but the
declaration keeps it working when Hermes enforces the documented rule.

## Verify

Run these before you remove anything from `.env`. Each line prints `1` when
the value that Hermes loads matches the value in the folder:

```sh
match() {
  a="$(infisical export --silent --projectId <project id> --env <environment> --path <folder> --format json \
    | python3 -c 'import json, sys; print(next(s["value"] for s in json.load(sys.stdin) if s["key"] == sys.argv[1]))' "$1" \
    | shasum)"
  b="$(env -u "$1" hermes config get "platforms.webhook.extra.routes.$2.secret" --raw </dev/null 2>/dev/null | shasum)"
  [ "$a" = "$b" ] && echo 1 || echo 0
}
match LINEAR_WEBHOOK_SECRET <webhook_route>
match GITHUB_WEBHOOK_SECRET github
```

Then remove each moved value from `.env`. Back up both files first:

```sh
for key in <each key from the first table>; do
  for file in <home>/.env <worker>/.env; do
    grep -v "^${key}=" "$file" > "$file.tmp"; mv "$file.tmp" "$file"; chmod 600 "$file"
  done
done
```

Run the `match` lines again. They must still print `1`, which shows that the
values now come from Infisical and not from `.env`.

Part 15 restarts the gateway. The webhook live tests in
[`linear-app.md`](linear-app.md) and [`github.md`](github.md) then confirm that
the routes accept signed deliveries.

## Rotation

Hermes reads Infisical once, when each process starts. After a secret changes
in Infisical, restart the gateway. Kanban workers start as new processes and
read the new value on their next run.

A Universal Auth client secret can have an expiry. When it does, record the
date where the operator tracks credential expiry. An expired client secret
leaves every moved value unset at the next start.
