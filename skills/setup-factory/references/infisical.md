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
  [Configure both homes](#configure-both-homes). When the factory uses
  [1Password](1password.md), the helper also carries that part's filter.
- Each of those commands prints `Command helper: applied <n> secrets` on its
  first line, where `<n>` is the number of values in the folder. When the
  folder holds `OP_SERVICE_ACCOUNT_TOKEN` for [1Password](1password.md),
  `<n>` is the number of values minus one.
- `<home>/bin/factory-secrets` exists, and the `cmp -s` check in
  [Configure both homes](#configure-both-homes) prints `same`, or the operator
  approved the differences there. When the factory uses
  [1Password](1password.md), the only other change is that part's filter.
- `hermes -p claude-worker config get terminal.env_passthrough --json` lists
  the token variable that `claude_session.auth_token_env` names.
- No moved value remains in a `.env` file: `env_has` prints `0` for each value
  in the first table, in both files.
- The machine identity reads one environment and nothing else, as
  [Check the identity's scope](#check-the-identitys-scope) shows.

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

### Check the identity's scope

Run this for a new identity and for a reused one. A reused identity often
carries grants from its earlier use, such as a project role or a second
project.

> **HUMAN CHECKPOINT.** Ask the operator to open the identity in
> **Organization → Access Control → Identities** and read its projects. It
> belongs to exactly one project, with project role `No Access`, and its only
> additional privilege is the one in
> [Create the machine identity](#create-the-machine-identity).

Then sign in as the identity and try each other environment of the project.
New projects have `dev`, `staging`, and `prod`. The project's settings page
lists its environment slugs. These lines read only the two
`FACTORY_INFISICAL_` keys from `<home>/.env` with `sed`, so they load nothing
else from the file. This prints nothing when the scope is right:

```sh
( t=$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID="$(sed -n 's/^FACTORY_INFISICAL_CLIENT_ID=//p' <home>/.env)" \
    INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET="$(sed -n 's/^FACTORY_INFISICAL_CLIENT_SECRET=//p' <home>/.env)" \
    infisical login --method=universal-auth --silent --plain)
  for env in <each other environment slug>; do
    INFISICAL_TOKEN="$t" infisical export --silent --projectId <project id> --env "$env" --path / --format dotenv >/dev/null 2>&1 \
      && echo "readable: $env"
  done )
```

Each `readable:` line fails the check. Remove the extra grant, or create a new
identity for the factory. Reuse its client ID only after the check passes, and
give this machine its own client secret.

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
  --path <folder> --format json |
  python3 -c 'import json, sys; [print(s["key"]) for s in json.load(sys.stdin)]'
```

## Configure both homes

The helper is a script at `<home>/bin/factory-secrets`. Both homes run the
same script. It signs in as the machine identity, then exports the folder.
When sign-in fails, it exports nothing, so it never falls back to the
operator's own Infisical login. Add `--domain <api url>` to both `infisical`
calls when the operator does not use US Cloud.

The script guards against a slow or failed run:

- It sets `INFISICAL_DISABLE_UPDATE_CHECK=true`. The Infisical CLI's automatic
  update check can hang, and a hung check made a real install miss the helper
  timeout.
- It stops each `infisical` call after 15 seconds.
- It retries a failed or empty attempt, up to 3 attempts, 2 seconds apart.
- It prints the export only after an attempt succeeds with output. On failure
  it prints nothing to stdout and exits `1`, so Hermes never reads partial
  output.

The commands below write the script to `<home>/bin/factory-secrets.new`
first, so they never overwrite a helper that an operator has tuned for this
machine:

```sh
mkdir -p <home>/bin
cat > <home>/bin/factory-secrets.new <<'SH'
#!/bin/sh
export INFISICAL_DISABLE_UPDATE_CHECK=true
bounded() { perl -e 'alarm shift; exec @ARGV or exit 127' 15 "$@"; }
attempt() {
  t="$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID="$FACTORY_INFISICAL_CLIENT_ID" \
    INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET="$FACTORY_INFISICAL_CLIENT_SECRET" \
    bounded infisical login --method=universal-auth --silent --plain)" && [ -n "$t" ] || return 1
  INFISICAL_TOKEN="$t" bounded infisical export --silent --projectId=<project id> \
    --env=<environment> --path=<folder> --format=dotenv
}
for n in 1 2 3; do
  if out="$(attempt)" && [ -n "$out" ]; then
    printf '%s\n' "$out"
    exit 0
  fi
  [ "$n" = 3 ] || sleep 2
done
echo "factory-secrets: Infisical export failed after 3 attempts" >&2
exit 1
SH
chmod 700 <home>/bin/factory-secrets.new
if [ ! -e <home>/bin/factory-secrets ]; then
  mv <home>/bin/factory-secrets.new <home>/bin/factory-secrets
elif cmp -s <home>/bin/factory-secrets <home>/bin/factory-secrets.new; then
  echo same
  rm <home>/bin/factory-secrets.new
else
  echo differs
fi
```

Never print the contents of either file or a `diff` between them. A helper
tuned on this machine can hold a secret inline. When the command prints
`differs`, keep both files and stop.

> **HUMAN CHECKPOINT.** Ask the operator to run
> `diff -u <home>/bin/factory-secrets <home>/bin/factory-secrets.new` in
> their own terminal, review it there, and say whether to replace the
> existing helper. On approval, run
> `mv <home>/bin/factory-secrets.new <home>/bin/factory-secrets`. Otherwise
> run `rm <home>/bin/factory-secrets.new` and keep the existing helper. Then
> confirm that `<home>/bin/factory-secrets.new` is gone and that
> `<home>/bin/factory-secrets` exists with mode `700`.

Then point both homes at the helper:

```sh
value="$(python3 -c 'import json, sys; print(json.dumps({"enabled": True, "helper_timeout_seconds": 100, "command": sys.argv[1]}))' "<home>/bin/factory-secrets")"
hermes config set secrets.command "$value" </dev/null
hermes -p claude-worker config set secrets.command "$value" </dev/null
hermes -p claude-worker config set terminal.env_passthrough '["<token variable>"]' </dev/null
```

A healthy run takes about two seconds. The worst case is 3 attempts of two
15-second calls, plus two 2-second pauses, which is 94 seconds. The command
sets `helper_timeout_seconds` to 100 to cover it. The Hermes default of 3
seconds is too short.

The 15-second bound stops only the `infisical` process itself. If a hung
`infisical` starts a child process that keeps its stdout open, the attempt
waits for that child, so one attempt can run past 15 seconds. The
`helper_timeout_seconds` of 100 still stops the whole helper. Hermes then
starts with no secrets, and the forged-signature check below catches it. Keep
`helper_timeout_seconds` above the 94-second worst case when you change the
bound, the attempts, or the pause.

Hermes discards the helper's stderr. To see why a run fails, run the script
with the two `FACTORY_INFISICAL_` values from `.env`. These lines read only
those two keys with `sed` and pass them in the script's environment, never as
command arguments, which other users can see in the process list. Send stdout
to `/dev/null` so no secret prints. Exit `0` means the helper works:

```sh
FACTORY_INFISICAL_CLIENT_ID="$(sed -n 's/^FACTORY_INFISICAL_CLIENT_ID=//p' <home>/.env)" \
FACTORY_INFISICAL_CLIENT_SECRET="$(sed -n 's/^FACTORY_INFISICAL_CLIENT_SECRET=//p' <home>/.env)" \
  <home>/bin/factory-secrets >/dev/null; echo "exit $?"
```

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

When the helper fails at startup, Hermes still starts. It logs
`Command helper: helper command failed` and applies no secrets. Each
`${...}` reference in `config.yaml` then stays unexpanded, and the webhook
routes use the literal strings `${LINEAR_WEBHOOK_SECRET}` and
`${GITHUB_WEBHOOK_SECRET}` as their signing secrets. Those strings are public,
so anyone can sign a delivery that the routes accept. The forged-signature
lines in part 15's local checks catch this. Run them after every gateway
restart, not only during setup. If either prints anything but `401`, follow
the HUMAN CHECKPOINT in part 15 to stop the gateway, run the helper by hand as
shown above, and fix it before the operator starts the gateway again.

A startup guard that refuses a route whose secret is still a `${...}`
reference is not yet enforced in code. Until it is, the forged-signature check
is the only protection.

## Rotation

Hermes reads Infisical once, when each process starts. After a secret changes
in Infisical, restart the gateway. Kanban workers start as new processes and
read the new value on their next run.

A Universal Auth client secret can have an expiry. When it does, record the
date where the operator tracks credential expiry. An expired client secret
leaves every moved value unset at the next start.
