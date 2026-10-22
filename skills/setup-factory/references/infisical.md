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
| `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` | `<home>` |
| `LINEAR_WEBHOOK_SECRET`, `GITHUB_WEBHOOK_SECRET` | `<home>` |
| `CLAUDE_PROXY_TOKEN`, the variable that `claude_session.auth_token_env` names | `<home>` |
| Each variable that `claude_session.mcp_servers.<name>.token_env` names, such as `SENTRY_ACCESS_TOKEN` | `<home>` |

The default home loads the whole folder. The worker home loads nothing from
Infisical and has no machine identity. It holds no factory secret of its own.
The gateway's broker gives it a Linear access token on request, and
`<worker>/scripts/worker-secrets` asks the broker for the proxy token when a
worker process starts, as part 5 of the skill sets up. A worker never holds an
Infisical identity, the Linear client secret, a webhook secret, or a Slack
token, so nothing it runs can read them.

These values stay in `.env`:

- `FACTORY_INFISICAL_CLIENT_ID` and `FACTORY_INFISICAL_CLIENT_SECRET`, the
  credential of the machine identity that the default home's helper signs in
  as, in `<home>/.env` only.
- Settings that are not secrets: `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` and
  `GH_CONFIG_DIR`.

The GitHub App private key stays a file at `github.private_key_path`, or, with
the broker user, in the broker's home.

## Inputs

Ask the operator for these. None of them is a secret.

| Input | Example |
| --- | --- |
| Infisical project ID | `<project id>` |
| Environment slug | `development` |
| Folder for the factory's secrets | `/factory` |
| The machine identity the default home signs in as | `<identity name>` |
| Infisical URL: `https://app.infisical.com` for US Cloud | `https://eu.infisical.com` |

## Check

All of these must hold:

- `env_has <home>/.env FACTORY_INFISICAL_CLIENT_ID` and
  `env_has <home>/.env FACTORY_INFISICAL_CLIENT_SECRET` print `1`, and the
  same lines for `<worker>/.env` print `0`.
