---
name: setup-factory
description: Set up or re-check a Hermes software factory on a machine from this repository. Installs and verifies Hermes, the Claude Code CLI and transport, the factory plugins and claude-worker profile, the Linear agent app, the factory's GitHub App and its webhook, the webhook routes, Slack, an optional Infisical secret source, optional 1Password access for operator-only credentials, Cloudflare Tunnel and Access ingress through the operator's SST app, and the machine-wide agent instructions. Use for "set up a factory", "install the factory on this machine", "re-run factory setup", or checking that an existing factory is configured correctly.
---

# Set up a factory

This skill is a runbook for you, the agent. You run every scriptable step
yourself. You stop only at a **HUMAN CHECKPOINT**, where the operator must act
in a browser, approve an installation, or type a secret into their own
terminal. After each part, you run its checks and continue only when they
pass.

## Rules

- **Check before you change.** Every part starts with a check. If the check
  passes, report the part as already configured and skip to the next part. A
  second run on a configured machine changes nothing.
- **No real values in this repository.** Ask the operator for each value, or
  discover it from the machine and the connected accounts. Never write a
  factory value into this repository, a commit, or an issue.
- **Secrets are created, not pasted.** Generate each secret on the machine, or
  have the operator type it into their own terminal. Never ask for a secret in
  chat. Never print, log, or commit one. Check that a secret exists with a
  count, not by reading it.
- **Never edit Hermes source.** If the Hermes install has local commits or a
  dirty tree, stop and report it. Do not repair it.
- **Privileged steps run in the operator's terminal.** You cannot answer a
  `sudo` password prompt. Put every privileged command for a part into one
  script in your scratch directory. Make the script refuse any change that
  could lock the operator out, then ask the operator to run it with `sudo`
  in their own terminal, and wait. The prompt times out after about five
  minutes. Then run the part's check again.
- **Run Hermes commands without a prompt.** Pass `-y` or `--yes-deps` where the
  command takes one, and redirect standard input from `/dev/null`.
- **Keep `check-install` in step with the runbook.** A part that sets a
  `config.yaml` key, creates a cron job, or installs a launchd service also has
  a row in the `CONFIG`, `CRON`, or `SERVICES` table of
  `profiles/claude-worker/scripts/check-install`. Change the part and its row
  in the same change.

A HUMAN CHECKPOINT looks like this in the parts below:

> **HUMAN CHECKPOINT.** What the operator does, and what you check afterwards.

At a checkpoint, tell the operator exactly what to do, wait for them to say it
is done, then run the check.

## Collect the inputs

**Discover before you ask.** Ask the operator only for a decision, such as
which repositories the factory works, and for a secret that they type into
their own terminal. Never ask the operator for a value that a command on the
machine can read, and never ask them to sign in anywhere, for example with
`gh auth login`. On a re-run, most values already exist in the installed
settings, the Hermes configuration, or the factory's private configuration
record that `~/.claude/CLAUDE.md` names. Run the command for each value
below first, and ask only when it prints nothing:

| Value | Discover with |
| --- | --- |
| Hermes install checkout and its tag | `git -C <home>/hermes-agent describe --tags` |
| Hermes release tag to pin | `tag_name` in `curl -s https://api.github.com/repos/NousResearch/hermes-agent/releases/latest`. Confirm the tag with the operator |
| Existing factory settings: repositories, `app_user_id`, `kanban_url`, `claude_session`, and the `github` object | `hermes config get plugins.entries.linear-agent-session.settings --json` |
| `<app-slug>` | `github.login` in the settings, without `[bot]` |
| GitHub App ID | `gh api /apps/<app-slug> --jq .id`. Any `gh` login can read it |
| GitHub App's permissions | `gh api /apps/<app-slug> --jq .permissions` |
| Installation ID and private key path | `github.installation_id` and `github.private_key_path` in the settings |
| Bot user ID | `gh api '/users/<app-slug>[bot]' --jq .id` |
| Repositories the installation covers | `GH_CONFIG_DIR=<worker>/gh gh api /installation/repositories --jq '.repositories[].full_name'` |
| Infisical project ID, environment, and folder | `grep -oE -e '--projectId[= ][^ ]+' -e '--env[= ][^ ]+' -e '--path[= ][^ ]+' <file>`, where `<file>` holds the `command` that `hermes config get secrets.command --json` prints: the script it names, or a file with that command line |
| Proxy base URL, model alias, and token variable | A loopback listener in `lsof -nP -iTCP -sTCP:LISTEN`, the proxy's own configuration file, and its `/v1/models` list |
| SST app, stage, Cloudflare account, and zone | `find ~ -maxdepth 4 -name 'sst.config.ts' -not -path '*/node_modules/*'`, then the stage configuration in that app. `dig +short <hostname>` shows whether the hostname already points at Cloudflare |
| Existing tunnel | The `cloudflared` system service. On macOS, find its plist with `ls /Library/LaunchDaemons/*cloudflared*` and read only `Label` and `Program` with `plutil -extract <key> raw <plist>`, falling back to `ProgramArguments.0` when `Program` is absent. Never print full `launchctl print` output or any `ProgramArguments` entry after index 0, which can hold the tunnel token. Use the filtered check in [`references/cloudflare-sst.md`](references/cloudflare-sst.md) |
| Slack workspace and channels | The connected Slack account, and the `SLACK_*` key names in `<home>/.env` or the secret source. Read key names only |
| `kanban.max_in_progress` and the webhook port | `hermes config get kanban.max_in_progress` and `hermes config get platforms.webhook.extra.port` |

When `gh` or `hermes` is not on the session's `PATH`, call it by its full
path, such as `~/.local/bin/gh`. Never run any of these with `--raw`, which
prints secrets.

Ask the operator for the values that remain before you start. Record them in
your task notes, not in the repository.

| Input | Used by |
| --- | --- |
| Hermes home directory (default `~/.hermes`) and install checkout | Every part |
| Hermes release tag to pin, `v0.21.4` or later | Hermes |
| Model alias for Hermes' own agent loop, for example `sonnet` | Model provider |
| Repositories: for each, `full_name`, local `path`, and `routing` (`default` for exactly one, `synced_github` for the rest), and an optional `worker_entry` prefix such as `/patina-mode` | Settings, clones, routes |
| `kanban.max_in_progress` | Kanban |
| Factory hostname, Cloudflare account, and zone | Ingress |
| Identities allowed through Access, for example an email domain, and the session length | Ingress |
| Whether people SSH into the machine through Access, and if so the SSH hostname and each person's email | Ingress, host exposure |
| The operator's SST app, its stage, and its review process | Ingress |
| Whether Claude Code uses a local proxy, and if so its base URL, model alias, and token variable name | Claude Code transport |
| Optional with a proxy: a model id for each of the Sonnet, Haiku, and Fable tiers, so subagents run on cheaper models than the worker's main agent. Offer only ids the proxy's `/v1/models` lists. Skip it when the proxy serves one model | Claude Code transport |
| Linear workspace, and whether the operator already has an agent app | Linear app |
| The GitHub organization or account that owns the repositories, and a name for the factory's GitHub App | GitHub App |
| A path outside every repository for the GitHub App's private key, for example `~/.config/factory/github-app.pem` | GitHub App |
| Whether secrets live in Infisical, and if so the project ID, environment, folder, and the two machine identities, one per home | Infisical |
| Whether the operator keeps operator-only credentials in 1Password, and if so the vault and the service account name | 1Password |
| Slack workspace and the channels the agent joins | Slack |
| Where the factory's private configuration record lives, and the credentials on the machine that Hermes agents must never receive | Agent instructions |
| Optional: a pstack model sheet, as a path to a sheet the operator keeps or as answers to the `setup-pstack` questions (a model per role from the aliases the `Agent` tool accepts, default effort, session hook on or off). Never invent model choices. Skip the step when the operator declines | Agent instructions |

