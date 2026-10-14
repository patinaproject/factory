# GitHub App and webhooks

The factory acts on GitHub as a GitHub App. The worker fetches, publishes
commits, and opens ready pull requests as the App's bot user,
`<app-slug>[bot]`. The App's own webhook sends the events that the `github`
route triages. The factory has no machine user account, because a machine
user takes a paid seat.

Hermes documents the worker's GitHub identity in
`website/docs/user-guide/features/kanban.md` of the Hermes install. Kanban
acceptance reads as the assignee profile's own `gh` login, which
`GH_CONFIG_DIR` in that profile's `.env` selects.

Hermes removes `GH_TOKEN`, `GITHUB_TOKEN`, and every `GITHUB_APP_*` variable
from each subprocess that a worker starts: the terminal, `claude-session`, and
Claude Code. It passes `GH_CONFIG_DIR`. The factory therefore keeps an App
installation token in the `gh` login that `GH_CONFIG_DIR` names.
`pr-ready-gate` runs `gh pr view` inside the worker profile, so the gate uses
the same login.

An installation token expires after one hour. The worker profile's
`refresh-gh-app-login` script mints a new token from the App's private key and
stores it with `gh auth login`. A Hermes cron job runs the script every 30
minutes.

Run this reference in two passes. Part 9 runs [Create the App](#create-the-app),
or [Reuse an existing App](#reuse-an-existing-app), through
[Set the worker's `gh` directory](#set-the-workers-gh-directory). Part 10
writes the settings and returns here for
[Sign the worker's `gh` in](#sign-the-workers-gh-in). It then clones the
checkouts as the App, and returns for
[Check the App's access](#check-the-apps-access),
[Configure git in each checkout](#configure-git-in-each-checkout), and
[Block plain pushes on the machine](#block-plain-pushes-on-the-machine).

## Check

`<app-slug>` is the App's URL name, from `https://github.com/apps/<app-slug>`.
All of these must hold:

- `github.login` in the settings of both homes is `<app-slug>[bot]`, and
  `github.app_id`, `github.installation_id`, and `github.private_key_path` have
  values.
- The file at `github.private_key_path` exists and prints `0o600` for
  `python3 -c 'import os, sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' <key path>`.
  The path is outside every configured checkout and outside this repository.
- `grep '^GH_CONFIG_DIR=' <worker>/.env <home>/.env` prints
  `GH_CONFIG_DIR=<worker>/gh` for each file.
- `hermes -p claude-worker cron list` shows exactly one job named
  `refresh-gh-app-login`, and its last run shows no error.
- `GH_CONFIG_DIR=<worker>/gh gh api /installation/repositories --paginate --jq '.repositories[].full_name'`
  lists every configured repository.
- `GH_CONFIG_DIR=<worker>/gh python3 <worker>/scripts/check-github-access`
  prints nothing. See [Check the App's access](#check-the-apps-access).
- For each repository's main checkout,
  `git -C <path> config --local --get-all credential.https://github.com.helper`
  prints an empty line and then `!gh auth git-credential`.
- For each main checkout, `git -C <path> config --local user.name` prints
  `<app-slug>[bot]`, and `git -C <path> config --local user.email` prints
  `<bot user id>+<app-slug>[bot]@users.noreply.github.com`.
- For each main checkout, `git -C <path> config --local remote.origin.pushurl`
  prints `no_push_use_push-signed`.
- `python3 <worker>/scripts/guard-git-push --check` prints nothing. See
  [Block plain pushes on the machine](#block-plain-pushes-on-the-machine).
- `env_has <home>/.env GITHUB_WEBHOOK_SECRET` prints `1`.
- The App's webhook is active and delivers to the factory (see
  [Live test](#live-test)).

If all of them hold, skip to the live test.

## Create the App

If the operator already has a GitHub App for the factory, skip to
[Reuse an existing App](#reuse-an-existing-app).

If `env_has <home>/.env GITHUB_WEBHOOK_SECRET` prints `1`, keep that secret.
A new secret breaks every webhook that uses the old one. When the Infisical
folder already exists, the secret lives there. In the lines below, replace
`env_set <home>/.env GITHUB_WEBHOOK_SECRET` with
`infisical_set GITHUB_WEBHOOK_SECRET`.

> **HUMAN CHECKPOINT.** Ask the operator to create a GitHub App under the
> organization or account that owns the repositories, in
> **Settings → Developer settings → GitHub Apps → New GitHub App**, with these
> values:
>
> - **GitHub App name:** the operator's choice. GitHub derives `<app-slug>`
>   from it.
> - **Webhook:** active, with the URL `https://<hostname>/webhooks/github`.
> - **Webhook secret:** copied to the clipboard in the operator's own
>   terminal and pasted into the form. When the secret does not exist yet,
>   this line generates it:
>
>   ```sh
>   openssl rand -hex 32 | tee >(pbcopy) | env_set <home>/.env GITHUB_WEBHOOK_SECRET
>   ```
>
>   When it exists, this line copies it:
>
>   ```sh
>   grep '^GITHUB_WEBHOOK_SECRET=' <home>/.env | cut -d= -f2- | tr -d '\n' | pbcopy
>   ```
>
> - **Repository permissions:** Contents read and write, Pull requests read
>   and write, Issues read and write, Checks read-only, Commit statuses
>   read-only, Actions read-only, and Metadata read-only.
> - **Subscribe to events:** Issues, Issue comment, Pull request, Pull request
>   review, Pull request review comment, Workflow run, and Check run. Not
>   Check suite.
> - **Where can this GitHub App be installed:** only on this account.
>
> Afterwards, ask for the **App ID** from the App's **General** page and for
> `<app-slug>`. Neither is a secret.

The events are the rows of `GITHUB_EVENT_TABLE` in `templates/render.py`.
Compare the list with the form when that file changes.

### Upgrade an existing App's events

An App created with Check suite still sends every `check_suite` delivery, and
the route drops each one.

> **HUMAN CHECKPOINT.** Ask the operator to open the App's **Permissions &
> events** page, clear **Check suite** under **Subscribe to events**, and save.

### Upgrade an existing App's permissions

An App created before a permission joined the list above lacks it. Without
Issues read and write, it cannot label, comment on, or edit issues, and the
[live test](#live-test) fails. Without Checks or Commit statuses, the
completion gate cannot read the pull request's checks, and refuses the first
ready pull request only after the worker has finished.
[Check the App's access](#check-the-apps-access) names each missing
permission.

> **HUMAN CHECKPOINT.** Ask the operator to open the App's
> **Permissions & events** page, set each permission that the check names to
> the access it names, and save.
> GitHub then asks the installation's owner to accept the new permissions.
> Ask the operator to accept them on the installation's settings page,
> `.../settings/installations/<installation id>`.

The worker's next token, at most 30 minutes later, carries the new
permission. To use it at once, run the `refresh-gh-app-login` job as in
[Sign the worker's `gh` in](#sign-the-workers-gh-in).

## Reuse an existing App

Use this path when the operator already has a GitHub App for the factory. It
creates nothing. It reads the App through GitHub's API with a JWT that the
App's private key signs, so it needs no personal login.

**Find the App ID and the key.** Read `github.app_id` and
`github.private_key_path` from the settings of either home on a re-run. On a
new machine, look for an App helper's configuration file next to the key, or
ask the operator for the App ID from the App's **General** page. The key can
stay where it is. It needs mode `0600`, and it must be outside every
repository. Run the `python3` mode check in [Check](#check), then record its
absolute path for `github.private_key_path`. Do not copy the key.

Define these two functions. They never print the key or the JWT:

```sh
app_jwt() {
  b64() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
  now=$(date +%s)
  h=$(printf '{"alg":"RS256","typ":"JWT"}' | b64)
  p=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 540)) "$1" | b64)
  s=$(printf '%s.%s' "$h" "$p" | openssl dgst -sha256 -sign "$2" | b64)
  printf '%s.%s.%s' "$h" "$p" "$s"
}
app_api() {
  curl -fsS -H "Authorization: Bearer $(app_jwt "$1" "$2")" \
    -H 'Accept: application/vnd.github+json' "https://api.github.com$3"
}
```

**Check the key, permissions, and events.**

```sh
app_api <app id> <key path> /app | python3 -c 'import json, sys; a = json.load(sys.stdin); print(a["slug"]); print(json.dumps(a["permissions"], sort_keys=True)); print(sorted(a["events"]))'
```

A `401` error means the key does not belong to that App ID. The slug is
`<app-slug>`. Compare the permissions and events with the lists in
[Create the App](#create-the-app). For a missing permission, follow
[Upgrade an existing App's permissions](#upgrade-an-existing-apps-permissions).
For a missing event, ask the operator to subscribe to it on the App's
**Permissions & events** page.

**Find the installation.**

```sh
app_api <app id> <key path> /app/installations | python3 -c 'import json, sys; [print(i["id"], i["account"]["login"], i["repository_selection"]) for i in json.load(sys.stdin)]'
```

The installation ID is the first value on the line for the account that owns
the repositories. When no line names that account, follow
[Install the App](#install-the-app). When the selection is `selected`, list
the repositories the installation covers. The token stays in a variable and
is never printed:

```sh
tok="$(curl -fsS -X POST -H "Authorization: Bearer $(app_jwt <app id> <key path>)" \
  -H 'Accept: application/vnd.github+json' \
  https://api.github.com/app/installations/<installation id>/access_tokens \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')"
curl -fsS -H "Authorization: Bearer $tok" 'https://api.github.com/installation/repositories?per_page=100' \
  | python3 -c 'import json, sys; [print(r["full_name"]) for r in json.load(sys.stdin)["repositories"]]'
unset tok
```

Every configured repository must be in the list. Otherwise ask the operator to
add the missing ones on the installation's settings page,
`.../settings/installations/<installation id>`.

**Find out who uses the webhook.**

```sh
app_api <app id> <key path> /app/hook/config
app_api <app id> <key path> '/app/hook/deliveries?per_page=30' | python3 -c 'import json, sys; [print(d["delivered_at"], d["event"], d["status_code"]) for d in json.load(sys.stdin)]'
```

The config shows the webhook URL. GitHub masks the secret. When the URL
is already `https://<hostname>/webhooks/github`, keep it. When it is another
URL and recent deliveries succeed, something else receives the App's events.

> **HUMAN CHECKPOINT.** Show the operator the current URL and the recent
> deliveries. Ask whether anything still needs that URL. If something does,
> stop and create a new App for the factory instead. If nothing does, ask them
> to set the webhook URL to `https://<hostname>/webhooks/github` on the App's
> **General** page, and to set the webhook secret with the lines in
> [Create the App](#create-the-app). GitHub never returns the old secret, so
> a factory with no copy of it needs a new one.

Then record the values for part 10 as [Install the App](#install-the-app)
describes, and continue with
[Set the worker's `gh` directory](#set-the-workers-gh-directory).

## Download the private key

The private key signs the token requests. Keep it outside every repository,
in a file that only the user who runs the Hermes gateway can read. This
reference uses `~/.config/factory/github-app.pem` as the example path.

> **HUMAN CHECKPOINT.** Ask the operator to open the App's **General** page,
> click **Generate a private key**, and move the downloaded file into place
> from their own terminal:
>
> ```sh
> mkdir -p ~/.config/factory && chmod 700 ~/.config/factory
> mv ~/Downloads/<app-slug>.*.private-key.pem ~/.config/factory/github-app.pem
> chmod 600 ~/.config/factory/github-app.pem
> ```

Check the mode with the `python3` line in [Check](#check). Record the absolute
path for `github.private_key_path`.

## Install the App

> **HUMAN CHECKPOINT.** Ask the operator to open
> `https://github.com/apps/<app-slug>/installations/new`, choose the
> organization, select **Only select repositories**, and add every configured
> repository. Afterwards, ask for the installation ID, the number at the end of
> the installation's settings URL, `.../settings/installations/<installation id>`.

Find the bot user's ID. The endpoint is public, so any `gh` login can read it:

```sh
gh api '/users/<app-slug>[bot]' --jq .id
```

Record these values for the `github` object in part 10:

| Key | Value |
| --- | --- |
| `login` | `<app-slug>[bot]` |
| `app_id` | The App ID |
| `installation_id` | The installation ID |
| `private_key_path` | The absolute path of the private key |

The `github` route keeps an event only when it belongs to a card. A
`pull_request`, `pull_request_review`, or `pull_request_review_comment` event
passes when the App, as `github.login`, opened the pull request from a branch
listed in `<home>/owned-refs.json`. A `workflow_run` event passes when the App
triggered it on a listed branch and it failed. A `check_run` event passes when
it failed on a listed branch and is not a GitHub Actions job. An `issues`
event passes when its issue URL is listed. An `issue_comment` event passes
when the App opened the pull request, or when the issue URL is listed. Its
payload has no branch, so a comment on any App pull request passes. Linear's
linkback comment and Cloudflare's deploy notice never pass: the linkback asks
for nothing, and a failed Cloudflare build already arrives as a `check_run`. The route
also drops events the App itself sends, so the factory does not triage its
own pushes and comments. Failed checks and the App's own merges are the
exceptions, because the worker needs the failure and the card has to
complete.

## Set the worker's `gh` directory

```sh
mkdir -p <worker>/gh
chmod 700 <worker>/gh
printf '%s' <worker>/gh | env_set <worker>/.env GH_CONFIG_DIR
printf '%s' <worker>/gh | env_set <home>/.env GH_CONFIG_DIR
```

The default home uses the same login, so Hermes' own agent acts on GitHub as
the App, for example when it answers a question in Slack.

Part 9 ends here. Continue with part 10.

## Sign the worker's `gh` in

Run this after part 10 has set the settings in the worker profile.

The script lives in the worker profile's `scripts/` directory, so the job
belongs to the worker profile. `cron create` does not remove duplicates. If
`hermes -p claude-worker cron list` shows more than one `refresh-gh-app-login`
job, show the list to the operator and remove the extras with their approval.
Create the job only when the list has none:

```sh
hermes -p claude-worker cron create "every 30m" --name refresh-gh-app-login --no-agent --script refresh-gh-app-login </dev/null
```

Run the job through Hermes once, then check the login:

```sh
hermes -p claude-worker cron run <job id>
hermes -p claude-worker cron list    # Last run shows no error
GH_CONFIG_DIR=<worker>/gh gh api /installation/repositories --jq .total_count
```

The count is the number of repositories the installation covers. It is at
least the number of configured repositories.

The script prints nothing on success. On failure it prints one line that names
the failed step, such as `check private key` or `mint installation token`, and
Hermes delivers the line. `gh api user` fails for an installation token, so do
not use it as the check.

### Check the App's access

`pr-ready-gate` and Hermes' Kanban completion contract read each pull
request, its base branch rules, its check runs, and its commit statuses as the
worker's `gh` login. A missing permission shows up only when the worker tries
to complete a card. `check-github-access` makes the same reads now, for every
configured repository, and compares the App's permissions with the
[permission table](#use-gh-on-a-factory-machine):

```sh
GH_CONFIG_DIR=<worker>/gh python3 <worker>/scripts/check-github-access
```

It prints nothing when every read succeeds. Otherwise it prints one line for
each problem and exits `1`. A line such as
`grant the App "Commit statuses: Read-only"` names the permission to add with
[Upgrade an existing App's permissions](#upgrade-an-existing-apps-permissions).
A `not found (HTTP 404)` line means the installation does not cover the
repository. After the operator accepts a new permission, run the
`refresh-gh-app-login` job, then run the check again.

## Configure git in each checkout

Run this after part 10 has cloned the repositories.

For each repository's main checkout, set a local credential helper. Fetches
then use the App's installation token from the worker's `gh` login, and the
global git configuration stays as it is. The empty first value clears any
helper that another configuration file sets for `github.com`.

```sh
git -C <path> config --local credential.https://github.com.helper ''
git -C <path> config --local --add credential.https://github.com.helper '!gh auth git-credential'
```

Set the commit author to the App's bot user, with the bot user ID from
[Install the App](#install-the-app):

```sh
git -C <path> config --local user.name '<app-slug>[bot]'
git -C <path> config --local user.email '<bot user id>+<app-slug>[bot]@users.noreply.github.com'
```

Block plain pushes:

```sh
git -C <path> config --local remote.origin.pushurl no_push_use_push-signed
```

A GitHub App cannot hold a signing key, so a commit that the worker pushes
with `git push` is unverified. Repositories that require verified signatures
reject such a push. Hosts such as Vercel cancel deployments for unverified
commits. The worker therefore publishes commits only with the `push-signed`
script, which recreates each local commit through GitHub's API as the App.
GitHub signs and verifies commits made that way. The push URL above makes a
plain `git push` fail with an error that names `push-signed`. Fetches still
use the normal URL.

Kanban worktrees share their main checkout's configuration, so each worktree
uses these values.

## Block plain pushes on the machine

The push URL above guards only the configured checkouts. An agent session in
another clone, such as an operator's interactive session, can still run
`git push`, and its unsigned commits then block the pull request's merge.
`guard-git-push` closes that gap for every repository of the user that runs
the factory. It adds global `url.no_push_use_push-signed/.pushInsteadOf`
entries for `https://github.com/`, `git@github.com:`, and
`ssh://git@github.com/`, so git rewrites any push to GitHub to a path that
does not exist:

```sh
python3 <worker>/scripts/guard-git-push           # adds the missing entries, prints nothing
python3 <worker>/scripts/guard-git-push --check   # prints nothing when all entries exist
```

A plain `git push` then fails with
`fatal: 'no_push_use_push-signed/<owner>/<repo>.git' does not appear to be a git repository`,
also with `--no-verify` and in a repository with its own hooks. Fetches keep
the normal URL, and `push-signed` publishes through GitHub's API, so neither
changes. This changes the user's global git configuration, `~/.gitconfig`.
Tell the operator before you run it. A remote with its own `pushurl` keeps
that URL. To remove the guard, run
`git config --global --unset-all url.no_push_use_push-signed/.pushInsteadOf`.

## Webhooks

The App's own webhook replaces per-repository and organization webhooks. It
delivers the subscribed events for every repository the installation covers.
The route ignores events from repositories that are not configured.

If a repository or organization webhook from an earlier setup also targets
`https://<hostname>/webhooks/github`, the route receives each event twice.
Listing them needs an admin login, which the factory machine does not have.
Ask the operator to read **Settings → Webhooks** of each repository and of the
organization, or to run these where their own admin `gh` login lives:

```sh
gh api repos/<full_name>/hooks --jq '.[] | {id, url: .config.url}'
gh api orgs/<org>/hooks --jq '.[] | {id, url: .config.url}'
```

Show any match to the operator, and delete it with their approval.

## Live test

Run this after part 15 has restarted the gateway.

> **HUMAN CHECKPOINT.** Ask the operator to comment on a test issue in a
> configured repository. Then ask them to open the App's
> **Advanced → Recent Deliveries** page and read the delivery's response
> code.

Expect `200` for an event the route ignores and `202` for an accepted event. A
`401` means `GITHUB_WEBHOOK_SECRET` in `<home>/.env` does not match the App's
webhook secret.

Then check that the App can change issues. Add and remove an existing label on
the same test issue as the App:

```sh
GH_CONFIG_DIR=<worker>/gh gh issue edit <issue number> --repo <full_name> --add-label <label>
GH_CONFIG_DIR=<worker>/gh gh issue edit <issue number> --repo <full_name> --remove-label <label>
```

Both commands must succeed. `Resource not accessible by integration` means the
App still has Issues read-only; follow
[Upgrade an existing App's permissions](#upgrade-an-existing-apps-permissions).
The `github` route drops both label events, because their sender is the App.

## Use `gh` on a factory machine

Every agent session on a factory machine acts on GitHub as the factory's App.
This covers the worker and any interactive agent session that the operator
starts, in this repository or in a configured checkout. The App's login is the
worker's `gh` directory, so select it on each command:

```sh
GH_CONFIG_DIR=<worker>/gh gh <command>
```

The configured checkouts' git credential helper already uses the same login,
as set in [Configure git in each checkout](#configure-git-in-each-checkout).

Follow these rules:

- Do not run `gh auth login`, and do not ask the operator to run it. The
  `refresh-gh-app-login` job owns the worker's login.
- Do not set `GH_TOKEN` or `GITHUB_TOKEN`. Either one overrides the login that
  `GH_CONFIG_DIR` selects.
- Do not fall back to the operator's personal login when a command fails. Only
  the steps in this reference that name the operator's own admin login use it.
- Publish commits only with `push-signed`, never with `git push`.
- When `gh` is not on the session's `PATH`, find it through the login shell,
  for example `zsh -lc 'command -v gh'`, and call it by that path.

The App can do what its repository permissions allow:

| Permission | Access | Examples |
| --- | --- | --- |
| Contents | Read and write | Fetch, create branches, publish commits with `push-signed` |
| Pull requests | Read and write | Open, edit, label, and comment on pull requests, and resolve review threads |
| Issues | Read and write | Open, edit, label, and comment on issues |
| Checks | Read-only | Read check runs, which the completion gate needs |
| Commit statuses | Read-only | Read commit statuses, which the completion gate needs |
| Actions | Read-only | Read workflow runs and logs |
| Metadata | Read-only | Read repository names, settings, and collaborators |

When a command fails:

- `Resource not accessible by integration` (HTTP 403) means the App lacks the
  permission. Report the command and the missing permission to the operator.
- `Bad credentials` (HTTP 401) means the token expired. Run the
  `refresh-gh-app-login` job, then retry once.
- `gh api user` always fails for an installation token. Do not treat that as a
  broken login.

### Publish commits

On a factory machine, publish commits only with the worker profile's
`push-signed` script, never with `git push`:

```sh
GH_CONFIG_DIR=<worker>/gh python3 <worker>/scripts/push-signed
```

It recreates each local commit through GitHub's Git Data API as the App. It
uploads the changed blobs, builds a tree, creates the commit with no author or
committer, and fast-forwards the branch. GitHub signs those commits. Tree
entries keep their mode, so symlinks publish as symlinks. `push-signed` still
refuses a new executable file, a mode change, a submodule, and a merge
commit. A commit is verified only when it is
signed with a key that GitHub links to the committer's account, or when GitHub
itself creates the commit. A GitHub App cannot register a signing key, so a
bot commit made locally and pushed with `git push` is never verified. A
repository whose ruleset requires signed commits then refuses to merge the pull
request, and a squash merge does not help, because GitHub checks every commit
in the pull request. Signing with the operator's personal key would verify the
commit as the operator, not the App.

The configured checkouts set `remote.origin.pushurl` to
`no_push_use_push-signed`, and
[Block plain pushes on the machine](#block-plain-pushes-on-the-machine) does
the same for every other clone, so a plain `git push` to GitHub fails with
that name.

When the remote branch does not exist, `push-signed` creates it at the merge
base with the default branch and replays every commit after that point. For a
branch stacked on another unmerged branch, create the remote branch at the
parent's signed head first, so only the new commits are replayed:

```sh
GH_CONFIG_DIR=<worker>/gh gh api --method POST repos/<full_name>/git/refs \
  -f ref=refs/heads/<branch> -f sha=<parent head>
```

`push-signed` only adds commits on top of the remote branch. It cannot rewrite
one. If unsigned commits already reached a pull request, squash the change
locally onto the base, point the remote branch back at the base, then run
`push-signed`. GitHub closes a pull request whose head equals its base, so
reopen it afterwards. Bring the base branch in with `gh pr update-branch`;
GitHub signs that merge.
