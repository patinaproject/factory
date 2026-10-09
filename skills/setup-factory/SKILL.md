---
name: setup-factory
description: Set up or re-check a Hermes software factory on a machine from this repository. Installs and verifies Hermes, the Claude Code CLI and transport, the factory plugins and claude-worker profile, the Linear agent app, the factory's GitHub App and its webhook, the webhook routes, Slack, an optional Infisical secret source, and Cloudflare Tunnel and Access ingress through the operator's SST app. Use for "set up a factory", "install the factory on this machine", "re-run factory setup", or checking that an existing factory is configured correctly.
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
- **Run Hermes commands without a prompt.** Pass `-y` or `--yes-deps` where the
  command takes one, and redirect standard input from `/dev/null`.

A HUMAN CHECKPOINT looks like this in the parts below:

> **HUMAN CHECKPOINT.** What the operator does, and what you check afterwards.

At a checkpoint, tell the operator exactly what to do, wait for them to say it
is done, then run the check.

## Collect the inputs

Ask the operator for these values before you start. Record them in your task
notes, not in the repository.

| Input | Used by |
| --- | --- |
| Hermes home directory (default `~/.hermes`) and install checkout | Every part |
| Hermes release tag to pin, `v0.21.4` or later | Hermes |
| Model alias for Hermes' own agent loop, for example `sonnet` | DirectSDK provider |
| Repositories: for each, `full_name`, local `path`, and `routing` (`default` for exactly one, `synced_github` for the rest), and an optional `worker_entry` | Settings, clones, routes |
| `kanban.max_in_progress` | Kanban |
| Factory hostname, Cloudflare account, and zone | Ingress |
| Identities allowed through Access, for example an email domain, and the session length | Ingress |
| The operator's SST app, its stage, and its review process | Ingress |
| Whether Claude Code uses a local proxy, and if so its base URL, model alias, and token variable name | Claude Code transport |
| Linear workspace, and whether the operator already has an agent app | Linear app |
| The GitHub organization or account that owns the repositories, and a name for the factory's GitHub App | GitHub App |
| A path outside every repository for the GitHub App's private key, for example `~/.config/factory/github-app.pem` | GitHub App |
| Whether secrets live in Infisical, and if so the project ID, environment, folder, and machine identity | Infisical |
| Slack workspace and the channels the agent joins | Slack |

## Where each value lives

`<home>` is the default Hermes home. `<worker>` is the `claude-worker` profile
home, `<home>/profiles/claude-worker`. Each one has its own `config.yaml`,
`.env`, `plugins/`, and `scripts/`.

| Value | Location |
| --- | --- |
| `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` | `<home>/.env` and `<worker>/.env` |
| `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` | `<home>/.env` and `<worker>/.env` |
| `LINEAR_WEBHOOK_SECRET`, `GITHUB_WEBHOOK_SECRET` | `<home>/.env` |
| `GH_CONFIG_DIR` | `<worker>/.env`. The directory holds the worker's `gh` login as the GitHub App, which `refresh-gh-app-login` renews |
| GitHub App private key | The file that `github.private_key_path` names, mode `0600`, outside every repository |
| The token variable that `claude_session.auth_token_env` names | `<worker>/.env`. Never name it `ANTHROPIC_*` |
| Dashboard OIDC client secret | `<home>/.env` |
| Cloudflare tunnel token | The `cloudflared` system service only |
| `CLOUDFLARE_API_TOKEN` | The SST app's own secret storage or environment |
| `plugins.entries.linear-agent-session.settings` | `config.yaml` of both homes, same JSON |
| `plugins.entries.pr-ready-gate.settings` | `config.yaml` of both homes |
| `kanban.max_in_progress`, `platforms.webhook.*`, `dashboard.oauth.self_hosted` | `<home>/config.yaml` |
| The `default` board's `default_workdir` | Kanban board metadata, not `config.yaml` |

