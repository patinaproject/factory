# GitHub account and webhooks

The worker commits, pushes, and opens ready pull requests as a dedicated
GitHub account that belongs to the factory. Each configured repository sends
the GitHub webhook events that the `github` route triages.

Hermes documents this pattern in `website/docs/user-guide/features/kanban.md`
of the Hermes install. Kanban acceptance reads as the assignee profile's own
`gh` login, which `GH_TOKEN` or `GH_CONFIG_DIR` in that profile's `.env`
selects. On a host with more than one profile, sign `gh` in once per profile,
with one GitHub identity per organization.

Hermes removes `GH_TOKEN`, `GITHUB_TOKEN`, and every GitHub App credential
variable from each subprocess that a worker starts: the terminal,
`claude-session`, and Claude Code. It passes `GH_CONFIG_DIR`. The factory
therefore keeps the worker's `gh` login in the directory that `GH_CONFIG_DIR`
names. A `GH_TOKEN` alone never reaches the worker's commands.

`pr-ready-gate` also runs `gh pr view` inside the worker profile, so the gate
needs the same login.

## Check

`<login>` is the factory account's GitHub login. In the
[interim setup](#create-the-factory-account), read `<worker>/gh` as the
operator's login directory and `<login>` as the operator's login. All of these
must hold:

- `grep '^GH_CONFIG_DIR=' <worker>/.env` prints `GH_CONFIG_DIR=<worker>/gh`.
- `GH_CONFIG_DIR=<worker>/gh gh api user --jq .login` prints `<login>`.
- For each configured repository,
  `GH_CONFIG_DIR=<worker>/gh gh repo view <full_name> --json viewerPermission --jq .viewerPermission`
  prints `WRITE`, `MAINTAIN`, or `ADMIN`.
- For each repository's main checkout,
  `git -C <path> config --local --get-all credential.https://github.com.helper`
  prints an empty line and then `!gh auth git-credential`.
- For each main checkout, `git -C <path> config --local user.name` and
  `git -C <path> config --local user.email` print the factory account's name
  and email address.
- `github.login` in the settings of both homes is `<login>`. In the interim
  setup below, it is empty.
- `env_has <home>/.env GITHUB_WEBHOOK_SECRET` prints `1`.
- Each configured repository has an active webhook for the factory (see
  [Verify](#verify)). Reading webhooks needs the admin access described in
  [Create the webhooks](#create-the-webhooks).

If all of them hold, skip to the live test.

## Create the factory account

> **HUMAN CHECKPOINT.** Ask the operator to create a GitHub account for the
> factory and to give it write access to each configured repository, for
> example as an organization member in a team with the Write role. You never
> create accounts. Record the account's login for `github.login` in part 10.

**Interim setup.** Until the dedicated account exists, the operator can point
`GH_CONFIG_DIR` at a directory where they already signed `gh` in, such as
`~/.config/gh`. The worker then acts as the operator. In this setup, leave
`github.login` empty. The `github` route drops every event whose
`sender.login` equals `github.login`, so the operator's login there would drop
the operator's own reviews and comments. Move to the dedicated account later
by repeating this reference.

## Sign the worker's `gh` in

Point the worker profile at its own `gh` configuration directory:

```sh
mkdir -p <worker>/gh
chmod 700 <worker>/gh
printf '%s' <worker>/gh | env_set <worker>/.env GH_CONFIG_DIR
```

> **HUMAN CHECKPOINT.** Ask the operator to sign in as the factory account
> from their own terminal:
>
> ```sh
> GH_CONFIG_DIR=<worker>/gh gh auth login --hostname github.com --git-protocol https --web
> ```
>
> Afterwards, check that `GH_CONFIG_DIR=<worker>/gh gh api user --jq .login`
> prints `<login>`.

## Configure git in each checkout

For each repository's main checkout, set a local credential helper. Pushes
then use the worker's `gh` login, and the global git configuration stays as
it is. The empty first value clears any helper that another configuration
file sets for `github.com`.

```sh
git -C <path> config --local credential.https://github.com.helper ''
git -C <path> config --local --add credential.https://github.com.helper '!gh auth git-credential'
```

Set the commit author to the factory account. Use an email address that
GitHub links to the account, such as its no-reply address
`<id>+<login>@users.noreply.github.com`. `GH_CONFIG_DIR=<worker>/gh gh api user --jq .id`
prints `<id>`.

```sh
git -C <path> config --local user.name '<login>'
git -C <path> config --local user.email '<email>'
```

Kanban worktrees share their main checkout's configuration, so each worktree
uses these values.

## Create the webhooks

Each configured repository needs a webhook with these values:

- **Payload URL:** `https://<hostname>/webhooks/github`.
- **Content type:** `application/json`.
- **Secret:** the value of `GITHUB_WEBHOOK_SECRET` in `<home>/.env`.
- **Events:** the `GITHUB_EVENTS` list in `templates/render.py`. Today these
  are `issues`, `issue_comment`, `pull_request`, `pull_request_review`,
  `pull_request_review_comment`, `workflow_run`, `check_run`, and
  `check_suite`.

An organization webhook with the same values covers every repository in the
organization. The route ignores events from repositories that are not
configured.

A webhook needs admin access to the repository. The factory account has only
write access, so ask the operator to choose one of these:

- The operator creates each webhook in the repository's **Settings** page.
- You create each webhook with `gh api` under the operator's own admin `gh`
  login, without `GH_CONFIG_DIR`.

If `env_has <home>/.env GITHUB_WEBHOOK_SECRET` prints `0`, create the secret
first. A new secret breaks every existing webhook that uses the old one, so
keep an existing secret.

> **HUMAN CHECKPOINT.** When the operator creates the webhooks in the web
> page, ask them to generate the secret in their own terminal with the
> clipboard line from the skill and paste it into each webhook form:
>
> ```sh
> openssl rand -hex 32 | tee >(pbcopy) | env_set <home>/.env GITHUB_WEBHOOK_SECRET
> ```

When you create the webhooks, generate the secret straight into the file:

```sh
openssl rand -hex 32 | env_set <home>/.env GITHUB_WEBHOOK_SECRET
```

Then, for each repository, list the existing webhooks:

```sh
gh api repos/<full_name>/hooks --jq '.[] | {id, url: .config.url, events, active}'
```

Write the request body to a `0600` file in your scratch directory. The script
reads the secret from `<home>/.env` and the events from `templates/render.py`,
and prints nothing:

```sh
( umask 077
  python3 - <home>/.env <repo>/templates <hostname> > <scratch>/hook.json <<'PY'
import json, sys
env, templates, hostname = sys.argv[1:]
sys.path.insert(0, templates)
from render import GITHUB_EVENTS
secret = next(line.split("=", 1)[1].strip() for line in open(env) if line.startswith("GITHUB_WEBHOOK_SECRET="))
print(json.dumps({"active": True, "events": GITHUB_EVENTS, "config": {
    "url": f"https://{hostname}/webhooks/github", "content_type": "json", "secret": secret}}))
PY
)
```

If a webhook's `config.url` is `https://<hostname>/webhooks/github`, update
that webhook. Otherwise create one:

```sh
gh api --method PATCH repos/<full_name>/hooks/<id> --input <scratch>/hook.json --jq .id
gh api --method POST repos/<full_name>/hooks --input <scratch>/hook.json --jq .id
```

For an organization webhook, use `orgs/<org>/hooks` in place of
`repos/<full_name>/hooks` and add `"name": "web"` to the body. Delete
`<scratch>/hook.json` when every repository is done. The webhook API never returns the secret, so a
second run updates each webhook again with the same values.

## Verify

Run the checks in [Check](#check). For the webhooks, each repository's list
shows exactly one webhook whose `url` is `https://<hostname>/webhooks/github`,
whose `events` equal `GITHUB_EVENTS`, and whose `active` is `true`.

## Live test

Run this after part 14 has restarted the gateway.

> **HUMAN CHECKPOINT.** Ask the operator to comment on a test issue in a
> configured repository.

Read the latest delivery of the factory's webhook:

```sh
gh api repos/<full_name>/hooks/<id>/deliveries --jq '.[0] | {event, status_code}'
```

Expect `200` for an event the route ignores and `202` for an accepted event. A
`401` means `GITHUB_WEBHOOK_SECRET` in `<home>/.env` does not match the
webhook's secret.
