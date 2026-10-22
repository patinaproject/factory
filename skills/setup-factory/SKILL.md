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
| Infisical URL, project ID, environment, and folder | `grep -e '^api_url=' -e '^project=' -e '^environment=' -e '^folder=' <file>`, where `<file>` is the script that the `command` of `hermes config get secrets.command --json` names. A helper rendered before the REST API change has no `api_url` line and names the rest with `--projectId`, `--env`, and `--path` flags, which `grep -oE -e '--projectId[= ][^ ]+' -e '--env[= ][^ ]+' -e '--path[= ][^ ]+' <file>` prints |
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
| `triage.max_running`, how many triage cards may run at once | Kanban |
| Factory hostname, Cloudflare account, and zone | Ingress |
| Identities allowed through Access, for example an email domain, and the session length | Ingress |
| Whether people SSH into the machine through Access, and if so the SSH hostname and each person's email | Ingress, host exposure |
| The operator's SST app, its stage, and its review process | Ingress |
| Whether Claude Code uses a local proxy, and if so its base URL, model alias, and token variable name | Claude Code transport |
| Optional with a proxy: a model id for each of the Sonnet, Haiku, and Fable tiers, so subagents run on cheaper models than the worker's main agent. Offer only ids the proxy's `/v1/models` lists. Skip it when the proxy serves one model | Claude Code transport |
| Linear workspace, and whether the operator already has an agent app | Linear app |
| The GitHub organization or account that owns the repositories, and a name for the factory's GitHub App | GitHub App |
| A path outside every repository for the GitHub App's private key, for example `~/.config/factory/github-app.pem` | GitHub App |
| Whether secrets live in Infisical, and if so the project ID, environment, folder, and the machine identity the default home signs in as | Infisical |
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
| `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` | `<home>/.env` only. The gateway's broker gives the worker a Linear access token on request, so the worker never holds the app's credentials |
| `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` | `<home>/.env` and `<worker>/.env` |
| `LINEAR_WEBHOOK_SECRET`, `GITHUB_WEBHOOK_SECRET` | `<home>/.env` |
| `GH_CONFIG_DIR` | `<home>/.env` and `<worker>/.env`, both set to `<worker>/gh`. The directory holds the worker's `gh` login as the GitHub App, which `refresh-gh-app-login` renews. Hermes' own agent uses it too, such as when it answers in Slack |
| GitHub App private key | The file that `github.private_key_path` names, mode `0600`, outside every repository |
| `CLAUDE_PROXY_TOKEN`, the proxy's token that `claude_session.auth_token_env` names | `<home>/.env`. The worker's `worker-secrets` asks the gateway's broker for it when a worker process starts, because the worker's own agent loop sends it to the proxy, and Claude Code gets it through `apiKeyHelper`. Never store it in `<worker>/.env`, and never name it `ANTHROPIC_*` |
| Each MCP server token that `claude_session.mcp_servers.<name>.token_env` names, such as `SENTRY_ACCESS_TOKEN` | `<home>/.env`. The run's server of that name gets it through `mcp-headers`, which asks the gateway's broker. Never store it in `<worker>/.env`. See [MCP server identities](references/mcp-identities.md) |
| `FACTORY_INFISICAL_CLIENT_ID` and `FACTORY_INFISICAL_CLIENT_SECRET`, when the factory uses Infisical | `<home>/.env` only. The worker has no Infisical identity. Claude Code never receives it: `claude-session` starts it from an allowlist of variable names |
| The factory's secret names that Hermes strips from every process a home starts | `plugins/factory-secret-names/plugin.yaml`, installed in both homes |
| Cloudflare tunnel token | The `cloudflared` system service only |
| `CLOUDFLARE_API_TOKEN` | The SST app's own secret storage or environment |
| `OP_SERVICE_ACCOUNT_TOKEN`, when the factory uses 1Password | The factory's Infisical folder only, never a Hermes `.env`. It is for the operator only. The helper drops it, so Hermes agents never receive it, and the operator's `op` wrapper reads it |
| `plugins.entries.linear-agent-session.settings` | `config.yaml` of both homes, same JSON |
| `plugins.entries.pr-ready-gate.settings` | `config.yaml` of both homes |
| `GITHUB_UPLOAD_TOKEN`, the operator's upload-only token | The factory's Infisical folder only, entered by the operator. The gateway loads it. Neither `.env` holds it, and the broker never hands it out |
| `platforms.factory_uploads.enabled` and `platforms.factory_uploads.extra.repositories` | `<home>/config.yaml` only |
| `plugins.entries.team.settings.skills_dir` and the optional `allow` | `<home>/config.yaml` of the root home only. The directory path is private, so it never enters this repository |
| `kanban.max_in_progress`, `kanban.review_dispatch`, `platforms.webhook.*`, `dashboard.public_url`, `dashboard.oauth.self_hosted` | `<home>/config.yaml` |
| `streaming.enabled`, `platforms.slack.extra.native_task_cards` | `<home>/config.yaml` |
| The `default` and `triage` boards' `default_workdir` | Kanban board metadata, not `config.yaml` |

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

When the proxy requires a token, `<token variable>` is `CLAUDE_PROXY_TOKEN`,
and `env_has <home>/.env CLAUDE_PROXY_TOKEN` prints `1` or the Infisical folder
holds it. The worker home reads the same variable, which `worker-secrets` gets
from the gateway's broker when a worker process starts, as part 5 sets up.

**Configure** whatever the check found missing, in each home:

```sh
hermes config set providers.<proxy provider>.api http://127.0.0.1:<proxy port>
hermes config set providers.<proxy provider>.transport anthropic_messages
hermes config set providers.<proxy provider>.key_env <token variable>
hermes config set model.provider custom:<proxy provider>
hermes config set model.default <alias>
```

Skip the `key_env` line when the proxy needs no token. Part 3 stores the token
in `<home>/.env` or the Infisical folder, never in `<worker>/.env`.

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

