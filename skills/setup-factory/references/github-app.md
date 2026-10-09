# GitHub App

The worker commits, pushes, and opens ready pull requests as the factory's
GitHub App. The App also sends the GitHub webhook events that the `github`
route triages.

## Check

All of these must hold:

- `github_app.app_id` and `github_app.installation_id` are set in the settings
  of both homes.
- `env_has <home>/.env GITHUB_WEBHOOK_SECRET` prints `1`.
- `env_has <worker>/.env GITHUB_APP_PRIVATE_KEY_PATH` prints `1`, and the file
  it names exists with mode `0600`, outside every repository checkout:
  `stat -f '%Lp' <key path>` on macOS or `stat -c '%a' <key path>` on Linux
  prints `600`, and `git -C "$(dirname <key path>)" rev-parse 2>/dev/null`
  fails.
- The App is installed on every configured repository (see [Verify](#verify)).
- `gh` in the worker profile's environment acts as the App (see
  [Authenticate the worker as the App](#authenticate-the-worker-as-the-app)).

## Create the App

> **HUMAN CHECKPOINT.** Ask the operator to create a GitHub App owned by the
> organization or account that owns the repositories, with these values:
>
> - **Webhook URL:** `https://<hostname>/webhooks/github`.
> - **Webhook secret:** generated in the operator's terminal with the
>   clipboard line from the skill, for `GITHUB_WEBHOOK_SECRET` in
>   `<home>/.env`.
> - **Repository permissions:** Contents read and write, Pull requests read
>   and write, Issues read, Checks read, Actions read, Metadata read.
> - **Events:** Issues, Issue comment, Pull request, Pull request review, Pull
>   request review comment, Workflow run, Check run, Check suite.
> - **Where it can be installed:** only on this account.
>
> Then ask the operator to generate a private key and save the downloaded
> file in a directory outside every repository, for example
> `~/.config/factory/`.

The events match `GITHUB_EVENTS` in `templates/render.py`. If that list
changes, change the App's events to match.

After the checkpoint:

```sh
chmod 600 <key path>
printf '%s' <key path> | env_set <worker>/.env GITHUB_APP_PRIVATE_KEY_PATH
```

Ask the operator for the App ID shown on the App's settings page. It is not a
secret.

## Install the App on the repositories

> **HUMAN CHECKPOINT.** Ask the operator to install the App on the account and
> select each configured repository. The installation ID is the number at the
> end of the installation's settings URL.

Record `app_id` and `installation_id` for the settings in part 10.

## Authenticate the worker as the App

GitHub issues an installation access token in exchange for a JSON Web Token
that the App's private key signs. The token acts as the App on the installed
repositories and expires after one hour. The worker's `gh` reads `GH_TOKEN`,
and `git` reads credentials through a credential helper, so each needs a token
that is fresh when it runs.

`pr-ready-gate` also runs `gh pr view` inside the worker profile, so `gh` must
be authenticated there for the gate to pass.

**UNCONFIRMED.** This repository does not yet include the component that mints
installation tokens for the worker. Do not store a personal access token or the
operator's own `gh` login as a stand-in, because the worker would then act as a
person instead of the App. Report this part as blocked and name the gap. Resume
when the operator chooses how the worker gets a fresh installation token.

## Verify

Mint one installation token to check the App, its key, and its installation.
Keep the token in a variable and never print it:

```sh
token="$(python3 - <key path> <app_id> <installation_id> <<'PY'
import json, subprocess, sys, time, base64, urllib.request
key, app_id, inst = sys.argv[1:]
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
now = int(time.time())
head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
body = b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key], input=head + b"." + body,
                     capture_output=True, check=True).stdout
jwt = (head + b"." + body + b"." + b64(sig)).decode()
req = urllib.request.Request(f"https://api.github.com/app/installations/{inst}/access_tokens", method="POST",
                             headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json"})
print(json.load(urllib.request.urlopen(req))["token"])
PY
)"
GH_TOKEN="$token" gh api /installation/repositories --jq '.repositories[].full_name'
unset token
```

The list contains every configured `full_name`.

## Live test

Run this after part 14 has restarted the gateway.

> **HUMAN CHECKPOINT.** Ask the operator to open the App's **Advanced** page
> and redeliver the latest delivery, or to comment on a test issue in a
> configured repository. The **Recent Deliveries** list shows the response
> code.

Expect `200` for an event the route ignores and `202` for an accepted event. A
`401` means `GITHUB_WEBHOOK_SECRET` in `<home>/.env` does not match the App's
webhook secret.
