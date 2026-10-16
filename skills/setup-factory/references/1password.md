# 1Password for operator-only credentials

This part is optional, and it needs the
[Infisical secret source](infisical.md). Run it when the operator keeps
credentials in [1Password](https://1password.com) that they need while they
configure the factory, such as an Apple ID for Xcode.

These credentials are for the operator only. Hermes agents, both the gateway
and the worker, never receive them. The design has four pieces:

- A 1Password service account reads one dedicated vault. A service account is
  "not associated with an individual", so it needs no member seat
  ([service accounts](https://www.1password.dev/service-accounts/)).
- Its token, `OP_SERVICE_ACCOUNT_TOKEN`, lives in the factory's Infisical
  folder. It is never in a plaintext `.env` file.
- The Infisical command helper that feeds Hermes removes
  `OP_SERVICE_ACCOUNT_TOKEN` from its output.
- An `op` wrapper gives the token to the operator's interactive shells. It
  signs in to Infisical as the machine identity, reads that one secret, and
  runs the real `op`.

The strip keeps the token out of the environment that Hermes gives its agents.
The worker home reads nothing from Infisical: its helper,
`<worker>/scripts/worker-secrets`, gets only the proxy token from the
gateway's broker, as [`infisical.md`](infisical.md) explains. The default
home's identity can read the whole folder, and both homes run as one user, so
the strip is not a hard boundary there. Keep the vault small, read-only, and limited to what
the operator needs.

## Inputs

Ask the operator for these. None of them is a secret.

| Input | Example |
| --- | --- |
| The vault the service account reads | `<vault>` |
| A name for the service account | `<service account name>` |
| The Infisical project ID, environment, and folder from [`infisical.md`](infisical.md) | `<project id>`, `<environment>`, `<folder>` |
| The directory that holds the tools setup installed | `<bin dir>`, for example `~/.local/bin` |
| A directory for the wrapper that is not on the gateway's `PATH` | `<wrapper dir>`, for example `~/.local/libexec/op-wrapper` |
| The owner-only files that hold the machine identity's client ID and client secret | `<home>/.env` by default, or separate mode `0600` files such as `<id file>` and `<secret file>` |

## Check

Run these in a new operator login shell. All of them must hold:

- `command -v op` prints `<wrapper dir>/op`. With the shell-function
  alternative from [Install the op wrapper](#install-the-op-wrapper),
  `type op` reports a function instead.
- `ls -l` on each file that holds the machine identity's credentials shows
  `-rw-------`, owned by the operator.
- `op whoami` exits `0`.
- `op user get --me | grep -c '^Type: *SERVICE_ACCOUNT'` prints `1`.
- `op vault list --format json | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))'`
  prints `1`, and `op vault list` names `<vault>`.
- The Hermes helper output has no token line. The `helper_count` lines in
  [Verify](#verify) each print `token=0` with `keys=` greater than `0`, for
  both homes.
- `env_has <home>/.env OP_SERVICE_ACCOUNT_TOKEN` and
  `env_has <worker>/.env OP_SERVICE_ACCOUNT_TOKEN` each print `0`.

If all of them hold, skip to [Verify](#verify).

## Install 1Password CLI

Install the official `op` binary. On macOS, the Homebrew cask
`1password-cli` works when system security policy allows it, and `<bin dir>`
is then Homebrew's `bin` directory. When policy blocks the cask, use the
direct download into `<bin dir>`.

Download the latest release for the machine from the
[1Password CLI release page](https://app-updates.agilebits.com/product_history/CLI2).
It offers a ZIP archive for macOS and for Linux
([get started](https://www.1password.dev/cli/get-started/)). Unzip it into a new,
empty directory, verify it as that page describes, and move the `op` binary to
`<bin dir>`. On Linux the check is `gpg --verify op.sig op`.

Check with `<bin dir>/op --version`.

## Create the vault and the service account

Create the vault first. A service account's vault access is immutable. To
change it, you create a new service account
([get started](https://www.1password.dev/service-accounts/get-started/)).
A service account cannot read the built-in Personal, Private, or Employee
vault, or the default Shared vault (same page). In Teams and Business
accounts, only owners and administrators can create service accounts by
default
([manage](https://www.1password.dev/service-accounts/manage-service-accounts/)).

> **HUMAN CHECKPOINT.** Ask the operator to do these in the 1Password web app:
>
> 1. Open the account's billing page and note the number of seats in use.
> 2. Create the vault `<vault>`, and move only the operator-only credentials
>    into it.
> 3. Create the service account `<service account name>`. Give it access to
>    `<vault>` only, with the `read_items` permission only. Do not allow it to
>    create vaults.
> 4. 1Password shows the token once. Store it in the Infisical folder with the
>    `read -rs` line from [Store the secrets](infisical.md#store-the-secrets),
>    with `<KEY>` set to `OP_SERVICE_ACCOUNT_TOKEN`. Do not paste it into chat,
>    a file, or a `.env` file.

Then confirm the cost.

> **HUMAN CHECKPOINT.** Ask the operator to open the billing page again and
> confirm that the service account added no seat. The number of seats in use
> must match the number from step 1, and the monthly total must not change.
> 1Password includes service accounts with the subscription
> ([Secrets Automation](https://www.1password.dev/secrets-automation/)), but no
> 1Password page states outright that one uses no seat. When the count went
> up, stop and ask the operator whether to delete the service account.

Check the folder by key names only:

```sh
infisical export --silent --projectId <project id> --env <environment> \
  --path <folder> --format dotenv | grep -c '^OP_SERVICE_ACCOUNT_TOKEN='   # 1
```

## Strip the token from the Hermes helper

Both homes run the helper script from the Infisical part,
`<home>/bin/factory-secrets`, as described in
[Configure both homes](infisical.md#configure-both-homes). Do not replace it
with an inline command. Add one filter to the script instead.

In `<home>/bin/factory-secrets`, find the line that prints the export after a
successful attempt:

```sh
    printf '%s\n' "$out"
```

Change it so the output passes through the filter:

```sh
    printf '%s\n' "$out" | sed '/^OP_SERVICE_ACCOUNT_TOKEN=/d'
```

The script still checks that the export is not empty before it prints, so a
failed export still fails the helper. The worker's helper,
`<worker>/scripts/worker-secrets`, needs no change: it reads no Infisical
folder and prints only the proxy token it gets from the gateway's broker.

Hermes now reports one fewer secret than the folder holds:
`Command helper: applied <n> secrets`, where `<n>` is the folder's count minus
one.

## Install the op wrapper

The wrapper is a script that supplies the token to the real `op`. Two ways to
reach it work:

- **Recommended: the wrapper on `PATH` as `op`, ahead of the real binary.**
  It covers every shell and every script the operator runs, because each one
  finds `op` on `PATH`.
- **Acceptable: a login-shell function named `op` that calls the wrapper.**
  It covers only the shells that load the file that defines it. Scripts in
  `sh` or `bash`, and shells of another type, run the real `op` with no
  token.

Either way, the wrapper reads the machine identity's credentials from files
that only the operator can read, mode `0600`. Never export them as shell
environment variables, which every process the shell starts inherits.

The wrapper does these things:

- It reads the client ID and client secret from their owner-only files
  without sourcing them, and passes them only to the `infisical login` call.
- It signs in to Infisical as the machine identity, the same way the Hermes
  helper does.
- It reads only `OP_SERVICE_ACCOUNT_TOKEN`, exports it, and runs the real
  `op` with the same arguments.
- It turns off the Infisical CLI's update check, which can hang.
- Each Infisical call stops after 20 seconds.
- It never prints the token. On failure it prints one line to standard error
  and exits `1`.

Write this to `<wrapper dir>/op`, with mode `0755`. It holds no secret. Add
`--domain <api url>` to both `infisical` calls when the operator does not use
US Cloud.

```sh
#!/bin/sh
set -u
export INFISICAL_DISABLE_UPDATE_CHECK=true
real_op='<bin dir>/op'

with_timeout() { perl -e 'alarm shift; exec @ARGV or die "exec failed\n"' 20 "$@"; }
client_id() { sed -n 's/^FACTORY_INFISICAL_CLIENT_ID=//p' '<home>/.env' | head -n 1; }
client_secret() { sed -n 's/^FACTORY_INFISICAL_CLIENT_SECRET=//p' '<home>/.env' | head -n 1; }
fail() { echo "op wrapper: $1" >&2; exit 1; }

t=$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID="$(client_id)" \
  INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET="$(client_secret)" \
  with_timeout infisical login --method=universal-auth --silent --plain 2>/dev/null) \
  && [ -n "$t" ] || fail "Infisical sign-in failed"

token=$(INFISICAL_TOKEN="$t" with_timeout infisical secrets get OP_SERVICE_ACCOUNT_TOKEN \
  --projectId='<project id>' --env='<environment>' --path='<folder>' \
  --plain --silent 2>/dev/null) \
  && [ -n "$token" ] || fail "could not read OP_SERVICE_ACCOUNT_TOKEN"

OP_SERVICE_ACCOUNT_TOKEN=$token
export OP_SERVICE_ACCOUNT_TOKEN
unset t token
exec "$real_op" "$@"
```

`perl` keeps the timer through `exec`, so it works where `timeout` is not
installed, as on macOS.

When the credentials live in their own files, such as under
`~/.config/infisical/`, change `client_id` and `client_secret` to read those
files, for example `head -n 1 '<id file>'`. Keep each file at mode `0600`.

For the recommended setup, put `<wrapper dir>` ahead of `<bin dir>` on `PATH`
in the login file of each shell the operator uses, such as `~/.zprofile` and
`~/.bash_profile`:

```sh
export PATH="<wrapper dir>:$PATH"
```

For the function alternative, save the wrapper under another name, such as
`<wrapper dir>/op-with-token`, and set `real_op` to the real binary's full
path. Then define the function in the shell's startup file:

```sh
op() { '<wrapper dir>/op-with-token' "$@"; }
```

Pick the startup file with care. `~/.zshenv` loads in every `zsh`, including
any `zsh` that Hermes agents start, so they can call the function too. The
agent instructions forbid it, but a file that only interactive shells load,
such as `~/.zshrc`, keeps the function out of their shells.

Do not add `<wrapper dir>` to the `PATH` that the gateway's service definition
sets. Hermes agents then run the real `op`, with no token.

## Verify

Open a new login shell, so that it reads the new `PATH`. Then run the checks
from [Check](#check).

`helper_count` reads the helper command that a Hermes home is configured
with, runs it with the machine identity from that home's `.env`, and counts
the token lines. It reads the two `FACTORY_INFISICAL_` keys with `sed`, so it
loads nothing else from the file, and it never prints a value:

```sh
helper_count() {
  env_file=$1; shift
  cmd="$(hermes "$@" config get secrets.command --json 2>/dev/null </dev/null |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["command"])')"
  [ -n "$cmd" ] || { echo "no secrets.command" >&2; return 1; }
  out="$(FACTORY_INFISICAL_CLIENT_ID="$(sed -n 's/^FACTORY_INFISICAL_CLIENT_ID=//p' "$env_file")" \
    FACTORY_INFISICAL_CLIENT_SECRET="$(sed -n 's/^FACTORY_INFISICAL_CLIENT_SECRET=//p' "$env_file")" \
    sh -c "$cmd")" || { echo "helper failed" >&2; return 1; }
  keys="$(printf '%s\n' "$out" | grep -c '^[A-Za-z_][A-Za-z0-9_]*=')"
  [ "$keys" -gt 0 ] || { echo "helper printed no KEY= lines" >&2; return 1; }
  echo "keys=$keys token=$(printf '%s\n' "$out" | grep -c '^OP_SERVICE_ACCOUNT_TOKEN=')"
}
helper_count <home>/.env                     # keys=<n> token=0
helper_count <worker>/.env -p claude-worker  # keys=3 token=0
```

The check passes only when each line prints `token=0` and `keys=` is greater
than `0`. When the helper exits nonzero or prints no `KEY=` line, the function
prints one error line and returns `1`, so a broken helper cannot pass as
`token=0`. `<n>` is the folder's count minus one. The worker's line prints
`keys=3`, or `keys=2` when the proxy needs no token.

Also confirm that the gateway's `PATH` does not contain `<wrapper dir>`.

## Rate limits

1Password limits each service account token to 1,000 reads per hour on
1Password, Families, and Teams accounts, and 10,000 on Business. All service
accounts in one 1Password account share a daily limit of 1,000 requests on
1Password and Families, 5,000 on Teams, and 50,000 on Business. Some `op`
commands make more than one request
([rate limits](https://www.1password.dev/service-accounts/rate-limits/)).

Hermes never reads 1Password in this design, so the factory's own runs use
none of these requests. Only the operator's `op` commands count, and those
stay far below the limits, so the factory does not need a Connect server.
To see the current usage, run `op service-account ratelimit`.

## Rotation

Rotate the token from the service account's page in 1Password. Rotation issues
a new token with the same permissions. The old token can expire now, in 1
hour, in 3 days, or at a chosen time
([manage](https://www.1password.dev/service-accounts/manage-service-accounts/)).

1. Rotate with a grace period, such as 1 hour.
2. Store the new token in the Infisical folder with the same `read -rs` line.
3. Run `op whoami` in a new shell. The wrapper reads the new token on each run,
   so no restart is needed. Hermes never loads the token, so the gateway
   needs no restart either.

## Revocation

If the token leaks, revoke it from the service account's page. Revocation
removes access immediately
([manage](https://www.1password.dev/service-accounts/manage-service-accounts/)).
Then create a new service account as in
[Create the vault and the service account](#create-the-vault-and-the-service-account),
and replace the token in the Infisical folder.

## Audit

The service account's item usage report shows which vaults and items it read,
when, and the action
([manage](https://www.1password.dev/service-accounts/manage-service-accounts/)).
Review it when you rotate the token.