Both list `linear-agent-session`, `pr-ready-gate`, `blocker-order`, and
`factory-secret-names`,
enabled. The root home also lists `factory-uploads`, `team`, and
`webhook-retry`, enabled, and the worker home lists none of them. On a fresh
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
hermes plugins install "file://<repo>#plugins/blocker-order" --enable --yes-deps </dev/null
hermes -p claude-worker plugins install "file://<repo>#plugins/blocker-order" --enable --yes-deps </dev/null
hermes plugins install "file://<repo>#plugins/factory-secret-names" --enable --yes-deps </dev/null
hermes -p claude-worker plugins install "file://<repo>#plugins/factory-secret-names" --enable --yes-deps </dev/null
```

`factory-secret-names` runs no code. Its manifest declares the factory's
secret variables, and Hermes strips each one from every process the home
starts: a terminal command, a background process, a cron script, or a
`claude-session` run. Without it, any process on the machine could read the
Linear client secret, the webhook secrets, or the proxy token from such a
process's environment with `ps eww`. Hermes reads the manifest at each spawn,
so it takes effect without a restart.

The strip has two gaps. Hermes runs a `secrets.command` helper without it,
so `factory-secrets` holds the gateway's Infisical identity (#176). A plugin
hook that starts a subprocess without its own `env` passes the home's
process environment on, so `pr-ready-gate`'s `git` and `gh` calls in the
worker hold the proxy token.

To install from GitHub instead of a local checkout, use
`<owner>/<repo>#plugins/<name> --ref <40-character commit SHA>`. Add `--force`
only to reinstall a plugin at a newer commit.

**Gateway broker, root home only.** The `factory-uploads` plugin runs inside
the gateway. Its broker uploads evidence images with `GITHUB_UPLOAD_TOKEN`, and
it gives the worker the two credentials the worker may use: a Linear access
token, minted with the gateway's Linear app credentials, and the proxy token.
The worker home holds no factory secret of its own. Install the plugin in the
root home and never in the worker's home. `check-install` reports an install
in the worker home.

1. **Operator, in their own terminal.** Enter `GITHUB_UPLOAD_TOKEN` in the
   factory's Infisical folder, as in [`references/infisical.md`](references/infisical.md).
   Never ask for the value in chat. The gateway reads secret sources when it
   starts, so a new or rotated token takes effect at the next gateway restart.
2. Install and enable the plugin in the root home:

   ```sh
   hermes plugins install "file://<repo>#plugins/factory-uploads" --enable --yes-deps </dev/null
   ```

3. Enable the platform and list the repositories the broker may upload for,
   as `<owner>/<repo>` names from the factory's `repositories` setting. An
   empty list refuses every upload:

   ```sh
   hermes config set platforms.factory_uploads.enabled true
   hermes config set platforms.factory_uploads.extra.repositories '["<owner>/<repo>"]'
   ```

4. > **HUMAN CHECKPOINT.** Ask the operator to restart the gateway with
   > `python3 <repo>/skills/setup-factory/scripts/restart-gateway` from a shell outside Hermes, as part 15 describes. Hermes loads
   > platform plugins only when the gateway starts.

5. **Check.** `<home>/gateway_state.json` lists `factory_uploads` as
   `connected`, and `<home>/factory-uploads.sock` exists with mode `srw-------`.
   `check-install` also checks the platform's config keys and sends the socket
   `{"op":"status"}`. It reports a socket nobody listens on, and a broker
   whose `token_present`, `linear_ready`, or, with a proxy token,
   `proxy_token_present` is false. It never uploads and asks for no token.
   `python3 <home>/plugins/factory-uploads/upload_token.py --probe --repo <owner>/<repo>`
   is for the operator's own terminal: it prints the HTTP status and the
   attachment URL, never the token. The platform is not a chat platform, so
   `hermes status` lists it without any channel and it is not a cron
   `--deliver` or `send_message` target.

**Broker user, macOS.** The gateway runs as the factory user, and so does
every process Claude Code starts. Such a process can read any file the factory
user can read. On macOS, move the broker and the secrets only it needs to a
hidden service user. The worker side then reaches them only through a socket
in a shared directory, and the broker answers only the factory user's uid.

The gateway itself stays with the factory user. Hermes' Kanban dispatcher runs
inside the gateway and starts every worker, so a gateway under the service
user would start Claude Code there too.

| Path | Owner and mode | Holds |
| --- | --- | --- |
| `/Library/Application Support/factory-broker/lib` | `root:wheel`, `0755`, files `0644` | The broker's code, copied from this repository |
| `/Library/Application Support/factory-broker/broker.json` | `root:wheel`, `0644` | Non-secret config: socket, allowed uid, repository ids, Infisical location |
| `/Library/Application Support/factory-broker/home` | `_factorybroker`, `0700` | `.env`, the audit log, and the Linear token cache |
| `/Library/Application Support/factory-broker/run` | `_factorybroker:_factorybroker`, `0750` | The socket, mode `0660` |
| `/Library/LaunchDaemons/local.factory-broker.plist` | `root:wheel`, `0644` | The service, run as `_factorybroker` with the Command Line Tools' `python3` |

Only root can change the code the broker runs. A copy in the factory user's
home would let a factory process rewrite the broker and read its secrets. For
the same reason, root never runs the script from the factory's checkout: a
factory process can change that checkout. `apply` and `set-secret` refuse to
run unless the script and every file they copy are root-owned, so the operator
runs them from a fresh root-owned clone at the commit to install. The service
runs `/Library/Developer/CommandLineTools/usr/bin/python3`, not
`/usr/bin/python3`, because that shim follows `xcode-select` into
`/Applications`, which any admin can write.

1. **Check.** `check-install` reads `broker.json` when it exists and reports
   each difference as a `broker user` line. When `broker.json` exists and
   `check-install` prints no `broker user` or `upload broker` line, skip to
   the next part.