## Where each value lives

`<home>` is the default Hermes home. `<worker>` is the `claude-worker` profile
home, `<home>/profiles/claude-worker`. Each one has its own `config.yaml`,
`.env`, `plugins/`, and `scripts/`.

| Value | Location |
| --- | --- |
| `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` | `<home>/.env` and `<worker>/.env` |
| `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` | `<home>/.env` and `<worker>/.env` |
| `LINEAR_WEBHOOK_SECRET`, `GITHUB_WEBHOOK_SECRET` | `<home>/.env` |
| `GH_CONFIG_DIR` | `<home>/.env` and `<worker>/.env`, both set to `<worker>/gh`. The directory holds the worker's `gh` login as the GitHub App, which `refresh-gh-app-login` renews. Hermes' own agent uses it too, such as when it answers in Slack |
| GitHub App private key | The file that `github.private_key_path` names, mode `0600`, outside every repository |
| The token variable that `claude_session.auth_token_env` names | `<worker>/.env`. Never name it `ANTHROPIC_*` |
| `FACTORY_INFISICAL_CLIENT_ID` and `FACTORY_INFISICAL_CLIENT_SECRET`, when the factory uses Infisical | `<home>/.env` holds the default home's machine identity and `<worker>/.env` the worker's, which reads only the worker's three keys. Claude Code receives neither: `claude-session` starts it from an allowlist of variable names |
| Cloudflare tunnel token | The `cloudflared` system service only |
| `CLOUDFLARE_API_TOKEN` | The SST app's own secret storage or environment |
| `OP_SERVICE_ACCOUNT_TOKEN`, when the factory uses 1Password | The factory's Infisical folder only, never a Hermes `.env`. It is for the operator only. The helper drops it, so Hermes agents never receive it, and the operator's `op` wrapper reads it |
| `plugins.entries.linear-agent-session.settings` | `config.yaml` of both homes, same JSON |
| `plugins.entries.pr-ready-gate.settings` | `config.yaml` of both homes |
| `kanban.max_in_progress`, `kanban.review_dispatch`, `platforms.webhook.*`, `dashboard.public_url`, `dashboard.oauth.self_hosted` | `<home>/config.yaml` |
| `streaming.enabled`, `platforms.slack.extra.native_task_cards` | `<home>/config.yaml` |
| The `default` board's `default_workdir` | Kanban board metadata, not `config.yaml` |

When the factory uses Infisical, part 14 moves the Linear credentials, both
webhook secrets, and the proxy token from `.env` into one Infisical folder. On
a new machine, earlier parts still write them to `.env` first. When the folder
already exists, such as on a re-run, write each of those values straight into
the folder with `infisical_set` below, in place of `env_set`. A part's
`env_has` check for such a value then reads the folder's key names, as
[`references/infisical.md`](references/infisical.md) shows.

Write a secret into a `.env` file with this function. It reads the value from
standard input, replaces any earlier line for the key, and keeps the file at
mode `0600`. It never prints the value.

```sh
env_set() {
  file="$1"; key="$2"
  ( umask 077
    touch "$file"
    value="$(cat)"
    grep -v "^${key}=" "$file" > "$file.tmp" || true
    printf '%s=%s\n' "$key" "$value" >> "$file.tmp"
    mv "$file.tmp" "$file" )
  chmod 600 "$file"
}
env_has() { if [ -f "$1" ]; then grep -c "^$2=." "$1"; else echo 0; fi; }
```

`env_has <file> <KEY>` prints `1` when the key has a value and `0` when it does
not.

When the Infisical folder exists, write a secret into it with this function.
It reads the value from standard input and passes it through a mode `0600`
file that it deletes afterwards. Use it anywhere a part pipes into `env_set`.
It needs an Infisical login that can write to the folder. The factory's machine
identity can only read, so this runs under the operator's own Infisical login,
in their terminal when the machine has none.

```sh
infisical_set() {
  ( umask 077; f="$(mktemp)"; printf '%s=%s\n' "$1" "$(cat)" > "$f"
    infisical secrets set --projectId <project id> --env <environment> \
      --path <folder> --file "$f" >/dev/null; rm -f "$f" )
}
```

When a secret comes from a web page, such as an app's client secret, ask the
operator to run this in their own terminal. The value never reaches you:

```sh
read -rs VALUE && printf '%s' "$VALUE" | env_set <file> <KEY>; unset VALUE
```

When the operator must also enter a new secret in a web form, such as a
webhook secret, ask them to generate it in their own terminal. This copies it
to their clipboard on macOS. On Linux, replace `pbcopy` with their clipboard
tool, such as `xclip -selection clipboard`.

```sh
openssl rand -hex 32 | tee >(pbcopy) | env_set <file> <KEY>
```

Give the operator the `env_set` definition first, or have them run each line
in a shell where you defined it.

## Part order

Run the parts in this order. Later parts depend on values from earlier ones.

