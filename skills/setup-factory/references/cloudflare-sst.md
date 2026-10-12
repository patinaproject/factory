# Public ingress with Cloudflare and SST

Linear and GitHub reach the factory through one public hostname. A Cloudflare
Tunnel carries the traffic to the machine, and Cloudflare Access guards every
path except the two webhook paths. Every Cloudflare resource is code in the
operator's own SST app. This repository ships no SST app. Never create a
factory resource by hand in the Cloudflare dashboard or with the `cloudflared`
CLI.

Ask the operator for the hostname, the Cloudflare account and zone, the
identities that Access allows, and the session length. Never assume them.

Also ask whether people should reach the machine over SSH through Access, and
if so, for the SSH hostname and the email of each person. That email list is
the machine's one SSH allowlist. The Access SSH policy and the machine's sshd
principals both come from it.

## Security model

Reproduce this model exactly.

| Layer | Requirement |
| --- | --- |
| Tunnel | One remotely managed Cloudflare Tunnel. `cloudflared` runs it on the machine as a system service that survives a reboot, with only the tunnel token. |
| Tunnel ingress | An explicit path allowlist on the factory hostname, in this order: the exact webhook paths `^/webhooks/(linear\|github)$` go to the Hermes webhook listener. The proxy's management UI paths go to the proxy, when a proxy is used. Every other path on the hostname goes to the Hermes dashboard. A final catch-all returns `http_status:404`. |
| DNS | A proxied `CNAME` from the factory hostname to `<tunnel-id>.cfargotunnel.com`. |
| Access | One `self_hosted` Access application covers the whole hostname. It has an `allow` policy for the operator's identities, such as an email domain, and a bounded session, such as `24h`. Every path requires an allowed identity by default, including the dashboard and the proxy's management UI. |
| Webhook bypass | One path-scoped `self_hosted` Access application for each webhook path, each with a single `bypass` policy for `everyone`. Nothing broader than the exact webhook paths is exempt. These paths rely on Hermes' signature check: `GET` returns `405` and an unsigned `POST` returns `401`. |
| Dashboard sign-in | A `saas` Access application with OIDC is the Hermes dashboard's identity provider. It is a public client: the authorization code grant with PKCE, without a client secret. It also allows the `refresh_tokens` grant, so the dashboard renews its session when the short-lived access token expires instead of signing the user out. Its issuer and client ID go in `dashboard.oauth.self_hosted`, and Hermes requests the `offline_access` scope, without which Cloudflare returns no refresh token. The machine never receives the client secret. The dashboard checks identity even behind the edge. |
| Dashboard host | `dashboard.public_url` is `https://<hostname>`. The tunnel keeps the public `Host` header, and a loopback-bound dashboard rejects every host except loopback and this one with `400 Invalid Host header`. Setting it also turns on the dashboard's sign-in gate, so it needs the dashboard sign-in above. |
| Local binding | The webhook listener, the dashboard, and any proxy listen on `127.0.0.1` only. Nothing is reachable except through the tunnel and Access. |
| SSH, optional | An Access application of type `ssh` on its own SSH hostname, with a short-lived certificate authority. Its `allow` policy includes each person on the SSH allowlist by email, never an email domain: SSH lands in an account whose password grants root. A tunnel ingress rule sends the SSH hostname to `ssh://127.0.0.1:22` and requires the application's Access token at the origin. |
| sshd | `TrustedUserCAKeys` holds the certificate authority's public key, and `AuthorizedPrincipalsFile` lists the local part of each allowlist email, one per line. sshd accepts only keys and certificates, never a password, on every address. The runbook's host exposure part checks this with `scripts/check-host-exposure`. |

## Check