2. **Operator, in Infisical.** Follow
   [Broker identity](references/infisical.md#broker-identity). It moves
   `GITHUB_UPLOAD_TOKEN`, `LINEAR_CLIENT_ID`, and `LINEAR_CLIENT_SECRET` into
   a folder that only the broker's own identity reads. Without Infisical, skip
   this step. The operator enters the three values with `set-secret` in step 5.
3. Write the input file in your scratch directory. It holds no secret. Get
   each repository's numeric id with
   `gh api repos/<owner>/<repo> --jq .id`, and the variable names from
   `hermes config get plugins.entries.linear-agent-session.settings.claude_session --json`:

   ```json
   {"factory_user": "<account>",
    "repositories": {"<owner>/<repo>": <id>},
    "claude_session": {"auth_token_env": "<name>", "mcp_servers": {"<server>": {"token_env": "<name>"}}},
    "infisical": {"api_url": "<url>", "project_id": "<project id>",
                  "environment": "<environment>", "paths": ["<folder>", "<folder>/broker"]}}
   ```

   Leave `infisical` out when the factory does not use Infisical. `api_url`
   is `https://app.infisical.com` or `https://eu.infisical.com`. For a
   self-hosted server, also set `"self_hosted_host"` to the URL's host name.
   The script refuses any other URL, because the broker sends its Infisical
   identity there.
4. Print the plan. It changes nothing and needs no `sudo`:

   ```sh
   python3 <repo>/skills/setup-factory/scripts/setup-broker-user plan --input <scratch>/broker-input.json
   ```

   It refuses, before any change, a name that belongs to an existing login
   account, a factory user that is not a login account, a group with other
   members, an interpreter or parent directory that a non-root user can
   write, and a broker directory that is a link or another user's. It never
   edits an existing account, the factory user's shell, sshd, sudoers, or
   the SSH access group.
5. > **HUMAN CHECKPOINT. Privileged step.** Show the operator the plan.
   > `<sha>` is the full 40-character commit to install. The operator checks on GitHub that it
   > is on the default branch, because a factory process can push other
   > branches. The factory's App can also merge to the default branch
   > without a person's review, so the operator reads the broker's code
   > before root installs it. On a first install, they read the files at
   > `<sha>`. On an update, they read the change since the installed commit:
   >
   > ```sh
   > cat "/Library/Application Support/factory-broker/installed-commit"
   > git diff <installed>..<sha> -- plugins/factory-uploads plugins/linear-agent-session/linear.py skills/setup-factory/scripts/setup-broker-user
   > ```
   >
   > Then ask them to run, in their own terminal:
   >
   > ```sh
   > /usr/bin/sudo -H /usr/bin/env -i HOME=/var/root PATH=/usr/bin:/bin:/usr/sbin:/sbin GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null /bin/sh -ec 'g=/Library/Developer/CommandLineTools/usr/bin/git; d=/var/root/factory-src; [ -d "$d" ] || "$g" clone -q https://github.com/<owner>/<repo> "$d"; "$g" -C "$d" fetch -q origin; "$g" -C "$d" merge-base --is-ancestor <sha> origin/<default branch>; "$g" -C "$d" checkout -q --detach <sha>; test "$("$g" -C "$d" rev-parse HEAD)" = <sha>'
   > /usr/bin/sudo /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin /Library/Developer/CommandLineTools/usr/bin/python3 -I /var/root/factory-src/skills/setup-factory/scripts/setup-broker-user apply --input <scratch>/broker-input.json
   > /usr/bin/sudo /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin /Library/Developer/CommandLineTools/usr/bin/python3 -I /var/root/factory-src/skills/setup-factory/scripts/setup-broker-user set-secret BROKER_INFISICAL_CLIENT_ID
   > /usr/bin/sudo /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin /Library/Developer/CommandLineTools/usr/bin/python3 -I /var/root/factory-src/skills/setup-factory/scripts/setup-broker-user set-secret BROKER_INFISICAL_CLIENT_SECRET
   > ```
   >
   > The first line clones as root with no Git config from the factory
   > user's home, so no hook or URL rewrite of theirs runs, and it stops
   > unless `<sha>` is on the default branch. `env -i` keeps the factory
   > user's `PATH` and variables out of every root process, and
   > `/usr/bin/sudo` keeps the factory user's `PATH` from choosing `sudo`
   > itself. `apply` prints the diff of every root-owned file it changes,
   > against the installed copy, including its own copy of
   > `setup-broker-user`. It asks the operator to type `yes` before any
   > change, and records the commit in `installed-commit`. The operator
   > checks the code, the factory user, the repositories, and the Infisical
   > host. Without Infisical, the `set-secret` lines name
   > `GITHUB_UPLOAD_TOKEN`, `LINEAR_CLIENT_ID`, and `LINEAR_CLIENT_SECRET`
   > instead. `set-secret` reads the value from the terminal and never from
   > an argument. A second `apply` changes nothing.
6. Turn off the gateway's own broker, and remove `GITHUB_UPLOAD_TOKEN`,
   `LINEAR_CLIENT_ID`, and `LINEAR_CLIENT_SECRET` from `<home>/.env` when it
   holds them:

   ```sh
   hermes config set platforms.factory_uploads.enabled false
   ```

7. > **HUMAN CHECKPOINT.** Ask the operator to restart the gateway with
   > `python3 <repo>/skills/setup-factory/scripts/restart-gateway` from a
   > shell outside Hermes. The gateway then runs no broker, and processes it
   > starts carry the new group.
8. Link the root home's socket path to the service. Every worker script and
   the gateway's Linear client connect to `<home>/factory-uploads.sock`.
   Create the link after the restart, because the stopping gateway removes
   that path:

   ```sh
   ln -sfn "/Library/Application Support/factory-broker/run/factory-uploads.sock" <home>/factory-uploads.sock
   ```

9. **Check.** `check-install` prints no `broker user` and no `upload broker`
   line, and `check-host-exposure` still prints only `PASS` lines. The
   broker's log is `/Library/Application Support/factory-broker/home/broker.log`,
   which only root and the broker user read.

**Team skills, root home only.** The `team` plugin registers the skills in a
directory on the machine as `team:<name>` skills. It reads the directory in
place and copies nothing, so no skill content enters this repository. Install
it in the root home and never in the worker's home. `check-install` reports an
install in the worker home.

1. Install and enable the plugin in the root home:

   ```sh
   hermes plugins install "file://<repo>#plugins/team" --enable --yes-deps </dev/null
   ```

2. Point it at the team skills directory. Each subdirectory that holds a
   `SKILL.md` is one skill. The real path goes in Hermes config and the
   private record only:

   ```sh
   hermes config set plugins.entries.team.settings.skills_dir <team skills directory>
   ```

   To register only some skills, set `plugins.entries.team.settings.allow` to
   a list of skill names. An empty or unset `allow` registers every skill. A
   name the directory lacks stops the plugin from loading.
3. **Check.** `check-install` reports a `skills_dir` that is unset, missing,
   or empty, and an `allow` name the directory lacks. It prints no skill
   content.

Adding, renaming, or removing a skill, or changing its description, takes
effect at the next gateway restart. An edit to the content of an existing
skill is served at once.

**Webhook retry, root home only.** The `webhook-retry` plugin runs inside the
gateway. It records each webhook delivery, queues one whose agent turn fails on
a usage limit, and clears it when the turn finishes. The `retry-failed-webhooks`
cron job in part 12 reruns the queued deliveries. Install and enable the plugin
in the root home and never in the worker's home. `check-install` reports an
install in the worker home. It takes effect at the next gateway restart.

```sh
hermes plugins install "file://<repo>#plugins/webhook-retry" --enable --yes-deps </dev/null
```

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
An update keeps the installed `config.yaml`, so set the worker's wait limit
on a machine installed before the profile shipped it. `SOUL.md` waits on
`claude-session` with a 400 second timeout, which Hermes clamps to this value.
It stays under the 420 second limit Hermes puts on every tool call:

```sh
hermes -p claude-worker config set terminal.timeout 400 </dev/null
```

When the proxy needs a token, point the worker home at the gateway's copy.
`worker-secrets` asks the broker for the token that `auth_token_env` names and
prints it to Hermes, so the worker's own agent loop can reach the proxy. It
reads no Infisical folder and holds no identity. The broker answers in
milliseconds and the helper gives up after 30 seconds:

```sh
value="$(python3 -c 'import json, sys; print(json.dumps({"enabled": True, "helper_timeout_seconds": 40, "command": sys.argv[1]}))' "python3 <worker>/scripts/worker-secrets")"
hermes -p claude-worker config set secrets.command "$value" </dev/null
```

`check-install` reports a worker with a proxy token and any other
`secrets.command`, a worker without one that loads secrets, a
`terminal.env_passthrough` that is not empty, and each factory secret left in
`<worker>/.env`. A worker set up before it gave up its own identity follows
[Retire the worker's identity](references/infisical.md#retire-the-workers-identity).

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
  "triage": {"max_running": <n>},
  "claude_session": {"base_url": "", "model": "", "tier_models": {}, "auth_token_env": "", "mcp_servers": {}, "disabled_mcp_servers": [], "max_turns": 0}
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
`claude_session.mcp_servers` gives the factory its own identity on each
third-party MCP service, and `disabled_mcp_servers` lists the repository
servers a worker does not load, as [MCP server
identities](references/mcp-identities.md) describes. Both stay empty until
the operator creates the first identity. `check-install` reports each
repository server that a worker run does not see connected and that
`disabled_mcp_servers` does not list.

A card that waits in review leaves its Claude session idle past the 5-minute
prompt cache, and a resumed large session re-writes its whole context at the
cache-write price. `claude_session.fresh_after_tokens` sets the context size,
in tokens, at which `claude-session` starts a new session instead. It is a
positive integer and defaults to 100000. A run within 5 minutes of the
session's last write always resumes. A later run resumes when the last
context is under the setting, and otherwise starts a fresh session whose
prompt names the branch, its commit, the previous transcript, and the previous
run's last message. The run's JSON line reports `reason`: `new`, `warm`,
`small`, `fresh-large-idle`, or `fresh-unreadable`. Leave the key out unless the
default needs tuning.

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

`triage.max_running` caps how many triage cards run at once. It is a positive
integer from the operator's input, and its value belongs in the private
record. Hermes caps workers per host and per profile, but has no per-board
cap, so the factory applies this one itself: triage creates each triage card
with no assignee, and the `dispatch-triage` cron job from part 12 assigns
waiting cards only while fewer than `max_running` run or wait assigned.

`kanban_url` is the dashboard's
Kanban page; triage links it from the "Queued" activity in Linear. Leave it
empty to post no link. Set `github.login` to the App's
`[bot]` login. It is required: `render.py` rejects a value that does not end
in `[bot]`, and `claude-session` refuses a run that checks a branch for
adoption without one.

**Check** both homes:

```sh
python3 <repo>/skills/setup-factory/scripts/check-settings <settings.json>
```

It exits `0` and prints nothing when both homes match. Otherwise it prints one
line per home and differing key, such as
`claude-worker: claude_session.model differs`, and never a value. The installed
settings can hold `${...}` references, so do not compare them by printing
them. Without the argument, it compares the worker home with the
root home, which is how a deploy runs it.

**Set** the settings in both homes when the check prints a line:

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
hermes kanban boards create triage --name Triage --default-workdir <path of the default repository>
```

The `triage` board holds only triage cards. Every issue that lands in the
Linear Triage inbox gets one there, delegated or not, and its worker runs the
readiness check. The gateway's dispatcher serves every board that is not
archived on each tick, so the board needs no restart. Its worktrees anchor at
the same default repository. `boards create` leaves an existing board as it
is, so when the board exists, run
`hermes kanban boards set-default-workdir triage <path of the default repository>`
instead. An implementation card starts only when its Linear issue carries
`ready-for-agent`, which triage applies.

A triage card follows patina-mode's triage playbook from the factory checkout
that the worker home was installed from, the checkout that
`check-install --print-source` prints. That checkout vendors it as
`.agents/skills/patina-mode/playbooks/triage.md`, so a triage card follows the
same playbook in every repository, whatever that repository vendors.
`claude-session` refuses a triage card when the file is missing, and
`check-install` reports it. Update the checkout to the factory's default
branch to restore it.

A worker hands a card with a merge-ready pull request to the `review` lane.
With `kanban.review_dispatch` set to `false`, Hermes spawns no agent reviewer,
so the card waits there for a person. GitHub triage moves it to `done` when the
pull request merges, from whatever status the card is in unless a worker is running it, and back to the worker when a review requests changes.
Cards in `review` run no worker, so they do not count against
`kanban.max_in_progress`.

Hermes moves a card that blocks twice for the same reason to its `triage`
column, and by default its decomposer then splits the card into child cards.
A factory card is one Linear issue, and its children would lack the key block
the worker needs, so turn the decomposer off. A looping card then waits in
`triage` for a person, and a reply on its Linear issue, a changes-requested
review, or an actionable comment on its pull request resumes it.

Leave `kanban.default_assignee` unset. The resume steps in the triage prompt
unassign a card so the dispatcher skips it while `kanban specify` rewrites it,
and a default assignee would route the unassigned card to a worker first.

**Check.** `hermes config get kanban.review_dispatch` and
`hermes config get kanban.auto_decompose` each print `false`, and
`hermes config get kanban.default_assignee` prints nothing or an empty string.

`default_workdir` is board metadata, so `hermes config get` does not show it.
`hermes kanban boards list --json` lists the `default` and `triage` boards,
each with the default repository's path as its `default_workdir`.
`check-install` reports a missing `triage` board, another `default_workdir`,
a default repository without the triage playbook, and a `triage.max_running`
that is not a positive integer or differs between the homes.

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
jobs: `refresh-checkouts`, `refresh-owned-refs`, `reopen-conflicts`, and
`refresh-gh-app-login`, and `dispatch-triage`.
A copy there runs without the worker's scripts and settings, so remove it the
same way. The default home holds two jobs, `check-install` and
`retry-failed-webhooks`, described below. A second setup session runs this check before it
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
`github_issue_url` of every card not archived, in one atomic step. It also
writes each card's `linear_issue_id`, a `done` card included. The `linear`
route keeps a completed or canceled `Issue` update only when its issue id is
listed, so triage never starts for a closed issue that has no card. Without the
file the route drops every GitHub event except an `issue_comment` on the
App's pull request, and every completed or canceled `Issue` update. `check-install` reports the file when it
is missing or older than 5 minutes. Create the job when `cron list` has no
`refresh-owned-refs` job:

```sh
hermes -p claude-worker cron create "every 1m" --name refresh-owned-refs --no-agent --script refresh-owned-refs </dev/null
```

The same job writes `<home>/triage-refs.json` with the `linear_issue_id` of
every card on the `triage` board. The `linear` route keeps a status change of
an issue listed there, so a triage card is archived when a person moves its
issue out of Triage or closes it. `check-install` reports this file the same
way.

**Triage dispatch job.** Triage creates triage cards with no assignee, so the
dispatcher skips them. `dispatch-triage` runs every minute. It assigns
`claude-worker` to waiting triage cards, highest priority and then oldest
first, while fewer than `triage.max_running` cards on the `triage` board run or
wait assigned. A reply that resumes a blocked triage card also leaves it
unassigned, so this job is the only one that starts triage work. Create the
job when `cron list` has no `dispatch-triage` job:

```sh
hermes -p claude-worker cron create "every 1m" --name dispatch-triage --no-agent --script dispatch-triage </dev/null
```

**Conflict job.** GitHub sends no webhook when a pull request starts to
conflict because its base branch moved. `reopen-conflicts` lists the App's
open pull requests for every `review` or `blocked` card every 5 minutes. For
each one GitHub reports as `CONFLICTING`, it comments on the card and hands
the card back to its worker. The worker merges the base branch, resolves the
conflicts, and publishes the merge with `push-signed`. A card is sent back
once per pull request head, so a worker that blocks on the same head stays
blocked. Create the job when `cron list` has no `reopen-conflicts` job:

```sh
hermes -p claude-worker cron create "every 5m" --name reopen-conflicts --no-agent --script reopen-conflicts </dev/null
```

**Install drift job.** `check-install` compares the live worker profile, the
factory plugins in both homes, and the installed webhook routes with
`origin/HEAD` of the checkout the profile was installed from. It also checks
that the dashboard's OIDC sign-in can keep a user signed in. It also reports
machine changes this runbook cannot reproduce: a `config.yaml` key from parts
4, 6, 10, or 13 that differs from what the part sets, a cron job that is
missing, duplicated, changed, not created by the runbook, or in the wrong
profile, a cron job that delivers to a platform with no home channel in its
profile, a cron job whose last run failed or was not delivered, with the first
line of its error and secrets masked (a failed run of the `check-install` job
itself is not reported, because that failure is the previous run's report), the
root home's `scripts/check-install-alert` and `scripts/retry-failed-webhooks`
when one is missing or differs from the repository, and, on macOS, a launchd service from part 7 or 15
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

**Webhook retry job.** The `webhook-retry` plugin from part 5 keeps a queue in
`<root>/webhook-retry/`. A delivery whose triage turn fails on a usage limit
waits there until the limit resets. This job reruns the due deliveries every 10
minutes, oldest first and one at a time, and stops at the first usage limit.
It prints a line only when something happened, so Hermes delivers nothing on a
quiet run. It belongs to the root profile for the same reason as
`check-install`. Install the wrapper and create the job when `hermes cron list`
for the root profile has no `retry-failed-webhooks` job:

```sh
install -m 0644 skills/setup-factory/scripts/retry-failed-webhooks <root>/scripts/retry-failed-webhooks
hermes cron create "every 10m" --name retry-failed-webhooks --no-agent --script retry-failed-webhooks --deliver <platform> </dev/null
```

To inspect the queue, run the command from the root home:
`HERMES_HOME=<root> python3 <root>/plugins/webhook-retry/retry.py --list`
prints one line per delivery and never its prompt. Add `--now` to retry every
queued delivery at once, or `--drop <delivery>` to remove one. A delivery that
is older than 48 hours or has failed 8 retries is given up and stays listed
until dropped. A retry that a crash or timeout cut short is marked failed and
never run again, because its turn may already have changed a card.

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
- `hermes gateway restart` waits for chat work and in-process cron jobs, and
  the gateway refuses webhooks while it waits. It does not wait for Kanban
  workers. They run inside the gateway's launchd job, so a bare restart kills
  every running card. Restart only with `restart-gateway`, which waits until
  no board has a `running` card, then holds new cards with `hermes pause`
  while it restarts, and lifts the pause afterwards. When the wait expires, it
  names the running cards, exits 1, and does not restart. `--timeout` sets
  the wait in seconds, 3600 by default. An operator's own `hermes pause` stays
  in place. Restart when no long cron job is running.

> **HUMAN CHECKPOINT.** Ask the operator to run
> `python3 <repo>/skills/setup-factory/scripts/restart-gateway` from a shell outside Hermes, not from a Hermes session or tool.
> When it exits 1, wait for the named cards or ask the operator whether to
> run it again with a longer `--timeout`.

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

**Confirm a delegation became a card.** Cards live in the worker home, so
list them with that home as `HERMES_HOME`. Replace `PAT-123` with the issue's
identifier:

```sh
HERMES_HOME=<worker> hermes kanban list --json 2>/dev/null \
  | python3 -c 'import json, sys; [print(t["id"], t["status"], t["title"]) for t in json.load(sys.stdin) if t["title"].startswith(sys.argv[1] + ":")]' PAT-123
```

Each line is one card: its ID, its status, such as `ready`, `running`,
`review`, or `blocked`, and its title. No line means no card exists yet. Wait
a minute and run it again, then read the gateway log for the delivery. An
archived card appears only with `--archived`. The command's secret helper
writes a status line to standard error, so the command discards it to keep
the JSON clean.

**End-to-end checks.** Use a test issue in the operator's Linear workspace for
each check. Report each result.

1. Delegate an issue that routes to the `default` repository and carries
   `ready-for-agent`. Linear shows the acknowledgement within 10 seconds, then
   the "queued" activity. The new card has that repository as its
   `completion_contract`. When the issue was in Triage, it moves to Todo.
2. Delegate a `ready-for-agent` issue synced from a `synced_github` repository. Its card has that
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
10. Un-delegating stops the worker and archives the card, whatever its
    status, so `hermes kanban list --json` no longer shows it. Delegating again
    creates a new card that continues on the same branch and pull request, and
    `git -C <repo> worktree add` can check that branch out even when the
    archived card kept a dirty worktree.
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
    the issue's `gitBranchName` and no pull request. Triage creates no card
    and posts a `thought` that tells the person to open a pull request from
    the branch or to rename or delete it. The person's branch is unchanged.
16. Open a pull request as the GitHub App on a branch other than the issue's
    `gitBranchName`, link it to the issue, and delegate the issue. The card's
    branch is that pull request's head branch, and no second pull request
    opens.
17. Open a pull request as a person, link it to the issue, and delegate the
    issue. The card body carries `takeover_pr`, the worker starts from that
    pull request's head, and its own pull request links the original. Once that
    pull request is open, the run's `claude-session` line reports `superseded`
    with `action` `closed`, the original is closed with a comment that links
    the replacement, and the replacement's description ends with
    `Supersedes #<number>`. If the person pushed commits that the replacement lacks
    after it opened, whatever the commits' dates, or built on the replacement's
    head, or opened the original from a fork, the line reports `left` with
    `person pushed after takeover` and the original stays open. The original's
    branch is unchanged. Run the card again and the line reports `left`, with
    no second comment.
18. Once a card's pull request is open, send a Linear `stop`, then reply in
    the same agent session. In `hermes kanban show <task id> --json`, the
    events after `blocked` are `unblocked`, `review_requested`, and
    `review_reopened`. The next dispatcher tick starts a worker, no
    `respawn_guarded` event with reason `active_pr` follows, and the card
    completes without operator action.
19. Move a card to `triage`, for example by letting it block twice for the same
    reason, then reply in its Linear agent session. In
    `hermes kanban show <task id> --json`, the events after the reply are
    `assigned` with no assignee, `specified`, `edited`, and `assigned` to
    `claude-worker`. The card's title and body are unchanged, the next
    dispatcher tick starts a worker, and no `respawn_guarded` event follows
    the last `assigned` event. Run it once with a pull request open and once
    without.
20. Move a card to `triage` with its pull request open, then submit a
    `changes_requested` review on the pull request as a person. In
    `hermes kanban show <task id> --json`, the events after the review are the
    same as in check 19: `assigned` with no assignee, `specified`, `edited`,
    and `assigned` to `claude-worker`, with the title and body unchanged. A
    card in `review` still gets `review_reopened`. Repeat with an `approved`
    review from a person: the events are the same, and the worker hands the
    approved pull request off. An approval from a bot, or on a card in any
    other status, adds only the card comment.
21. Merge a card's pull request while the card is in `triage`, with no worker
    running it. In `hermes kanban show <task id> --json`, the events after the
    merge include, in this order, `assigned` with no assignee, `specified`,
    `edited`, and `completed`. The card is `done`, its title and body are unchanged, and the
    Linear agent session shows a `response` that the pull request merged. Repeat
    with a card in `todo` (its events include `promoted_manual`, then `completed`) and in
    `blocked` (its events include `completed`). A card with a worker running stays `running`
    until its worker completes it, and a pull request closed without merging
    changes no card. A refused promote or completion after `specify` leaves the
    card unassigned, with a comment that names the pull request, and no
    `assigned` event to `claude-worker` follows it.

22. Block a card with `kanban_block` kind `needs_input`, then block a second
    card twice in a row for the same reason so Hermes moves it to `triage`.
    The first card's Linear agent session gets an `elicitation` that says the
    card is blocked, and the second's gets an `elicitation` that says it is
    parked in triage, each with the reason and "Reply here to resume it". A
    `transient` block posts nothing. The `linear-agent-session` plugin does
    this from Hermes' `kanban_task_blocked` hook, so both Hermes homes need
    the plugin at the current commit, and the gateway needs a restart after a
    reinstall. A failed post appears only in the log of the process that
    blocked the card, as `reporting the block of card <id> failed`.

23. Move a card to `blocked` with its pull request open, then submit a
    `changes_requested` review on the pull request as a person. In
    `hermes kanban show <task id> --json`, the events after the review are
    `unblocked`, `review_requested`, and `review_reopened`. The card's
    comments show the review before those events, the next dispatcher tick
    starts a worker, and no `respawn_guarded` event with reason `active_pr`
    follows. Repeat with an actionable inline comment from a person. A review
    or comment from a bot, an approval, a thanks, and a bare question leave the
    card `blocked`.

24. Merge a card's pull request while Hermes still holds a failed check run
    against it, with the card in `review` and its Linear issue In Progress.
    First merge past a check whose newest run failed. The card's comments
    include `Merge refused by Hermes: required checks failed:` with the check
    names, the card is unassigned and `blocked` with block kind `needs_input` (a
    `review` card stays in `review`, unassigned),
    and the Linear agent session gets one `elicitation` that says the pull
    request merged and asks whether the issue is done. A reply in the session
    leaves the card `blocked`. Mark the Linear issue Done, and the card is
    archived. Then repeat with a check that failed once and passed on a newer
    run (re-run it before the merge). The comment is
    `Merge refused by Hermes: Hermes counted failed runs that newer passing runs replaced:`
    and the elicitation says the checks passed on their newest runs. Repeat
    either case with the issue already Done before the merge: the card is
    archived with the same comment, and the session gets no post. Then merge a
    pull request into a base branch with no required checks, so Hermes refuses
    it as `missing`. The comment is
    `Merge refused by Hermes: the base branch has no required checks.`, and the
    elicitation asks whether the issue is done. Repeat it with the issue
    already Done before the merge, and with the issue marked Done after the
    refusal: the card is archived both times. In every case the card stays
    unassigned.

25. Create an issue in the team's Triage status. Within a minute the
    `triage` board has one card for it, with no assignee until
    `dispatch-triage` assigns it, and Linear shows a "Queued for triage"
    activity on an agent session the factory opened, and no other activity on
    that session. Edit the issue's title: no second card appears. Move a
    second issue into Triage from Backlog: it gets its own card. Move the
    first issue out of Triage: its triage card is archived. Label the second
    issue `ready-for-agent` and delegate it to the agent while it is still in
    Triage: it gets an implementation card and moves to Todo.
26. Delegate an issue without `ready-for-agent`. No implementation card
    appears, and the session gets a `thought` that the work starts once the
    issue passes the readiness check. Delegate an issue with
    `ready-for-human`: no card appears, and the session gets a `response`.
    Mark the first issue `ready-for-agent`: the `Issue` webhook creates its
    implementation card on the same session and moves the issue to Todo when
    it is in Triage. Start a card by hand on an issue without the label:
    `claude-session` refuses it before Claude starts.
27. Let a triage card block on a question. Its session gets one
    `elicitation` that starts with the AI-generated disclaimer, and one reply
    in the session's thread mentions each person the questions name. Each of
    them gets one Linear notification. A reply from a teammate
    who did not delegate the issue raises `prompted` on the session the
    factory created, unblocks the card, and `dispatch-triage` assigns it
    again.
28. No more than `triage.max_running` cards on the `triage` board run at
    once, and the triage card's run never pushes a branch or opens a pull
    request.
29. Delegate two throwaway issues in a repository that has no real work
    queued, where B is blocked by A in Linear. Delegate A, then B. Both
    cards are created with `claude-worker` as the assignee. A's worker
    starts Claude. B's worker run prints `{"waiting_on": [<A card>]}` and
    starts no Claude run. `hermes kanban show <B card> --json` lists A's card
    as a parent, and B is in `todo` after a `dependency_wait` event. Merge A's
    pull request: A's card is `done`, B's card moves to `ready` through a
    `promoted` event, and B's worker branches from a default branch that
    holds A's merge. Delegate them again, B and then A within a minute: B's
    run waits up to 5 minutes for A's card, then waits on it. An issue whose
    blocker is not delegated runs at once, and its worker's blocker gate
    decides. For two issues that block each other, the second card's run
    links nothing, its session gets one `thought` naming the cycle, and its
    Claude starts.

**Known limitation.** Only the Kanban dashboard reopens a `done` card. The
operator drags the card to Ready, which Hermes documents as a deliberate
re-queue. Hermes v0.21.6 has no CLI command or tool for it. When a GitHub event
arrives for a done card, triage comments on the card and asks the operator to
reopen the card from the Kanban dashboard. A card reaches `done` only when its
pull request merges, so this applies to follow-up work after the merge. A
`triage` card needs no dashboard: its Linear reply, a changes-requested review, or an actionable pull request
comment unassigns the card, runs
`specify`, restores the title and body, and assigns the card again.

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

A merged change to `skills/setup-factory/scripts/factory-secrets` is live only
after the render steps in
[Configure both homes](references/infisical.md#configure-both-homes) install
it at `<home>/bin/factory-secrets`. Hermes runs the new helper the next time a
Hermes process starts.

1. Find the source checkout. It is the checkout `<worker>/distribution.yaml`
   points at, and this command prints it:

   ```sh
   HERMES_HOME=<worker> python3 <worker>/scripts/check-install --print-source
   ```

   Fast-forward only that checkout to `origin/HEAD`. It must be on the default
   branch with no uncommitted changes. Never touch the worker-owned
   checkouts, which are the configured repository checkouts the worker profile
   uses. They change only through a Kanban card or `refresh-checkouts`.
2. List what is out of date. Run the copy in the source checkout, not the
   installed one. The installed copy predates the change, so it cannot report
   a board, config key, or cron job that the change adds:

   ```sh
   HERMES_HOME=<worker> python3 <source checkout>/profiles/claude-worker/scripts/check-install
   ```

3. Install each item it names in this order: the part 10 `kanban board` and
   `config` lines, then the profile, then the part 12 `cron job` lines, and
   the route last. The new profile's cron jobs read the boards and settings on
   their first run, one minute after the profile update. `refresh-owned-refs`
   lists the `triage` board and fails on each run until the board exists.
   `dispatch-triage` reads `triage.max_running`. A cron job runs a profile
   script, so create a missing job only after the profile update installs its
   script. The `linear` route drops a completed or canceled `Issue` update unless its
   issue id is in `<root>/owned-refs.json`. Update the profile, run
   `refresh-owned-refs` once, confirm the file holds issue UUIDs, and only then
   re-render the route. Find the job id with `hermes -p claude-worker cron list`
   and run it with `hermes -p claude-worker cron run <job id>`, or wait one
   minute for the cron schedule. An empty board (`[]`) is fine: it holds no
   cards to close. The reverse order drops every closure until the next
   cron run. `check-install` reports a route that filters on issue ids while
   the file holds none.
   - A `kanban board <name>` line: run the part 10 Kanban commands before the
     profile update. For a missing `triage` board, run
     `hermes kanban boards create triage --name Triage --default-workdir <path of the default repository>`.
     When the line names another `default_workdir`, run
     `hermes kanban boards set-default-workdir <name> <path of the default repository>`.
   - A `config plugins.entries.linear-agent-session.settings.triage.max_running`
     line: add `"triage": {"max_running": <n>}` to the factory settings and set
     them in both homes as in part 10, before the profile update.
   - A `profile claude-worker` line: `hermes profile update claude-worker -y </dev/null`.
   - A `plugin <name> in <home>` line: run the part 5 install command for that
     home with `--force`.
   - A `team skills` line: the team skills directory is missing, empty, or
     lacks a skill that `allow` names. Fix the directory or the part 5
     setting.
   - A `webhook config <key>` line: run the part 11 install for that key.
   - A `webhook route <name>: <n> rejected with HTTP 429` line: the gateway
     dropped deliveries in the last hour. Check that
     `platforms.webhook.extra.rate_limit` is installed. If it is, show the
     operator the count, and open an issue to raise `RATE_LIMIT` in
     `templates/render.py`.
   - An `owned refs` line: create the part 12 `refresh-owned-refs` job, or
     run it with `cron run` when it exists.
   - A `source checkout` line: the source checkout is not at `origin/HEAD`.
     When the line gives a `git merge --ff-only` command, run it as written,
     then run `check-install` again. Otherwise the checkout is ahead, diverged,
     on another branch, or dirty. Fix that by hand before you deploy.
   - `plugin` lines for the root home's `factory-uploads` or
     `linear-agent-session` together with a `plugin factory-secret-names in
     <worker>` line: the worker is moving to the gateway's broker for its
     tokens. Two orders break every worker run. A worker on the new scripts
     before the gateway runs the new broker gets each token request refused.
     The worker's `factory-secret-names` before its new scripts makes the old
     `claude-session` lose `CLAUDE_PROXY_TOKEN` and refuse the run. Deploy in
     this order. First, install the root home's plugins and run
     `restart-gateway`, then confirm that `{"op":"status"}` reports
     `linear_ready` and `proxy_token_present` true. Second, run `hermes pause`.
     Update the profile, install the worker's `linear-agent-session` and
     `factory-secret-names`, and set the worker's `terminal.env_passthrough`
     to `[]`, all together. Third, retire the worker's identity as the next
     line says, run `check-install` until it is clean, and then run
     `hermes resume`.
   - A `secret <name> in <worker>/.env` line: the worker holds a secret only
     the gateway may hold. Follow
     [Retire the worker's identity](references/infisical.md#retire-the-workers-identity),
     which also applies to a factory without Infisical.
   - A `broker user` line that says a file under `lib` differs: the broker
     runs a root-owned copy of its code, so a deploy cannot change it. Print
     the plan as in step 4 of "Broker user, macOS" in part 5, and ask the
     operator to read the `git diff` that step 5 shows and then run its
     clone and `apply` lines at the deployed commit. Other `broker user` lines name the step of that section to repeat.
   - A `checkout` line: the checkout is missing or has no `origin/HEAD`. Clone
     it as in part 10, or run `git -C <path> remote set-head origin -a`.
   - A `dashboard sign-in` line: follow step 6 of
     [`references/cloudflare-sst.md`](references/cloudflare-sst.md#6-configure-the-dashboard-sign-in),
     or allow the `refresh_tokens` grant on the `saas` Access application when
     the line names the issuer. Then restart the dashboard service.
   - A `script <root>/scripts/check-install-alert` or
     `<root>/scripts/retry-failed-webhooks` line: install the wrapper again
     with the part 12 `install` commands, which create `<root>/scripts` first.
   - A `cron job retry-failed-webhooks` line: create the job in the root
     profile as in part 12.
   - A `cron job dispatch-triage` line: after the profile update, create the
     job with the part 12 `cron create` command.
   - A `cron job check-install` line: the job is missing, in the wrong
     profile, or delivers to a platform with no home channel. Create it in the
     root profile as in part 12, and remove a leftover copy in the worker
     profile. Set the platform's home channel in the root home as in part 13.
   - A `config`, `cron job`, or `service` line: the machine differs from what
     the named part produces. If the runbook is right, run that part again.
     If the machine is right, change the part and its row in `check-install`
     in one pull request, as the replicability rule in `AGENTS.md` requires.
4. Run `check-install` again. It prints nothing.
5. Check that the worker home's factory settings match the root home's:

   ```sh
   python3 <source checkout>/skills/setup-factory/scripts/check-settings
   ```

   With no argument, it uses the root home's installed settings as the
   reference, so a deploy needs no settings file and no `--raw` read. It
   exits `0` and prints nothing when the homes match. Otherwise it prints one
   line per differing key, such as
   `claude-worker: claude_session.model differs`, and never a value. It does
   not prove that the root home holds what the operator intended; part 10's
   check against the settings file does. When it prints a line, find which
   home is right with the operator, and set that home's settings in the other
   as in part 10.
6. If step 3 changed a plugin, a webhook config key, or a team skills setting:

   > **HUMAN CHECKPOINT.** Ask the operator to restart the gateway with
   > `python3 <repo>/skills/setup-factory/scripts/restart-gateway` from a shell outside Hermes, as part 15 describes. Then confirm
   > that
   > `~/.hermes/logs/gateway.log` shows a startup after the install.

7. Record the deployed commit in the private configuration record.

## Report

End with one line per part: already configured, configured now, or blocked.
For a blocked part, name the failed check, the output, and the action the
operator must take.