- `hermes config get secrets.command --json` shows `"enabled": true` and the
  command from [Configure the default home](#configure-the-default-home). When
  the factory uses [1Password](1password.md), the helper also carries that
  part's filter.
- `hermes config get secrets.command --json` prints
  `Command helper: applied <n> secrets` on its first line, where `<n>` is the
  number of values in the folder. When the folder holds
  `OP_SERVICE_ACCOUNT_TOKEN` for [1Password](1password.md), `<n>` is the number
  of values minus one.
- `<home>/bin/factory-secrets` exists, and the `cmp -s` check in
  [Configure the default home](#configure-the-default-home) prints `same`, or
  the operator approved the differences there. When the factory uses
  [1Password](1password.md), the only other change is that part's filter.
- No moved value remains in a `.env` file: `env_has` prints `0` for each value
  in the first table, in both files.
- The machine identity reads one environment and nothing else, as
  [Check the identity's scope](#check-the-identitys-scope) shows.
- `HERMES_HOME=<worker> python3 <worker>/scripts/check-install` prints no
  `secret <name> in <worker>/.env` line and no `config secrets.command` line.
  A worker set up before the worker gave up its identity needs
  [Retire the worker's identity](#retire-the-workers-identity).

If all of them hold, skip to [Verify](#verify).

## Create the machine identity

The default home signs in as one machine identity through Universal Auth,
which reads the factory's environment. Infisical adds permissions together,
so a role such as `Viewer` would widen any narrower grant.

> **HUMAN CHECKPOINT.** Ask the operator to create an identity, or reuse an
> existing one, in **Organization → Access Control → Identities**. In the
> project, set these values for `<identity name>`:
>
> - **Project role:** `No Access`.
> - **Additional privilege:** permanent, on subject `secrets`, with the actions
>   `describeSecret` and `readValue`, and the condition `environment` equals
>   `<environment>`.
> - **Authentication:** Universal Auth.
>
> Then ask the operator to create a Universal Auth client secret for the
> identity, and store its client ID and client secret with the `read -rs`
> line from the skill, once per key, into `<home>/.env` only, under the names
> `FACTORY_INFISICAL_CLIENT_ID` and `FACTORY_INFISICAL_CLIENT_SECRET`.

Use the `FACTORY_INFISICAL_` names, not `INFISICAL_UNIVERSAL_AUTH_CLIENT_ID`
and `INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET`. A repository's own Infisical
tooling can change how it signs in when it sees the Universal Auth names.

The identity is in no environment the worker or Claude Code holds. The
`factory-secret-names` plugin declares `FACTORY_INFISICAL_CLIENT_SECRET`, so
Hermes strips it from every process the gateway starts. Both homes run as the
same user, so a file read of `<home>/.env` is not prevented; keep the identity
scoped to one environment for that reason.

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
lists its environment slugs. Render
[`scripts/identity-secrets`](../scripts/identity-secrets) into
`<home>/bin/identity-secrets` and source it in the operator's shell. It
defines `identity_keys`, which signs in as the identity in an env file, reads
one folder, and prints the names of the secrets it can read, and
`identity_value`, which prints one secret's value. Both make the same REST
calls as the helper in "Configure the default home", so the identity never
sits in the environment or arguments of a process that another process of the
same user can read with `ps eww`: only `/usr/bin/curl` and `/usr/bin/perl`
receive it, on standard input, and macOS hides the environment of those
platform binaries. Each function runs in a subshell, so no variable of it
outlives the call, whether it succeeds or fails. They read only the two
`FACTORY_INFISICAL_` keys of the file they name. `identity_keys` prints no
value. Only the 1Password wrapper in [`1password.md`](1password.md) calls
`identity_value`.

```sh
mkdir -p <home>/bin
sed -e 's|<api url>|<infisical url>|g' -e 's|<project id>|<project id>|g' \
  <repo>/skills/setup-factory/scripts/identity-secrets > <home>/bin/identity-secrets
grep -c '<[a-z ]*>' <home>/bin/identity-secrets   # 0: every placeholder is rendered
. <home>/bin/identity-secrets
```

It returns `2` and prints `identity_keys: sign-in failed` when sign-in fails,
and returns `1` and prints nothing when the identity cannot read the folder.
A sign-in failure is not a pass, so leave stderr on. This prints nothing when
the scope is right:

```sh
for env in <each other environment slug>; do
  identity_keys <home>/.env "$env" / >/dev/null && echo "readable: $env"
done
```

Each `readable:` line fails the check. Remove the extra grant, or create a new
identity for the factory. Reuse its client ID only after the check passes, and
give this machine its own client secret.

## Broker identity

Run this when the broker runs as its own macOS user, as "Broker user, macOS"
in part 5 of the skill sets up. The broker then signs in as a second machine
identity, whose credential lives only in the broker user's home. The secrets
that only the broker needs move to a subfolder that the default home's
identity cannot read:

| Value | Folder | Read by |
| --- | --- | --- |
| `GITHUB_UPLOAD_TOKEN`, `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` | `<folder>/broker` | The broker |
| The proxy token and the Sentry token | `<folder>` | The default home and the broker |

The broker reads `<folder>` first and `<folder>/broker` second. The grant
below keeps the default home's identity out of the subfolder.

> **HUMAN CHECKPOINT.** Ask the operator to do these steps in Infisical:
>
> 1. Create the folder `<folder>/broker` in `<environment>`, and move
>    `GITHUB_UPLOAD_TOKEN`, `LINEAR_CLIENT_ID`, and `LINEAR_CLIENT_SECRET`
>    into it.
> 2. Narrow the default home's identity: change the condition of its
>    additional privilege to `environment` equals `<environment>` and
>    `secretPath` equals `<folder>`.
> 3. Create an identity for the broker, as in
>    [Create the machine identity](#create-the-machine-identity), with the
>    condition `environment` equals `<environment>` and `secretPath` glob
>    `<folder>/**`, and a Universal Auth client secret.
> 4. Enter its client ID and secret in their own terminal with
>    the `set-secret` lines of step 5 in "Broker user, macOS"
>    Never into `<home>/.env`.

Check it from the factory user's shell, with `identity_keys` from
[Check the identity's scope](#check-the-identitys-scope). The default home's
identity must read none of the moved values. This prints only names, and
prints nothing when the scope is right. A `sign-in failed` line means the
check proved nothing:

```sh
identity_keys <home>/.env <environment> <folder>/broker
```

`check-install` then reports `upload broker` lines when the broker holds
no upload token or no Linear app credentials.

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

## Configure the default home

The default home's helper is a script at `<home>/bin/factory-secrets`,
rendered from
[`scripts/factory-secrets`](../scripts/factory-secrets) in this skill. It
signs in as the machine identity through Infisical's Universal Auth login
endpoint, then reads the folder from the `GET /api/v4/secrets` endpoint. When
sign-in fails, it reads nothing, so it never falls back to the operator's own
Infisical login. It prints the same `KEY='value'` lines as
`infisical export --format=dotenv`.

The helper does not run the Infisical CLI. The CLI accepts the identity only
through environment variables or arguments, and while it runs, any process of
the same user can read them with `ps eww`. The helper runs only
`/usr/bin/curl` and `/usr/bin/perl`. macOS hides the environment of these
platform binaries from other processes. The helper also removes the two
`FACTORY_INFISICAL_` variables from its environment before it starts them,
keeps the identity and the access token in shell variables, and sends both to
`curl` on standard input, never as arguments.

The script prints secrets only to Hermes. It refuses any argument, so a
`--help` run prints nothing, and it refuses to run when its standard output is
a terminal. Both refusals exit `2`. Hermes reads it through a pipe, and so does
the diagnostic run below, which sends stdout to `/dev/null`.

The script guards against a slow or failed run:

- It stops each request after 15 seconds.
- It retries a failed or empty attempt, up to 3 attempts, 2 seconds apart.
- It prints the export only after an attempt succeeds with output. On failure
  it prints nothing to stdout and exits `1`, so Hermes never reads partial
  output.
- It appends one line to `<home>/logs/factory-secrets.log`, mode `600`, for
  each failed attempt and for the attempt that succeeds. A line holds the
  time, the failed step (`login`, `export`, or `credentials`), the attempt
  number, and an exit code, and never a value. The exit code is curl's, such
  as `6` for a name that did not resolve, `7` for a refused connection, `22`
  for an HTTP error, and `28` for a timeout. It is `65` when a response is not
  the expected JSON. Hermes discards the helper's stderr, so this log is the
  record of why a gateway started without its secrets. The helper cuts the file
  to its last 200 lines once it passes 64 KB, and a log it cannot write never
  fails the helper.
- It prints a message without secret values to stderr for each failed
  attempt, such as `curl: (22) The requested URL returned error: 401`, and
  exits `1` before any request when either `FACTORY_INFISICAL_` variable is
  unset.

The commands below render the template into `<home>/bin/factory-secrets.new`
first, so they never overwrite a helper that an operator has tuned for this
machine:

```sh
mkdir -p <home>/bin
sed -e 's|<api url>|<infisical url>|g' -e 's|<project id>|<project id>|g' \
  -e 's|<environment>|<environment>|g' -e 's|<folder>|<folder>|g' \
  -e 's|<log file>|<home>/logs/factory-secrets.log|g' \
  <repo>/skills/setup-factory/scripts/factory-secrets > <home>/bin/factory-secrets.new
grep -c '<[a-z ]*>' <home>/bin/factory-secrets.new   # 0: every placeholder is rendered
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

Then point the default home at the helper. The worker home's
`secrets.command` stays the `worker-secrets` command from part 5, which asks
the gateway for the proxy token and never reads Infisical:

```sh
value="$(python3 -c 'import json, sys; print(json.dumps({"enabled": True, "helper_timeout_seconds": 100, "command": sys.argv[1]}))' "<home>/bin/factory-secrets")"
hermes config set secrets.command "$value" </dev/null
```

A healthy run takes about two seconds. The worst case is 3 attempts of two
15-second calls, plus two 2-second pauses, which is 94 seconds. The command
sets `helper_timeout_seconds` to 100 to cover it. The Hermes default of 3
seconds is too short.

If the helper still runs when `helper_timeout_seconds` ends, Hermes stops it
and starts with no secrets, and the forged-signature check below catches it.
Keep `helper_timeout_seconds` above the 94-second worst case when you change
the bound, the attempts, or the pause.

Hermes discards the helper's stderr, so read `<home>/logs/factory-secrets.log`
first: its last lines name the step, attempt, and exit code of each failure.
A cause the log does not explain needs a run of the script. Run it
with the two `FACTORY_INFISICAL_` values from `.env`. These lines read only
those two keys with `sed` and pass them in the script's environment, never as
command arguments, which other users can see in the process list. Send stdout
to `/dev/null` so no secret prints. Exit `0` means the helper works:

```sh
FACTORY_INFISICAL_CLIENT_ID="$(sed -n 's/^FACTORY_INFISICAL_CLIENT_ID=//p' <home>/.env)" \
FACTORY_INFISICAL_CLIENT_SECRET="$(sed -n 's/^FACTORY_INFISICAL_CLIENT_SECRET=//p' <home>/.env)" \
  <home>/bin/factory-secrets >/dev/null; echo "exit $?"
```

The gateway needs no Infisical CLI on its `PATH`. The helper runs only
`/usr/bin/curl` and `/usr/bin/perl`.

## Retire the worker's identity

A worker set up before it gave up its own Infisical identity still holds one.
`check-install` reports it as a `secret FACTORY_INFISICAL_CLIENT_SECRET in
<worker>/.env` line, and reports `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET`,
the proxy token variable, and each MCP server token variable the same way. Retire it after the gateway runs
the broker from part 5, so the worker has a source for its tokens:

1. Remove each reported key from `<worker>/.env`. Back up the file first,
   with mode `600`. The backup holds the identity, so delete it after step 4
   passes. `umask 077` keeps the temporary file private while it exists.
   This prints nothing:

   ```sh
   umask 077
   for key in FACTORY_INFISICAL_CLIENT_ID FACTORY_INFISICAL_CLIENT_SECRET LINEAR_CLIENT_ID LINEAR_CLIENT_SECRET CLAUDE_PROXY_TOKEN SENTRY_ACCESS_TOKEN; do
     grep -v "^${key}=" <worker>/.env > <worker>/.env.tmp; mv <worker>/.env.tmp <worker>/.env; chmod 600 <worker>/.env
   done
   ```

2. Set the worker's `secrets.command` to the part 5 command, and clear the
   passthrough list an earlier setup gave the worker:

   ```sh
   hermes -p claude-worker config set terminal.env_passthrough '[]' </dev/null
   ```

3. > **HUMAN CHECKPOINT.** Ask the operator to delete the worker's machine
   > identity in **Organization → Access Control → Identities**, or to revoke
   > its Universal Auth client secrets. Nothing on the machine uses it any
   > more.

4. Run `check-install` again. It prints no `secret` line for `<worker>/.env`.
   Then delete the backup from step 1.

## Verify

Verify is an operator-only step. When `<home>/.env` does not set a key,
the check runs the `secrets.command` helper, and agents never run a secrets
helper by hand. The script refuses to run without a terminal.

[`scripts/check-secret-match`](../scripts/check-secret-match) compares the
folder's value with the value that the gateway loads for the key. Hermes keeps
a non-empty value that `.env` sets, so when `<home>/.env` sets the key, the script
compares that value. Otherwise it compares the value that the helper prints.
It also checks that the route's `secret` in `config.yaml` still reads
`${<key>}`. Each line prints `1` when the values match and `0` when they do
not. The values stay inside the script, and it never runs
`hermes config get --raw`.

> **HUMAN CHECKPOINT.** Before you remove anything from `.env`, ask the
> operator to run these lines in their own terminal and to report what each
> one prints:
>
> ```sh
> match() {
>   python3 <repo>/skills/setup-factory/scripts/check-secret-match <home>/.env <project id> <environment> <folder> "$1" "$2"
> }
> match LINEAR_WEBHOOK_SECRET <webhook_route>
> match GITHUB_WEBHOOK_SECRET github
> ```
>
> Continue only when both print `1`. A `0` means that the gateway uses a
> value today that differs from the folder's. Stop and report the key to the
> operator.

Then remove each moved value from `.env`. Back up both files first:

```sh
for key in <each key from the first table>; do
  for file in <home>/.env <worker>/.env; do
    grep -v "^${key}=" "$file" > "$file.tmp"; mv "$file.tmp" "$file"; chmod 600 "$file"
  done
done
```

> **HUMAN CHECKPOINT.** Ask the operator to run the `match` lines again in
> their own terminal. They must still print `1`, which shows that the values
> now come from Infisical and not from `.env`.

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

The `linear-agent-session` plugin drops a delivery on any route whose secret
is still a `${...}` reference before any agent run, and logs the route and the
variable name. It does not fix the secret source, so the forged-signature
check and the restart still apply.

## Rotation

Hermes reads Infisical once, when each process starts. After a secret changes
in Infisical, restart the gateway. Kanban workers start as new processes and
read the new value on their next run.

A Universal Auth client secret can have an expiry. When it does, record the
date where the operator tracks credential expiry. An expired client secret
leaves every moved value unset at the next start.