1. [Hermes](#1-hermes)
2. [Claude Code CLI](#2-claude-code-cli)
3. [Claude Code transport](#3-claude-code-transport)
4. [Model provider](#4-model-provider)
5. [Factory plugins and the worker profile](#5-factory-plugins-and-the-worker-profile)
6. [Webhook secrets and listener](#6-webhook-secrets-and-listener)
7. [Public ingress](#7-public-ingress)
8. [Linear agent app](#8-linear-agent-app)
9. [GitHub App](#9-github-app)
10. [Factory settings, repositories, and Kanban](#10-factory-settings-repositories-and-kanban)
11. [Webhook routes](#11-webhook-routes)
12. [Checkout refresh and drift cron jobs](#12-checkout-refresh-and-drift-cron-jobs)
13. [Slack](#13-slack)
14. [Infisical secret source](#14-infisical-secret-source), optional
15. [Restart and end-to-end checks](#15-restart-and-end-to-end-checks)
16. [Host exposure](#16-host-exposure)
17. [Agent instructions](#17-agent-instructions)

## 1. Hermes

**Check.** All three must hold:

```sh
git -C <install> status --porcelain          # prints nothing
git -C <install> describe --tags --exact-match HEAD   # prints the pinned tag
git -C <install> rev-parse HEAD
git -C <install> rev-parse "<tag>^{commit}"   # same SHA as the line above
```

If the tree is dirty, or `HEAD` has commits that the tag does not, stop. Report
the output to the operator. Never reset, stash, or patch the install.

**Install.** If Hermes is absent, resolve the pinned tag to its commit, then
run the upstream installer from that commit with `--commit`:

```sh
sha="$(git ls-remote https://github.com/NousResearch/hermes-agent "refs/tags/<tag>" "refs/tags/<tag>^{}" | sort -k2 | tail -n1 | cut -f1)"
[ -n "$sha" ] && curl -fsSL "https://raw.githubusercontent.com/NousResearch/hermes-agent/$sha/scripts/install.sh" \
  | bash -s -- --commit "$sha" --dir <install> --hermes-home <home> --non-interactive
```

The `^{}` line names the commit of an annotated tag, so `sort` puts it last
when it exists. An empty `sha` means the tag does not exist; stop and report
it. The installer refuses a commit that is not on its default branch. In that
case, add `--branch <branch>` with the branch that holds the tag. Then run the
check.

**Upgrade.** If the install is clean at an older tag, ask the operator before
you move it to the new tag. Check out the tag and run the check.

## 2. Claude Code CLI

**Check.** `claude --version` meets the DirectSDK provider's minimum, and the
CLI is signed in for the user that runs the Hermes gateway.

The DirectSDK README qualifies version `2.1.263`. Each model alias has its own
minimum:

| Alias | Minimum version |
| --- | --- |
| `opus` | 2.1.280 |
| `sonnet` | 2.1.284 |
| `haiku` | 2.1.293 |

The check uses the higher of `2.1.263` and the minimum for every alias the
factory uses, in both `model.default` and `claude_session.model`.

**Install or upgrade.** Use the operator's install method, either Anthropic's
native installer or the npm package. Run the check again.

> **HUMAN CHECKPOINT.** If the CLI is not signed in, ask the operator to run
> `claude` as the gateway's user and sign in with `/login`. Then run
> `claude -p "Reply with exactly: PONG"` and expect `PONG`.

`claude-session` reads Claude's transcripts from `CLAUDE_CONFIG_DIR`, or
`~/.claude` when that is unset. Sign in under the same config directory that
the worker uses.

## 3. Claude Code transport

This part is optional. Without a proxy, `claude-session` uses the CLI's own
login from part 2. To set up a local Anthropic-compatible proxy, follow
[`references/claude-transport.md`](references/claude-transport.md).

## 4. Model provider

The model provider runs Hermes' own agent loop. Choose one of these and
configure the same one in the default home and in the worker profile:

| Provider | `model.provider` | Use it when |
| --- | --- | --- |
| DirectSDK | `claude-subscription-directsdk-experimental` | No proxy, or the operator wants only Claude Code on the proxy |
| Proxy | `custom:<proxy provider>` | Part 3 set up a proxy and the operator wants Hermes on it too |

`<proxy provider>` is a name the operator picks, such as `proxy`.
`check-install` reports any other `model.provider`, and a value that differs
between the two homes.

Part 5 creates the worker profile, so on a new machine run this part for
`<home>` now and for the worker at the end of part 5.

### DirectSDK

The provider runs Hermes' own agent loop on the Claude subscription.

**Check**, for each home (`hermes` and `hermes -p claude-worker`):

```sh
hermes config get model.provider   # claude-subscription-directsdk-experimental
hermes config get model.default    # the operator's alias
hermes plugins list                # lists claude-subscription-directsdk, enabled
grep -c '^CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1$' <home>/.env <worker>/.env   # 1 for each file
```

The last line matters when the gateway runs as a launchd or systemd service.
In that clean environment, the first request that Claude Code sends is a
terminal-title request. DirectSDK relays a single request, so it returns that
title, a `{"title": ...}` object, in place of the model's answer, and tool
calls never happen. `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` stops the title
request.

**Configure** whatever the check found missing:

```sh
hermes plugins install claude-subscription-directsdk --yes-deps </dev/null
hermes plugins enable claude-subscription-directsdk-experimental
hermes config set model.provider claude-subscription-directsdk-experimental
hermes config set model.default <alias>
printf 1 | env_set <home>/.env CLAUDE_CODE_DISABLE_TERMINAL_TITLE
printf 1 | env_set <worker>/.env CLAUDE_CODE_DISABLE_TERMINAL_TITLE
```

**Verify.**

```sh
hermes chat -q "Reply with exactly: PONG"
hermes -p claude-worker chat -q "Reply with exactly: PONG"
sqlite3 <home>/state.db "select model, billing_provider, input_tokens, output_tokens from sessions order by started_at desc limit 1"
sqlite3 <worker>/state.db "select model, billing_provider, input_tokens, output_tokens from sessions order by started_at desc limit 1"
```

Each latest session names the operator's alias and
`claude-subscription-directsdk-experimental`, with non-zero token counts. A
session with no billing provider and zero tokens never reached the model, even
when the chat appeared to finish. Each reply is `PONG`. A `{"title": ...}`
object in place of the reply means the home's `.env` lacks
`CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1`.

### Proxy

The provider sends Hermes' own agent loop to the proxy from part 3, through a
Hermes custom provider. It needs the proxy port and, when the proxy requires a
token, the token variable from part 3.

**Check**, for each home (`hermes` and `hermes -p claude-worker`):

```sh
hermes config get model.provider                        # custom:<proxy provider>
hermes config get model.default                         # an alias the proxy lists
hermes config get providers.<proxy provider>.api        # http://127.0.0.1:<proxy port>
hermes config get providers.<proxy provider>.transport  # anthropic_messages
hermes config get providers.<proxy provider>.key_env    # <token variable>
```

When the proxy requires a token, `env_has <home>/.env <token variable>` and
`env_has <worker>/.env <token variable>` each print `1`, or the Infisical
folder holds the variable. The name does not start with `ANTHROPIC_`.

**Configure** whatever the check found missing, in each home:

```sh
hermes config set providers.<proxy provider>.api http://127.0.0.1:<proxy port>
hermes config set providers.<proxy provider>.transport anthropic_messages
hermes config set providers.<proxy provider>.key_env <token variable>
hermes config set model.provider custom:<proxy provider>
hermes config set model.default <alias>
```

Skip the `key_env` line when the proxy needs no token. Part 3 stores the token
in the Infisical folder or in `<worker>/.env`. Without Infisical, also store it
in `<home>/.env` with `env_set`.

**Verify** with the same `PONG` chats and `sqlite3` queries as DirectSDK. Each
latest session names the alias and the billing provider `custom`, with non-zero
token counts.

### Keep `ANTHROPIC_*` out of the gateway

This applies to either provider. The DirectSDK provider refuses to start when
the environment holds `ANTHROPIC_AUTH_TOKEN` or another native override. Only
`claude-session` sets `ANTHROPIC_*` variables, and only for the Claude Code
process it starts. This check must print nothing:

```sh
grep -nE '^[^#]*ANTHROPIC_' <home>/.env <worker>/.env \
  <home>/config.yaml <worker>/config.yaml \
  "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json" 2>/dev/null | cut -d: -f1,2
```

Each line it prints is a file and a line number that sets the variable. It
never prints the value. The check fails for that file. Comment lines do not
count. Name each file and line to the operator, and remove the setting with
their approval.

Some local proxy apps offer a one-click "configure Claude Code" or "configure
Hermes" action. That action writes `ANTHROPIC_*` into Claude Code's
`settings.json` or into Hermes' `config.yaml`. Do not use it. Configure the
proxy only through `claude_session`, as part 3 describes.

A variable set in the gateway's service definition or the operator's shell
profile also stops the provider, and the live test in part 15 then fails.

## 5. Factory plugins and the worker profile

`<repo>` is the absolute path of this repository's checkout. Each profile has
its own plugins directory, so install both plugins into both homes.

**Check**, for each home:

```sh
hermes plugins list
hermes -p claude-worker plugins list
```

Both list `linear-agent-session` and `pr-ready-gate`, enabled. On a fresh
machine, install both now and skip the content check.

On a re-run, `<worker>` already exists, and a listed plugin can still be
older than the repository. First bring `<repo>` to an up-to-date default
branch, as in step 1 of
[Deploy a merged factory change](#deploy-a-merged-factory-change). Then
compare content:

```sh
HERMES_HOME=<worker> python3 <worker>/scripts/check-install
```

Read only its `plugin <name> in <home>` lines here. Parts 10 and 11 handle
routes. Skip a plugin's install only when it is listed, enabled, and absent
from those lines. Reinstall a plugin it names with the install command below
plus `--force`.

**Install** each missing plugin into each home:

```sh
hermes plugins install "file://<repo>#plugins/linear-agent-session" --enable --yes-deps </dev/null
hermes plugins install "file://<repo>#plugins/pr-ready-gate" --enable --yes-deps </dev/null
hermes -p claude-worker plugins install "file://<repo>#plugins/linear-agent-session" --enable --yes-deps </dev/null
hermes -p claude-worker plugins install "file://<repo>#plugins/pr-ready-gate" --enable --yes-deps </dev/null
```

To install from GitHub instead of a local checkout, use
`<owner>/<repo>#plugins/<name> --ref <40-character commit SHA>`. Add `--force`
only to reinstall a plugin at a newer commit.

**Worker profile.** If `<worker>` does not exist, install it. If it exists,
update it. An update keeps the installed `config.yaml`. Pass `--force-config`
only when the operator asks to reset it.

```sh
hermes profile install <repo>/profiles/claude-worker --name claude-worker -y </dev/null
hermes profile update claude-worker -y </dev/null
hermes -p claude-worker skills opt-in --sync </dev/null
```

The sync makes the bundled `claude-code` skill available to the worker. A
distribution install sets no model, so now run part 4 for the worker profile.

**Verify.**

- `<worker>/scripts/claude-session`, `<worker>/scripts/refresh-checkouts`,
  and `<worker>/scripts/refresh-gh-app-login` exist and are executable.
- `<worker>/scripts/check-github-access` and `<worker>/scripts/guard-git-push`
  exist. Run them with `python3`.
- `<worker>/SOUL.md` matches `<repo>/profiles/claude-worker/SOUL.md`.
- `hermes -p claude-worker config get platform_toolsets.cli` lists `terminal`,
  `kanban`, and `linear_agent_session`.
- The worker's `PONG` test from part 4 passes.

## 6. Webhook secrets and listener

**Check.**

```sh
env_has <home>/.env LINEAR_WEBHOOK_SECRET   # 1
env_has <home>/.env GITHUB_WEBHOOK_SECRET   # 1
hermes config get platforms.webhook.enabled       # true
hermes config get platforms.webhook.extra.host    # 127.0.0.1
hermes config get platforms.webhook.extra.port    # the webhook port
```

Parts 8 and 9 create the two secrets, because each one also goes into the
Linear app's form or the GitHub App's form. A missing secret here is expected on
a new machine. When the Infisical folder already holds the secrets, check its
key names instead, and have parts 8 and 9 write a new secret with
`infisical_set`.

**Enable** the listener on the loopback interface only. The default port is
`8644`.

```sh
hermes config set platforms.webhook.enabled true
hermes config set platforms.webhook.extra.host 127.0.0.1
hermes config set platforms.webhook.extra.port 8644
```

Part 15 verifies the listener after a restart.

## 7. Public ingress

The factory's hostname reaches the machine through one Cloudflare Tunnel behind
Cloudflare Access. Every resource lives in the operator's own SST app. Follow
[`references/cloudflare-sst.md`](references/cloudflare-sst.md). It ends with
the external checks that this part must pass.

## 8. Linear agent app

Follow [`references/linear-app.md`](references/linear-app.md). It creates the
OAuth app or reuses an existing one, stores its client credentials in
both `.env` files, and finds the `app_user_id` for part 10.

## 9. GitHub App

Follow [`references/github.md`](references/github.md) through its
"Set the worker's `gh` directory" section. It creates the factory's GitHub
App with its webhook, stores the App's private key, installs the App on the
configured repositories, and points the worker's `GH_CONFIG_DIR` at its own
directory. Part 10 needs the App's `login`, `app_id`, `installation_id`, and
`private_key_path`.

## 10. Factory settings, repositories, and Kanban

Build the settings JSON from the operator's answers, the app user ID from
part 8, and the GitHub App values from part 9. Write it to a file in your
scratch directory. The settings hold no secrets. `private_key_path` names the
key file but does not contain the key.

```json
{
  "app_user_id": "<from part 8>",
  "webhook_route": "linear",
  "repositories": [
    {"full_name": "<owner>/<name>", "path": "/abs/path/to/checkout", "routing": "default", "worker_entry": ""}
  ],
  "github": {
    "login": "<app-slug>[bot]",
    "app_id": "<App ID from part 9>",
    "installation_id": "<installation ID from part 9>",
    "private_key_path": "/abs/path/to/github-app.pem"
  },
  "kanban_url": "https://<hostname>/kanban",
  "claude_session": {"base_url": "", "model": "", "tier_models": {}, "auth_token_env": "", "max_turns": 0}
}
```

Exactly one repository has `routing: default`. Leave
`claude_session.base_url`, `model`, and `auth_token_env` empty unless part 3
set up a proxy. With a proxy, set `tier_models` to the Sonnet, Haiku, and Fable
ids from the operator's input, so subagents run on cheaper models while the
worker's main agent stays on `model`. `worker_entry` is a prefix, such as `/patina-mode`, that
`claude-session` puts before the built-in task prompt. The task prompt names
the issue, its URL, the repository, and the branch, and is always sent. An
empty `worker_entry` sends the task prompt alone. `claude-session` refuses a `worker_entry` that
contains `{...}` fields, such as a full prompt from an older factory. Shorten
it to the prefix. `claude_session.max_turns`
caps the turns of one `claude-session` run. `0`, or no value, sets no limit, so
one run can finish a whole task. A positive limit pauses a long task, such as
a dependency upgrade, and the worker resumes it in a second run.

A repository whose workers verify on a simulator or emulator sets
`toolchain_checks`, a list of shell commands, on its entry. Before each run,
`claude-session` runs them in the worktree with the environment that Claude
Code gets. It refuses the run and names the check when one fails. This catches
a toolchain that the worker run cannot reach before a worker reports that the
host cannot verify. Claude Code gets `JAVA_HOME`, `ANDROID_HOME`,
`ANDROID_SDK_ROOT`, and `DEVELOPER_DIR` from the worker's environment, with
`PATH`. For example:

```json
"toolchain_checks": [
  "xcrun simctl help >/dev/null",
  "java -version",
  "test -d \"$ANDROID_HOME/build-tools\""
]
```

`kanban_url` is the dashboard's
Kanban page; triage links it from the "Queued" activity in Linear. Leave it
empty to post no link. Set `github.login` so triage can tell the factory's own
open pull requests from a person's. When it is empty, triage treats every open
pull request as someone else's.

**Check**, for each home:

```sh
diff <(hermes config get plugins.entries.linear-agent-session.settings --json --raw | python3 -m json.tool --sort-keys) \
     <(python3 -m json.tool --sort-keys <settings.json>)
diff <(hermes -p claude-worker config get plugins.entries.linear-agent-session.settings --json --raw | python3 -m json.tool --sort-keys) \
     <(python3 -m json.tool --sort-keys <settings.json>)
```

**Set** the settings in both homes when a diff is not empty:

```sh
hermes config set plugins.entries.linear-agent-session.settings "$(cat <settings.json>)"
hermes -p claude-worker config set plugins.entries.linear-agent-session.settings "$(cat <settings.json>)"
```

The gate's default `gated_profiles` is `[claude-worker]`. If the operator uses
another worker profile name, set
`plugins.entries.pr-ready-gate.settings.gated_profiles` in both homes and read
it back the same way.

**GitHub login.** Return to [`references/github.md`](references/github.md)
and run its "Sign the worker's `gh` in" section now. It needs only the
settings above. The clones below use that login.

**Repositories.** Clone each repository as the App, through the worker's `gh`
login. Never clone under a personal login, and never ask the operator to sign
`gh` in for it.

```sh
[ -d <path>/.git ] || GH_CONFIG_DIR=<worker>/gh gh repo clone <full_name> <path>
```

Then check each checkout:

```sh
git -C <path> remote set-head origin -a
git -C <path> symbolic-ref refs/remotes/origin/HEAD   # refs/remotes/origin/<default branch>
git -C <path> status --porcelain --untracked-files=no # prints nothing
```

The main checkout stays on its default branch. Kanban creates worktrees under
`<path>/.worktrees/`. `claude-session`, `push-signed`, and `refresh-checkouts`
read `origin/HEAD` in every worktree of the checkout, so `check-install`
prints a `checkout` line for a configured checkout that lacks it.

**Node version.** The gateway's service puts Hermes' own Node first on
`PATH`, and Hermes refuses a `PATH` override in config. That Node can be newer
than a repository allows. `claude-session` corrects this for each run:

- It reads `.nvmrc` at the root of the card's worktree. The file must hold a
  numeric version or prefix, such as `24` or `24.21.0`, with or without a
  leading `v`.
- It picks the highest version under `$NVM_DIR/versions/node` that matches
  the prefix, and puts its `bin` first on `PATH` for Claude Code. When the
  worker's environment has no `NVM_DIR`, it uses `~/.nvm`.
- When no installed version matches, it refuses to start Claude Code, exits
  `2`, and names the version in its error:
  `Node <version> from .nvmrc is not installed under <dir>; run nvm install <version>`.
- It refuses an alias such as `lts/*` or `node` as not a numeric version
  prefix.
- Without an `.nvmrc`, it leaves `PATH` alone, so the run uses Hermes' Node.

It does not read `engines.node` in `package.json`, and it does not find Node
installed by Homebrew, fnm, asdf, or Volta. A repository that pins Node only
through `engines.node` runs with Hermes' Node. Ask the operator to add an
`.nvmrc` to that repository.

For each repository, check the pin and the install. The installed list must
include a version that matches the `.nvmrc`:

```sh
cat <path>/.nvmrc                                    # a numeric version, such as 24
ls "${NVM_DIR:-$HOME/.nvm}/versions/node"            # includes a matching v<version>
python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("engines", {}).get("node", "-"))' <path>/package.json
```

The last line runs only when the repository has a `package.json`. It prints
the repository's `engines.node` range, and the `.nvmrc` version must satisfy
it. When no installed version matches, install it as the operator's user, in
a shell where nvm is loaded: `nvm install <version>`. Install any other tool
a repository needs, such as `pnpm`, before the first card runs.

A repository that enables the Engineering plugin runs its issue-handoff gate
with Bun. `<bin dir>` is the directory that holds `gh` and the other tools
setup installs, such as `~/.local/bin`. Check with `command -v bun` and with
`ls <bin dir>/bun`, because a non-login shell can lack `<bin dir>` on `PATH`.
When both fail, install Bun as the gateway's user. The installer puts the
binary in `$BUN_INSTALL/bin`, so set `BUN_INSTALL` to the parent of
`<bin dir>`:

```sh
curl -fsSL https://bun.sh/install | BUN_INSTALL="$(dirname <bin dir>)" bash
<bin dir>/bun --version
```

The installer also adds lines to the user's shell startup file. Leave them.

**GitHub access and checkouts.** Return to
[`references/github.md`](references/github.md) and run its
"Check the App's access", "Configure git in each checkout", and "Block plain
pushes on the machine" sections. They need the clones from this part.
Continue only when
`check-github-access` and `guard-git-push --check` print nothing.

**Kanban.**

```sh
hermes config set kanban.max_in_progress <n>
hermes config set kanban.review_dispatch false
hermes config set kanban.auto_decompose false
hermes kanban boards set-default-workdir default <path of the default repository>
```

A worker hands a card with a merge-ready pull request to the `review` lane.
With `kanban.review_dispatch` set to `false`, Hermes spawns no agent reviewer,
so the card waits there for a person. GitHub triage moves it to `done` when the
pull request merges, and back to the worker when a review requests changes.
Cards in `review` run no worker, so they do not count against
`kanban.max_in_progress`.

Hermes moves a card that blocks twice for the same reason to its `triage`
column, and by default its decomposer then splits the card into child cards.
A factory card is one Linear issue, and its children would lack the key block
the worker needs, so turn the decomposer off. A looping card then waits in
`triage` for a person.

**Check.** `hermes config get kanban.review_dispatch` and
`hermes config get kanban.auto_decompose` each print `false`.

`default_workdir` is board metadata, so `hermes config get` does not show it.
Read the board metadata with `hermes kanban boards --help` to find the listing
command, and confirm the path.

## 11. Webhook routes

`templates/render.py` builds the `linear` and `github` routes and the webhook
rate limit from the settings in part 10. [`templates/README.md`](../../templates/README.md) describes the
output.

**Check.** Compare each rendered route and the rate limit with the installed ones:

```sh
python3 <repo>/templates/render.py --check
```

It prints one line per route, such as
`route linear: matches; secret is the ${LINEAR_WEBHOOK_SECRET} reference`, then
`rate_limit: matches 600`. It exits `0` only when every route and the rate
limit match and every secret is its reference. A
`differs in <keys>` line names the keys that differ. A secret line other than
`is the ... reference` means the route holds a literal secret, or Hermes has no
value for the reference.

Never compare or print an installed route with
`hermes config get ... --raw`. That command expands each `${...}` reference,
so its output holds the live signing secret. `--check` reads it inside the
process and prints only route names, key names, and the reference names.

Render the webhook config for the install step. Its keys are full Hermes
config keys, such as `platforms.webhook.extra.routes.linear` and
`platforms.webhook.extra.rate_limit`:

```sh
python3 <repo>/templates/render.py > <scratch>/webhook-config.json
```

**Install** each key that `--check` reports as not installed, as differing, or
with the wrong secret:

```sh
hermes config set "$key" \
  "$(python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]]))' "$key" < <scratch>/webhook-config.json)"
```

The route secrets are `${LINEAR_WEBHOOK_SECRET}` and `${GITHUB_WEBHOOK_SECRET}`
references. Hermes expands them from `<home>/.env`, so the rendered JSON holds
no secret.

**Dynamic routes.** Run `hermes webhook list`. A static route overrides a
dynamic subscription of the same name in `webhook_subscriptions.json`. If a
dynamic subscription named `linear`, `github`, or the `webhook_route` value
exists, show it to the operator. With their approval, disable it with the
`hermes webhook` CLI or by setting `enabled: false`, so only one
configuration remains.

Render and install again whenever the settings change, then restart the
gateway.

## 12. Checkout refresh and drift cron jobs

The script lives in the worker profile's `scripts/` directory, so the job
belongs to the worker profile.

**Check.** `hermes -p claude-worker cron list` shows exactly one job named
`refresh-checkouts`. `cron create` does not remove duplicates. If more than one
exists, show the list to the operator and remove the extras with their
approval. `hermes cron list` for the default home shows none of the worker's
jobs: `refresh-checkouts`, `refresh-owned-refs`, and `refresh-gh-app-login`.
A copy there runs without the worker's scripts and settings, so remove it the
same way. The default home holds exactly one job, `check-install`, described
below. A second setup session runs this check before it
creates anything.

**Create** the job only when the list has none:

```sh
hermes -p claude-worker cron create "every 1h" --name refresh-checkouts --no-agent --script refresh-checkouts </dev/null
```

Hermes runs a script-only cron job's script with Python, whatever its
shebang says, so every script a `--no-agent` job runs must be Python. A shell
script fails on every tick with a `SyntaxError`.

**Owned refs job.** The `github` route keeps a pull request, review,
`workflow_run`, or `check_run` event only when its branch, and an `issues`
event or an `issue_comment` on an issue only when its issue URL, is listed in
`<home>/owned-refs.json`, which the route reads on every delivery through
Hermes' `in_file` operator. `refresh-owned-refs` rewrites that file
every minute from `hermes kanban list --json` with the `branch_name` and
`github_issue_url` of every card not archived, in one atomic step. Without the
file the route drops every GitHub event except an `issue_comment` on the
App's pull request. `check-install` reports the file when it
is missing or older than 5 minutes. Create the job when `cron list` has no
`refresh-owned-refs` job:

```sh
hermes -p claude-worker cron create "every 1m" --name refresh-owned-refs --no-agent --script refresh-owned-refs </dev/null
```

**Install drift job.** `check-install` compares the live worker profile, the
factory plugins in both homes, and the installed webhook routes with
`origin/HEAD` of the checkout the profile was installed from. It also checks
that the dashboard's OIDC sign-in can keep a user signed in. It also reports
machine changes this runbook cannot reproduce: a `config.yaml` key from parts
4, 6, 10, or 13 that differs from what the part sets, a cron job that is
missing, duplicated, changed, not created by the runbook, or in the wrong
profile, a cron job that delivers to a platform with no home channel in its
profile, the root home's `scripts/check-install-alert` when it is missing or
differs from the repository, and, on macOS, a launchd service from part 7 or 15
that is not installed, does not run at load, or is not running, and
`<home>/owned-refs.json` that is missing or older than 5 minutes. It never
prints a config value. It prints one line per difference and nothing when the
install is current. It fetches first, so a merge shows up within the hour.

Deliver it where the operator reads it: the home channel of the platform from
part 13, such as `slack`. Only the default (root) profile runs that platform
and has its home channel. The gateway refuses the platform's credential in a
second profile, and Hermes drops the output of a job whose profile has no home
channel for its `--deliver` platform, logging `no delivery target resolved`.
So this job belongs to the root profile, unlike the other jobs in this part.
Hermes runs a cron script from the owning profile's own `scripts/` directory,
so install the wrapper there. It runs the worker's current `check-install` with
the worker home, so the job never runs a stale copy. On a first install, do
this after part 13. A root home that has never run a script job has no
`scripts/` directory, so create it first. Run it from the factory checkout:

```sh
install -d <root>/scripts
install -m 0644 skills/setup-factory/scripts/check-install-alert <root>/scripts/check-install-alert
```

Then create the job when `hermes cron list` for the root profile has no
`check-install` job:

```sh
hermes cron create "every 1h" --name check-install --no-agent --script check-install-alert --deliver <platform> </dev/null
```

The job exits non-zero while drift exists, so its failure notice goes to the
same platform. A factory that created this job in the worker profile earlier
removes that copy with `hermes -p claude-worker cron remove <job id>`, with the
operator's approval, after the root job exists.

**Verify.** Run each job through Hermes once, then read its status:

```sh
hermes -p claude-worker cron run <job id>
hermes -p claude-worker cron list    # read each Last run line
```

`refresh-checkouts` prints nothing when every checkout is current, and its
last run shows no error. After `refresh-owned-refs` runs, `<home>/owned-refs.json`
exists and holds a JSON list. `check-install` shows an error until the install
matches, which the Deploy section below fixes. `refresh-checkouts` prints one
line for each checkout it left alone, such as a checkout on another branch or with
uncommitted changes. Fix each problem it names.

## 13. Slack

Follow [`references/slack.md`](references/slack.md).

## 14. Infisical secret source

Skip this part when the operator does not use Infisical. Otherwise follow
[`references/infisical.md`](references/infisical.md).

Then, when the operator uses 1Password for operator-only credentials, follow
[`references/1password.md`](references/1password.md). It needs Infisical.

## 15. Restart and end-to-end checks

**Older factory on this machine.** When the machine already runs an older
factory, do these before the restart:

- Leave the cards that the older factory created on the board. Linear triage
  leaves an issue alone while any card that is not `done` or `archived`, has no
  `linear_issue_id:` line, and names the issue still exists. Triage posts a
  Linear `thought` that names those cards instead of creating a new card.
- The static `linear` and `github` routes from part 11 override the older
  factory's dynamic subscriptions with the same names. Disable those
  subscriptions as the dynamic routes check in part 11 describes.
- Keep the existing webhook port when a tunnel already targets it. Set
  `platforms.webhook.extra.port` to that port in part 6, and use it in place of
  `8644` in the local checks below.
- Check `kanban.max_spawn`. It caps the running cards the gateway's dispatcher
  allows, so `0` means the dispatcher never starts a card. An older factory can
  set it to `0` because its own triage started cards. Factory triage only
  queues cards, so set `kanban.max_spawn` to the same value as
  `kanban.max_in_progress`. To keep the dispatcher from also starting the older
  factory's cards, set `kanban.dispatch_profiles` to `[claude-worker]`. Confirm
  with `hermes kanban dispatch --dry-run --json`: `spawned` lists only
  `claude-worker` cards.
- `hermes gateway restart` waits for running work, including in-process cron
  jobs, before it restarts, and the gateway refuses webhooks while it waits.
  Restart when no long cron job is running.

> **HUMAN CHECKPOINT.** Ask the operator to run `hermes gateway restart` from
> a shell outside Hermes, not from a Hermes session or tool.

If the gateway does not run as a service yet, install it as one with the
Hermes CLI's gateway service command (see `hermes gateway --help`), so it
survives a reboot.

**Local checks.**

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8644/webhooks/linear                  # 405
curl -s -X POST -H 'content-type: application/json' -d '{}' http://127.0.0.1:8644/webhooks/linear # 401 Invalid signature
lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(8644|<dashboard port>|<proxy port>) '                    # 127.0.0.1 only
```

Then prove that each route's secret is a real value and not an unexpanded
`${...}` reference. When the secret source fails at startup, Hermes still
starts, and a route then uses the public literal string as its secret. These
lines sign a delivery with each literal string. Each must print `401`. They
print no secret.

```sh
forged_post() {
  python3 - "$@" <<'PY'
import hashlib, hmac, sys, urllib.error, urllib.request
route, literal, header, event = sys.argv[1:]
body = b'{"type": "SetupCheck"}'
digest = hmac.new(literal.encode(), body, hashlib.sha256).hexdigest()
headers = {"Content-Type": "application/json", header: digest if header == "Linear-Signature" else "sha256=" + digest}
if event:
    headers["X-GitHub-Event"] = event
try:
    with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:8644/webhooks/{route}", data=body, headers=headers)) as resp:
        print(route, resp.status)
except urllib.error.HTTPError as err:
    print(route, err.code)
PY
}
forged_post linear '${LINEAR_WEBHOOK_SECRET}' Linear-Signature ""     # linear 401
forged_post github '${GITHUB_WEBHOOK_SECRET}' X-Hub-Signature-256 ping # github 401
```

A `401` proves something only when the same request signed with the real
secret is accepted. The signed checks in
[`references/cloudflare-sst.md`](references/cloudflare-sst.md) show that. Run
these lines after every gateway restart.

The `linear-agent-session` plugin is the last line of defense, for every
webhook route. The gateway answers a forged delivery on such a route with
`202` because it sends the status before plugins run. The plugin then drops
the delivery before any agent run and before its Linear acknowledgement, and
logs
`linear-agent-session: dropped a delivery on route <route>: secret is the unresolved reference ${<NAME>}`.
At startup it logs one line per such route. A `202` from a forged line
still means the secret source failed, so follow the steps below.

If a forged line prints anything but `401`, the gateway is serving a route
that anyone can sign for. Stop it before you fix the secret source.

> **HUMAN CHECKPOINT.** Ask the operator to run `hermes gateway stop` from a
> shell outside Hermes, not from a Hermes session or tool. Then confirm that
> `hermes gateway status` no longer shows a gateway PID and that
> `lsof -nP -iTCP:8644 -sTCP:LISTEN` prints nothing.

Fix the secret source, for example with the manual helper run in
[`references/infisical.md`](references/infisical.md#configure-both-homes).
Then ask the operator to start the gateway with `hermes gateway start` from a
shell outside Hermes, and run the forged lines again.

**External checks.** Run the checks at the end of
[`references/cloudflare-sst.md`](references/cloudflare-sst.md).

**End-to-end checks.** Use a test issue in the operator's Linear workspace for
each check. Report each result.

1. Delegate an issue that routes to the `default` repository. Linear shows the
   acknowledgement within 10 seconds, then the "queued" activity. The new card
   has that repository as its `completion_contract`.
2. Delegate an issue synced from a `synced_github` repository. Its card has that
   repository's contract, and its worktree starts from that repository's
   default branch.
3. The worker moves the issue to In Progress through the repository's own
   issue-start workflow. Hermes makes no status change.
4. While the pull request is a draft, has a pending or failing non-required
   check, has a conflict, or is `BLOCKED` or `BEHIND`, `kanban_request_review`
   and `kanban_complete` are refused. `hermes kanban complete` from the
   worker's shell is also refused.
5. A `CLEAN`, non-draft pull request moves the card to `review`, not `done`.
   An `action` activity with the pull request link appears in the same Linear
   session, and no reviewer worker starts for the card.
6. A "changes requested" review on GitHub returns the card to `ready`. A worker
   run starts, and the card returns to `review` after the fix.
7. Merging the pull request moves the card to `done`. The final `response`
   with the pull request link appears in the same Linear session.
8. A reply in Linear during a run is picked up when the run ends, and the next
   run resumes the same Claude session ID.
9. A Linear `stop` stops the worker and blocks the card.
10. Un-delegating stops the worker and blocks the card with reason
    `undelegated`. Delegating again unblocks the same card, which resumes the
    same branch, worktree, and session.
11. A mention without delegation gets an answer and creates no card.
12. No more than `kanban.max_in_progress` workers run at once, and no `claude`
    process runs outside a Kanban worker.
13. `git -C <install> status --porcelain` still prints nothing, and `HEAD` is
    still the pinned tag.
14. `git -C <repo> status --porcelain` prints nothing. This repository holds no
    factory value.
15. Publish an issue's `gitBranchName` to GitHub with one commit as the App,
    with `push-signed` from a clone outside the checkout, then delegate the
    issue. The first `claude-session` line reports `adopted` with `source`
    `remote_branch`, the branch starts at that commit, and the first
    `push-signed` publishes. Then repeat with a branch a person pushed under
    another issue's `gitBranchName` and no pull request. `claude-session`
    refuses with a line that names the branch and the App, the card blocks,
    and the person's branch is unchanged.
16. Open a pull request as the GitHub App on a branch other than the issue's
    `gitBranchName`, link it to the issue, and delegate the issue. The card's
    branch is that pull request's head branch, and no second pull request
    opens.
17. Open a pull request as a person, link it to the issue, and delegate the
    issue. The card body carries `takeover_pr`, the worker starts from that
    pull request's head, and its own pull request links the original. Before
    it completes, the worker asks in Linear whether to close the original and
    blocks with `needs_input`. It closes the original only after a human
    approves.
18. Once a card's pull request is open, send a Linear `stop`, then reply in
    the same agent session. In `hermes kanban show <task id> --json`, the
    events after `blocked` are `unblocked`, `review_requested`, and
    `review_reopened`. The next dispatcher tick starts a worker, no
    `respawn_guarded` event with reason `active_pr` follows, and the card
    completes without operator action.

**Known limitation.** Only the Kanban dashboard reopens a `done` or `triage`
card. The operator drags the card to Ready, which Hermes documents as a
deliberate re-queue. Hermes v0.21.6 has no CLI command or tool for it. When a
GitHub event arrives for a done card, triage comments on the card and asks the
operator to reopen the card from the Kanban dashboard. A card reaches `done`
only when its pull request merges, so this applies to follow-up work after the
merge.

## 16. Host exposure

Outside Cloudflare Access, the machine exposes only SSH, and SSH accepts only
keys and certificates. Run the check from this repository's checkout on the
machine. Pass the SSH allowlist when people SSH in through Access, and leave
`--ssh-users` out when nobody does:

```sh
python3 skills/setup-factory/scripts/check-host-exposure --ssh-users <email>,<email>
```

It probes each of the machine's LAN and public addresses, including public
IPv6 addresses that a router may not block. Add
`--principals-file /etc/ssh/authorized_principals/<account>` when people SSH
in as a different account from the one running the check. If every line
starts with `PASS`, report the part as already configured.

Each `FAIL` line names its fix. Turn off a sharing service in System Settings.

> **HUMAN CHECKPOINT.** Ask the operator to turn off each reported sharing
> service, and to turn on the firewall when it is off. Run the check again.

The SSH fixes need `sudo`. Write one script that does all of them, and follow
the rule for privileged steps:

- Back up each file it edits.
- Write `/etc/ssh/authorized_principals/<account>` with one line for each
  allowlist email's local part. Never add a name that is not on the list.
- Before it turns off password login, read the last day's accepted SSH logins
  with `log show --last 24h --info --predicate 'eventMessage CONTAINS "Accepted "'`.
  If any login used `password` or `keyboard-interactive`, skip that change
  and report who would be locked out.
- Add `PasswordAuthentication no` and `KbdInteractiveAuthentication no` to a
  file in `/etc/ssh/sshd_config.d/`, then run `sshd -t`. If it fails, restore
  the backups.

macOS starts sshd for each connection, so new connections use the change
without a restart. Run the check again. It must print only `PASS` lines.

## 17. Agent instructions

Follow [`references/agent-instructions.md`](references/agent-instructions.md).
It writes the factory's operating rules into `~/.claude/CLAUDE.md`, so every
Claude Code session on the machine acts on GitHub as the App, publishes
commits with `push-signed`, and knows where the private configuration record
lives.

**Hermes' own agent.** Hermes' default-profile agent, which answers in Slack,
does not read `~/.claude/CLAUDE.md`. Without its own notes, it looks for
credentials in its tool shell, finds none, and reports that GitHub or Linear
is not set up. `templates/memory.py` installs the factory's entries in its
memory file. They say that credentials live in the gateway, that `gh` acts as
the App through `GH_CONFIG_DIR`, that Linear is reached through the
`linear-agent-session` tools, and that a Slack message proves Slack works.

**Check.**

```sh
python3 <repo>/templates/memory.py --check --gh-config-dir <worker>/gh <home>/memories/MEMORY.md
grep '^GH_CONFIG_DIR=' <home>/.env    # GH_CONFIG_DIR=<worker>/gh
```

The first command prints nothing when every factory entry is present and
current. Otherwise it prints one line per entry that is missing or differs.

**Install.** The command replaces each factory entry with the same label,
such as `GitHub:`, and keeps every other entry, so the agent's own memories
stay. It refuses to write a file over Hermes' limit of 2,200 characters. Then
set the `gh` directory for the default home:

```sh
python3 <repo>/templates/memory.py --gh-config-dir <worker>/gh <home>/memories/MEMORY.md
printf '%s' <worker>/gh | env_set <home>/.env GH_CONFIG_DIR
```

Hermes reads the memory file when a session starts. The gateway reads
`<home>/.env` only when it starts, so after `env_set` changes that file, ask
the operator to restart the gateway as in part 15.

**pstack model sheet.** Skip this step when the operator gave no sheet and
declined the `setup-pstack` questions. `claude-session` forwards `HOME` and
`CLAUDE_CONFIG_DIR` to worker runs and does not set `CLAUDE_CONFIG_DIR`, so
worker sessions read the same `<config>/CLAUDE.md` as the operator, where
`<config>` is `$CLAUDE_CONFIG_DIR` or `~/.claude`.

1. Write the sheet to `<config>/pstack-models.md`, replacing the whole file so
   a re-run converges. Use the shape in
   [`references/pstack-models-template.md`](references/pstack-models-template.md)
   with the operator's values. Use model family names such as `opus`, never
   full `claude-*` IDs, which the `Agent` tool rejects. Record the values in
   the private configuration record, never in this repository.
2. Add the include, outside the factory block, only when it is absent. The
   command backs up `CLAUDE.md` first and refuses to edit a file whose
   `<!-- factory:begin -->` and `<!-- factory:end -->` pair is not intact:

   ```sh
   python3 <repo>/skills/setup-factory/scripts/pstack-include --write
   ```

3. Check. Every line must start with `PASS`: the sheet exists and names no
   model other than an `Agent` alias, the marker pair is intact, and the include
   appears exactly once outside the block. Part 17's block write keeps lines
   outside the markers, so a later re-run does not remove the include.

   ```sh
   python3 <repo>/skills/setup-factory/scripts/pstack-include
   ```

   Pass `--config-dir <config>` when `CLAUDE_CONFIG_DIR` is set in the worker
   environment but not in your shell.

## Deploy a merged factory change

A merged change to `plugins/`, `profiles/`, or `templates/` is not live until
it is installed. Hermes loads plugins and webhook routes only when the gateway
starts. The worker reads `SOUL.md` and its scripts on each run.

1. Bring the checkout the profile was installed from up to date. Its path is
   the `source` line of `<worker>/distribution.yaml`, two levels up. It must be
   on the default branch with no uncommitted changes. Fast-forward it to
   `origin/HEAD`.
2. List what is out of date:

   ```sh
   HERMES_HOME=<worker> python3 <worker>/scripts/check-install
   ```

3. Install each item it names:
   - A `profile claude-worker` line: `hermes profile update claude-worker -y </dev/null`.
   - A `plugin <name> in <home>` line: run the part 5 install command for that
     home with `--force`.
   - A `webhook config <key>` line: run the part 11 install for that key.
   - A `webhook route <name>: <n> rejected with HTTP 429` line: the gateway
     dropped deliveries in the last hour. Check that
     `platforms.webhook.extra.rate_limit` is installed. If it is, show the
     operator the count, and open an issue to raise `RATE_LIMIT` in
     `templates/render.py`.
   - An `owned refs` line: create the part 12 `refresh-owned-refs` job, or
     run it with `cron run` when it exists.
   - A `checkout` line: the checkout is missing or has no `origin/HEAD`. Clone
     it as in part 10, or run `git -C <path> remote set-head origin -a`.
   - A `dashboard sign-in` line: follow step 6 of
     [`references/cloudflare-sst.md`](references/cloudflare-sst.md#6-configure-the-dashboard-sign-in),
     or allow the `refresh_tokens` grant on the `saas` Access application when
     the line names the issuer. Then restart the dashboard service.
   - A `script <root>/scripts/check-install-alert` line: install the wrapper
     again with the part 12 `install` commands, which create `<root>/scripts` first.
   - A `cron job check-install` line: the job is missing, in the wrong
     profile, or delivers to a platform with no home channel. Create it in the
     root profile as in part 12, and remove a leftover copy in the worker
     profile. Set the platform's home channel in the root home as in part 13.
   - A `config`, `cron job`, or `service` line: the machine differs from what
     the named part produces. If the runbook is right, run that part again.
     If the machine is right, change the part and its row in `check-install`
     in one pull request, as the replicability rule in `AGENTS.md` requires.
4. Run `check-install` again. It prints nothing.
5. If step 3 changed a plugin or a webhook config key:

   > **HUMAN CHECKPOINT.** Ask the operator to run `hermes gateway restart`
   > from a shell outside Hermes. Then confirm that
   > `~/.hermes/logs/gateway.log` shows a startup after the install.

6. Record the deployed commit in the private configuration record.

## Report

End with one line per part: already configured, configured now, or blocked.
For a blocked part, name the failed check, the output, and the action the
operator must take.