All of these must hold. If they do, report the ingress as already configured
and run [Verify](#verify) only.

- The SST app defines every resource in [Add the resources](#3-add-the-resources)
  for the factory's stage.
- `sst diff --stage <stage>` reports no changes.
- `cloudflared` runs as a system service, and the tunnel shows a healthy
  connector.
- `check-install` (setup part 5) prints no `dashboard sign-in` line. It checks
  that `dashboard.public_url` is set, that the scopes request
  `offline_access`, and that the issuer offers the `refresh_tokens` grant.
  Confirm that `public_url` is `https://<hostname>` with
  `hermes config get dashboard.public_url`.
- The external checks in [Verify](#verify) pass.

## 1. Find or create the SST app

Ask the operator where their SST app lives and which stage the factory belongs
to. Read the app's SST version from its `package.json`, and use that version's
documentation for every syntax question. If they have none, create an SST app
with `home: 'cloudflare'` in the location they choose. Use the SST major
version that the operator's other apps use, or the current release when they
have no other app.

Add a typed stage setting for the factory. Follow the app's existing
stage-config conventions. For example:

```ts
type FactoryStage = {
  hostname: string;
  allowedEmailDomain: string;
  sessionDuration: string;
  webhookPaths: string[];
  dashboardPort: number;
  webhookPort: number;
  proxyPort?: number;
};
```

`webhookPaths` is `["/webhooks/linear", "/webhooks/github"]`. `webhookPort` is
the port from part 6.

## 2. Load the Cloudflare credentials

Load credentials the way the app already does, through its secret manager or
its environment. `CLOUDFLARE_API_TOKEN` needs these permissions:

- Zone DNS edit on the factory's zone.
- Account Cloudflare Tunnel edit.
- Account Zero Trust Access applications and policies edit.

If no such token exists, create a scoped token with exactly these permissions
and store it in the app's secret storage. Never print it.

> **HUMAN CHECKPOINT.** If you cannot create the token through an existing
> token or API, ask the operator to create it in the Cloudflare dashboard and
> to store it in the app's secret storage from their own terminal.

## 3. Add the resources

Add these resources with the SST Cloudflare provider. Read the installed
provider's type definitions for the exact argument names, because they change
between provider versions.

1. `cloudflare.ZeroTrustTunnelCloudflared` with `configSrc: 'cloudflare'`, so
   the tunnel is remotely managed.
2. `cloudflare.ZeroTrustTunnelCloudflaredConfig` with the ingress rules in this
   order:

   | Hostname | Path | Service |
   | --- | --- | --- |
   | `<hostname>` | `^/webhooks/(linear\|github)$` | `http://127.0.0.1:<webhookPort>` |
   | `<hostname>` | The proxy's management UI paths, only when a proxy is used | `http://127.0.0.1:<proxyPort>` |
   | `<ssh-hostname>`, only when SSH is used | None | `ssh://127.0.0.1:22`, with the SSH application's Access token required at the origin |
   | `<hostname>` | None | `http://127.0.0.1:<dashboardPort>` |
   | None | None | `http_status:404` |

3. `cloudflare.DnsRecord`, a proxied `CNAME` from `<hostname>` to
   `<tunnel-id>.cfargotunnel.com`.
4. `cloudflare.ZeroTrustAccessApplication` with type `self_hosted` for
   `<hostname>`, its `allow` policy for the operator's identities, and the
   session length.
5. One `cloudflare.ZeroTrustAccessApplication` with type `self_hosted` and
   `domain: <hostname><path>` for each entry in `webhookPaths`. Each has a
   single `bypass` policy whose include rule is `everyone`.
6. `cloudflare.ZeroTrustAccessApplication` with type `saas` and OIDC for the
   dashboard. Allow the `authorization_code_with_pkce` and `refresh_tokens`
   grants, allow PKCE without a client secret, and request the `openid`,
   `email`, and `profile` scopes. Keep the access token lifetime short, such as
   `5m`, and set the refresh token lifetime shorter than the Access session,
   such as `23h` for a `24h` session. Access checks its policies again at each
   refresh. Its only redirect URI is the dashboard's OAuth callback,
   `https://<hostname>/auth/callback`. Confirm the callback path in the Hermes
   dashboard documentation for the pinned release.

7. Only when SSH is used: a proxied `CNAME` from `<ssh-hostname>` to the
   tunnel, a `cloudflare.ZeroTrustAccessApplication` with type `ssh` for
   `<ssh-hostname>` whose `allow` policy includes each allowlist email, and a
   `cloudflare.ZeroTrustAccessShortLivedCertificate` for that application.
   Keep the allowlist as one non-empty list of emails in the app's stage data,
   and build the policy from it. Adding a person then changes the policy in
   place, and the application and certificate authority stay the same.
   Output the certificate authority's public key. It is not a secret, and
   the machine's `TrustedUserCAKeys` file needs it.

The proxy's management UI paths come from the proxy's documentation. Ask the
operator to confirm them.

### Adopt existing resources

Before you add a resource, look for it. Search the SST app's code, and list
the account's tunnels, the zone's DNS records, and the Access applications
through the Cloudflare API with `CLOUDFLARE_API_TOKEN`. Sort each resource in
the list above into one of three cases:

| Case | Action |
| --- | --- |
| The SST app already defines it | Keep it. Reference it from the factory's code, and change it only to match the table above. |
| It exists, but outside SST | A DNS record or an Access application: import it into SST with the provider's import option, so its ID stays the same. The tunnel: replace it, as below. |
| It does not exist | Add it. |

Replace a tunnel that was created outside SST rather than importing it.
Cloudflare does not return a tunnel's secret, so an imported tunnel cannot
match SST's state, and `sst diff` keeps reporting a change. Add the new
tunnel and its configuration to SST, deploy, then switch the machine and the
DNS record to it:

1. Deploy the new tunnel with its ingress rules. Do not change the DNS record
   yet.
2. Install the `cloudflared` service with the new tunnel's token, as in
   [Deploy](#5-deploy). Uninstall the old service first, because a machine
   runs one `cloudflared` service.
3. Point the DNS record at the new tunnel's ID and deploy again.
4. Run [Verify](#verify).

Steps 2 and 3 cause downtime. From the moment the old service stops until the
DNS change in step 3 takes effect, the hostname still points at the old
tunnel, which has no connector, so Linear and GitHub deliveries fail. Run
steps 2 and 3 back to back, in a quiet window that the operator chooses, and
tell the operator before step 2. GitHub does not retry a failed delivery, so
after Verify, redeliver each GitHub delivery that failed in that window from
the App's recent deliveries list.

> **HUMAN CHECKPOINT.** After Verify passes, ask the operator to delete the old
> tunnel in the Cloudflare dashboard. Deleting a tunnel cannot be undone.

The factory's resources are adopted when `sst diff --stage <stage>` reports no
changes and the account has no factory resource outside the SST app.

## 4. Keep secrets out of outputs

Output only non-secret values as SST outputs: the tunnel ID, the OIDC issuer,
and the OIDC client ID.

The tunnel token is a secret. Read it through the provider at deploy time and
write it straight into the `cloudflared` system service. It never goes into an
SST output, a log line, or the repository.

Cloudflare also generates a client secret for the `saas` application. Hermes is
a public PKCE client and never uses it. Leave it in SST state and give it to no
consumer, including the factory machine.

## 5. Deploy

If the SST app's repository uses pull request review, open a pull request with
these resources and wait for it to merge. Never deploy from an unmerged branch.
Then deploy the stage:

```sh
sst deploy --stage <stage>
```

Run `sst deploy --stage <stage>` a second time. It must report no changes.

> **HUMAN CHECKPOINT.** Installing a system service needs administrator rights.
> Ask the operator to run `sudo cloudflared service install` with the tunnel
> token from the secret storage of step 4, in their own terminal.

Check that the tunnel shows a healthy connector in the SST app's tunnel status
output or the Cloudflare API, and that the service is enabled at boot
(`launchctl print system/com.cloudflare.cloudflared` on macOS,
`systemctl is-enabled cloudflared` on Linux).

## 6. Configure the dashboard sign-in

Take the client ID from the SST outputs. Cloudflare's OIDC issuer for a `saas`
application is `https://<team>.cloudflareaccess.com/cdn-cgi/access/sso/oidc/<client ID>`.
Both values are public, so they go in `<home>/config.yaml`, not in a secret
store:

```sh
hermes config set dashboard.public_url "https://<hostname>"
hermes config set dashboard.oauth.self_hosted.issuer "<issuer>"
hermes config set dashboard.oauth.self_hosted.client_id "<client ID>"
hermes config set dashboard.oauth.self_hosted.scopes "openid profile email offline_access"
```

`hermes config set` can warn that the `self_hosted` keys are not recognized.
The bundled self-hosted provider reads them, so the warning is harmless. Check
the key names against the Hermes dashboard documentation for the pinned release.

Set them together. With `public_url` alone, the dashboard refuses to start
because no sign-in provider is configured. Without `offline_access`, the
dashboard signs the user out when the first access token expires, even though
the application allows refresh tokens. Restart the dashboard service, then run
`check-install` and confirm that it prints no `dashboard sign-in` line. Do not
read the block back with `hermes config get dashboard --json --raw`, which also
prints the `basic_auth` fields.

## Verify

Run these from a machine outside the factory's network, or from the factory
machine through the public hostname.

```sh
# Every path except the webhook paths redirects to the Access login.
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://<hostname>/
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://<hostname>/webhooks/other
# Expect 302 to https://<team>.cloudflareaccess.com/...

# The webhook paths reach Hermes.
curl -s -o /dev/null -w '%{http_code}\n' https://<hostname>/webhooks/linear   # 405
curl -s -o /dev/null -w '%{http_code}\n' https://<hostname>/webhooks/github   # 405
curl -s -X POST -H 'content-type: application/json' -d '{}' https://<hostname>/webhooks/linear   # 401 Invalid signature
curl -s -X POST -H 'content-type: application/json' -d '{}' https://<hostname>/webhooks/github   # 401 Invalid signature
```

A signed delivery of an event the routes ignore proves the whole path without
starting work. Each request returns `200` with `ignored`:

```sh
signed_post() {
  python3 - "$@" <<'PY'
import hashlib, hmac, sys, urllib.request
env_file, key, url, header, event = sys.argv[1:]
secret = next(l.split("=", 1)[1].strip() for l in open(env_file) if l.startswith(key + "="))
body = b'{"type": "SetupCheck"}'
digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
headers = {"Content-Type": "application/json", header: digest if header == "Linear-Signature" else "sha256=" + digest}
if event:
    headers["X-GitHub-Event"] = event
with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers)) as resp:
    print(resp.status, resp.read().decode())
PY
}
signed_post <home>/.env LINEAR_WEBHOOK_SECRET https://<hostname>/webhooks/linear Linear-Signature ""
signed_post <home>/.env GITHUB_WEBHOOK_SECRET https://<hostname>/webhooks/github X-Hub-Signature-256 ping
```

On the factory machine, the dashboard accepts the public host and sends it to
the sign-in. Each request returns `302` to `/auth/login`, not `400`:

```sh
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -H 'Host: <hostname>' http://127.0.0.1:<dashboardPort>/
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -H 'Host: <hostname>' http://127.0.0.1:<dashboardPort>/kanban
```

Then check these:

- Another hostname routed to the tunnel returns `404` from the catch-all. If
  the zone has no other such hostname, read the tunnel's remote configuration
  and confirm that the `http_status:404` rule is last.
- Opening `https://<hostname>/kanban` in a browser asks for the Access sign-in,
  then the dashboard's own OIDC sign-in through the `saas` application, then
  shows the Kanban board. Leave it open past the access token lifetime. It
  stays signed in, and `<home>/logs/dashboard-auth.log` records a
  `refresh_success` event instead of a new `login_success`.
- On the factory machine, `lsof -nP -iTCP -sTCP:LISTEN` shows the webhook
  listener, the dashboard, and any proxy on `127.0.0.1` only.
- After a reboot, the tunnel reconnects without anyone signing in.
- The Cloudflare account has no factory resource outside the SST app.
