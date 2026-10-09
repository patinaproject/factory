# Public ingress with Cloudflare and SST

Linear and GitHub reach the factory through one public hostname. A Cloudflare
Tunnel carries the traffic to the machine, and Cloudflare Access guards every
path except the two webhook paths. Every Cloudflare resource is code in the
operator's own SST app. This repository ships no SST app. Never create a
factory resource by hand in the Cloudflare dashboard or with the `cloudflared`
CLI.

Ask the operator for the hostname, the Cloudflare account and zone, the
identities that Access allows, and the session length. Never assume them.

## Security model

Reproduce this model exactly.

| Layer | Requirement |
| --- | --- |
| Tunnel | One remotely managed Cloudflare Tunnel. `cloudflared` runs it on the machine as a system service that survives a reboot, with only the tunnel token. |
| Tunnel ingress | An explicit path allowlist on the factory hostname, in this order: the exact webhook paths `^/webhooks/(linear\|github)$` go to the Hermes webhook listener. The proxy's management UI paths go to the proxy, when a proxy is used. Every other path on the hostname goes to the Hermes dashboard. A final catch-all returns `http_status:404`. |
| DNS | A proxied `CNAME` from the factory hostname to `<tunnel-id>.cfargotunnel.com`. |
| Access | One `self_hosted` Access application covers the whole hostname. It has an `allow` policy for the operator's identities, such as an email domain, and a bounded session, such as `24h`. Every path requires an allowed identity by default, including the dashboard and the proxy's management UI. |
| Webhook bypass | One path-scoped `self_hosted` Access application for each webhook path, each with a single `bypass` policy for `everyone`. Nothing broader than the exact webhook paths is exempt. These paths rely on Hermes' signature check: `GET` returns `405` and an unsigned `POST` returns `401`. |
| Dashboard sign-in | A `saas` Access application with OIDC is the Hermes dashboard's identity provider. Its issuer and client ID go in `dashboard.oauth.self_hosted`. Its client secret goes in `<home>/.env`. The dashboard checks identity even behind the edge. |
| Local binding | The webhook listener, the dashboard, and any proxy listen on `127.0.0.1` only. Nothing is reachable except through the tunnel and Access. |

## Check

All of these must hold. If they do, report the ingress as already configured
and run [Verify](#verify) only.

- The SST app defines every resource in [Add the resources](#3-add-the-resources)
  for the factory's stage.
- `sst diff --stage <stage>` reports no changes.
- `cloudflared` runs as a system service, and the tunnel shows a healthy
  connector.
- The external checks in [Verify](#verify) pass.

## 1. Find or create the SST app

Ask the operator where their SST app lives and which stage the factory belongs
to. If they have none, create an SST v3 app with `home: 'cloudflare'` in the
location they choose.

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
   dashboard. Its redirect URI is the dashboard's OAuth callback URL on
   `<hostname>`. Find the callback path in the Hermes dashboard documentation
   for the pinned release.

The proxy's management UI paths come from the proxy's documentation. Ask the
operator to confirm them.

## 4. Keep secrets out of outputs

Output only non-secret values as SST outputs: the tunnel ID, the OIDC issuer,
and the OIDC client ID.

Two values are secrets: the tunnel token and the OIDC client secret. Read them
through the provider at deploy time and write them straight into the machine's
secret storage:

- The tunnel token goes into the `cloudflared` system service.
- The OIDC client secret goes into `<home>/.env`, with `env_set`.

Neither value goes into an SST output, a log line, or the repository.

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

Set the issuer and client ID from the SST outputs under
`dashboard.oauth.self_hosted` in `<home>/config.yaml`. Take the exact key names
and the `.env` variable for the client secret from the Hermes dashboard
documentation for the pinned release. Read the values back with
`hermes config get dashboard.oauth.self_hosted --json --raw`.

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

Then check these:

- Another hostname routed to the tunnel returns `404` from the catch-all. If
  the zone has no other such hostname, read the tunnel's remote configuration
  and confirm that the `http_status:404` rule is last.
- Opening `https://<hostname>/` in a browser asks for the Access sign-in, then
  the dashboard's own OIDC sign-in through the `saas` application.
- On the factory machine, `lsof -nP -iTCP -sTCP:LISTEN` shows the webhook
  listener, the dashboard, and any proxy on `127.0.0.1` only.
- After a reboot, the tunnel reconnects without anyone signing in.
- The Cloudflare account has no factory resource outside the SST app.