When the factory uses Infisical, part 14 moves the Linear credentials, both
webhook secrets, and the proxy token from `.env` into one Infisical folder.
Earlier parts still write them to `.env` first.

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
4. [DirectSDK model provider](#4-directsdk-model-provider)
5. [Factory plugins and the worker profile](#5-factory-plugins-and-the-worker-profile)
6. [Webhook secrets and listener](#6-webhook-secrets-and-listener)
7. [Public ingress](#7-public-ingress)
8. [Linear agent app](#8-linear-agent-app)
9. [GitHub App](#9-github-app)
10. [Factory settings, repositories, and Kanban](#10-factory-settings-repositories-and-kanban)
11. [Webhook routes](#11-webhook-routes)
12. [Checkout refresh cron job](#12-checkout-refresh-cron-job)
13. [Slack](#13-slack)
14. [Infisical secret source](#14-infisical-secret-source), optional
15. [Restart and end-to-end checks](#15-restart-and-end-to-end-checks)

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

**Install.** If Hermes is absent, install it at the pinned tag with the
upstream installer from the Hermes README. Then run the check.

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

## 4. DirectSDK model provider

The provider runs Hermes' own agent loop on the Claude subscription. Configure
it in the default home and in the worker profile. Part 5 creates the worker
profile, so on a new machine run this part for `<home>` now and for the worker
at the end of part 5.

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

**Keep `ANTHROPIC_*` out of the gateway.** The provider refuses to start when
the environment holds `ANTHROPIC_AUTH_TOKEN` or another native override. Only
`claude-session` sets `ANTHROPIC_*` variables, and only for the Claude Code
process it starts. Each count must be `0`:

```sh
grep -c '^ANTHROPIC_' <home>/.env <worker>/.env
```

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

Both list `linear-agent-session` and `pr-ready-gate`, enabled. If a plugin is
listed and enabled, skip its install.

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
a new machine.

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
  "claude_session": {"base_url": "", "model": "", "auth_token_env": "", "max_turns": 40}
}
```

Exactly one repository has `routing: default`. Leave
`claude_session.base_url`, `model`, and `auth_token_env` empty unless part 3
set up a proxy. An empty
`worker_entry` uses the built-in prompt. `kanban_url` is the dashboard's
Kanban page; triage links it from the "Queued" activity in Linear. Leave it
empty to post no link.

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

**Repositories.** For each repository:

```sh
[ -d <path>/.git ] || gh repo clone <full_name> <path>
git -C <path> remote set-head origin -a
git -C <path> symbolic-ref refs/remotes/origin/HEAD   # refs/remotes/origin/<default branch>
git -C <path> status --porcelain --untracked-files=no # prints nothing
```

The main checkout stays on its default branch. Kanban creates worktrees under
`<path>/.worktrees/`.

**GitHub login and checkouts.** Return to
[`references/github.md`](references/github.md) and run its
"Sign the worker's `gh` in" and "Configure git in each checkout" sections.
They need the settings and the clones from this part.

**Kanban.**

```sh
hermes config set kanban.max_in_progress <n>
hermes kanban boards set-default-workdir default <path of the default repository>
```

`default_workdir` is board metadata, so `hermes config get` does not show it.
Read the board metadata with `hermes kanban boards --help` to find the listing
command, and confirm the path.

## 11. Webhook routes

`templates/render.py` builds the `linear` and `github` routes from the settings
in part 10. [`templates/README.md`](../../templates/README.md) describes the
output.

**Check.** Render the routes, then compare each with the installed one:

```sh
python3 <repo>/templates/render.py > <scratch>/routes.json
for name in $(python3 -c 'import json, sys; print(*json.load(sys.stdin))' < <scratch>/routes.json); do
  diff <(hermes config get "platforms.webhook.extra.routes.$name" --json --raw | python3 -m json.tool --sort-keys) \
       <(python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]], sort_keys=True, indent=4))' "$name" < <scratch>/routes.json)
done
```

**Install** each route whose diff is not empty:

```sh
hermes config set "platforms.webhook.extra.routes.$name" \
  "$(python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]]))' "$name" < <scratch>/routes.json)"
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

## 12. Checkout refresh cron job

The script lives in the worker profile's `scripts/` directory, so the job
belongs to the worker profile.

**Check.** `hermes -p claude-worker cron list` shows exactly one job named
`refresh-checkouts`. `cron create` does not remove duplicates. If more than one
exists, show the list to the operator and remove the extras with their
approval.

**Create** the job only when the list has none:

```sh
hermes -p claude-worker cron create "every 1h" --name refresh-checkouts --no-agent --script refresh-checkouts </dev/null
```

Hermes runs a script-only cron job's script with Python, whatever its
shebang says, so every script a `--no-agent` job runs must be Python. A shell
script fails on every tick with a `SyntaxError`.

**Verify.** Run the job through Hermes once, then read its status:

```sh
hermes -p claude-worker cron run <job id>
hermes -p claude-worker cron list    # Last run shows no error
```

It prints nothing when every checkout is current. It prints one line for each
checkout it left alone, such as a checkout on another branch or with
uncommitted changes. Fix each problem it names.

## 13. Slack

Follow [`references/slack.md`](references/slack.md).

## 14. Infisical secret source

Skip this part when the operator does not use Infisical. Otherwise follow
[`references/infisical.md`](references/infisical.md).

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

**External checks.** Run the checks at the end of
[`references/cloudflare-sst.md`](references/cloudflare-sst.md).

**End-to-end checks.** Use a test issue in the operator's Linear workspace for
each check. Report each result.

1. Delegate an issue that routes to the `default` repository. Linear shows the
   acknowledgement within 10 seconds, then the "queued" activity. The card has
   that repository as its `completion_contract`.
2. Delegate an issue synced from a `synced_github` repository. Its card has that
   repository's contract, and its worktree starts from that repository's
   default branch.
3. The worker moves the issue to In Progress through the repository's own
   issue-start workflow. Hermes makes no status change.
4. While the pull request is a draft, has a pending or failing non-required
   check, has a conflict, or is `BLOCKED` or `BEHIND`, `kanban_complete` is
   refused. `hermes kanban complete` from the worker's shell is also refused.
5. A `CLEAN`, non-draft pull request completes the card. The final `response`
   with the pull request link appears in the same Linear session.
6. A reply in Linear during a run is picked up when the run ends, and the next
   run resumes the same Claude session ID.
7. A Linear `stop` stops the worker and blocks the card.
8. Un-delegating stops the worker and blocks the card with reason
   `undelegated`. Delegating again unblocks the same card, which resumes the
   same branch, worktree, and session.
9. A mention without delegation gets an answer and creates no card.
10. No more than `kanban.max_in_progress` workers run at once, and no `claude`
    process runs outside a Kanban worker.
11. `git -C <install> status --porcelain` still prints nothing, and `HEAD` is
    still the pinned tag.
12. `git -C <repo> status --porcelain` prints nothing. This repository holds no
    factory value.

**Known limitation.** Only the Kanban dashboard reopens a `done` or `triage`
card. The operator drags the card to Ready, which Hermes documents as a
deliberate re-queue. Hermes v0.21.5 has no CLI command or tool for it. When a
GitHub event arrives for a done card, triage comments on the card and asks the
operator to reopen the card from the Kanban dashboard. A failed check after
completion therefore waits for the operator before the worker resumes.

## Report

End with one line per part: already configured, configured now, or blocked.
For a blocked part, name the failed check, the output, and the action the
operator must take.
