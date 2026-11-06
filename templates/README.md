# Webhook route templates

`render.py` builds the Hermes webhook config from the `linear-agent-session`
plugin settings. It prints one JSON object whose keys are Hermes config keys and
whose values are what `hermes config set` installs at them:

- `platforms.webhook.extra.routes.<name>`: the `linear`, `github`, and `email`
  route configs. The Linear route takes its name from the `webhook_route`
  setting. The `linear` and `github` routes each have a `skills` entry that
  names the route's skill. Their `prompt` is only the event section, so Hermes
  caches the rest. The `email` route has no prompt and no skill. See
  [The email route](#the-email-route).
- `platforms.webhook.extra.rate_limit`: the deliveries each route accepts per
  minute. Hermes counts every signed delivery before it applies the route's
  events and filters, and answers `429` past the limit. GitHub does not
  redeliver a rejected event. The default of 30 dropped review events, and the
  `github` route also gets a `push` for every ref update in every installed
  repository, so the factory sets 1200.

The `linear` route runs the `linear-route` script from `<home>/scripts` on each
delivery that passes its filters, and coalesces `Issue` updates per issue. The
script drops a delivery Linear sent again, which it recognises for 7 hours by
the event's type, action, `createdAt`, and object id (and the activity id of a
`prompted` event). It also records each `Issue` update's `updatedFrom` keys and
stamps the payload with `factoryRoute`, `{"issue": <issue id>, "seq": <n>}`. The
route's `coalesce` block keys on `factoryRoute.issue`, so Hermes holds an `Issue`
update for 30 seconds (300 at most) and dispatches only the newest one, whose
`updatedFrom` is now the union of the burst's keys with the earliest value of
each. A `Comment` or `AgentSessionEvent` has no stamp and dispatches at once.
The script fails open: on any error it passes the payload on unchanged. The
`linear-agent-session` dispatch hook clears the issue's recorded keys once the
settled event is handled. Hermes fails closed when the route's script is
missing, so deploy installs the script before the route, and `check-install`
reports a missing or out-of-date copy.

The `linear` route keeps every `AgentSessionEvent` and every `Issue` update that
changes `delegateId`. It keeps a completed, canceled, or duplicate `Issue` update only when
the issue's id is listed in `<home>/owned-refs.json`, which holds each card's
`linear_issue_id`. Deploy the profile and run `refresh-owned-refs` once before
you re-render this route.

The route also keeps a created top-level `Comment` whose `data.issueId` is listed in
`owned-refs.json` or `triage-refs.json`. The `linear-agent-session` plugin skips it
unless an approver answers a card that waits for a person.

For the Triage board, the route also keeps an `Issue` that is created in the
Triage status or moved into it, a status change of an issue listed in
`<home>/triage-refs.json`, which holds each triage card's `linear_issue_id`, and
a label change that leaves an issue delegated to the agent app with
`ready-for-agent` and no implementation card. `refresh-owned-refs` writes both
files. When a status change only moves an issue out of Triage, the
`linear-agent-session` plugin archives its triage card before dispatch, and the
triage prompt never runs. "Triage inbox" step 2 still covers every update the
plugin sends on.

The triage prompts are `triage-linear.md` and `triage-github.md`. In them,
`triage-resume.md` is the one parked-card resume section that both prompts
include through `<<triage_resume>>`. `triage-merged.md` is the merged-pull-request
completion section that only the GitHub prompt includes through `<<triage_merged>>`.
Only the Linear prompt includes `triage-inbox.md`, which creates and archives
triage cards, through `<<triage_inbox>>`, and `triage-delegation-gate.md`, which
creates no implementation card for an issue without `ready-for-agent`, through
`<<delegation_gate>>`. `<<name>>` marks a value the renderer fills from settings, and `{dot.path}` is a
Hermes payload placeholder that the webhook adapter fills per event. Both prompts
get `<<not_assignee_marker>>`, the card comment prefix for an event from someone
other than the issue's assignee. The renderer reads it from
`profiles/claude-worker/scripts/reply_inbox.py`, the worker script that acts on
it, so `check-install` renders from a copy of `templates/` and that file.

Each triage prompt holds an `<<event>>` marker where its event section sits.
`triage-linear-event.md` and `triage-github-event.md` are those sections: the
event fields, the prompt context, and the raw payload. The route's `prompt` is
the section. The route's skill is the rest of the template with the marker
removed, written as `<home>/skills/<name>/SKILL.md`. `ROUTE_TABLE` in `render.py` lists each route with its
templates and skill name. A skill holds the factory's real values, so install
it with `render.py --install-skills` and never commit it. Hermes lists skills
once, so restart the gateway after a new or changed skill. The Linear
dispatch hook adds the computed intent block after the event section.

`GITHUB_EVENT_TABLE` in `render.py` lists each GitHub event and action triage
acts on, and when the event belongs to the App's own work. The `github` route's
filter and the prompt's "events that matter" list are both rendered from it,
so they cannot drift apart. The filter keeps an event only when it belongs to
a card: a pull request the App opened (`github.login` in the settings) from a
branch in `<home>/owned-refs.json`, a failed workflow run the App triggered on
such a branch, a failed `check_run` on such a branch, or an `issues` event on an
issue URL listed there. An `issue_comment` passes on a pull request the App
opened, since its payload has no branch, or on a listed issue URL. Linear's
linkback comment and the deploy and CI bots' notices (Cloudflare, Vercel, GitHub Actions) never pass. The worker's
`refresh-owned-refs` cron job rewrites that file every minute with the branch
and GitHub issue URL of every card not archived, and the route reads it on every
delivery through Hermes' `in_file` operator. The route also drops events the
App itself sends, except failed checks and its own merges.

The `github` route also keeps a `push` to the factory's own repository and
branch, `factory_repository.full_name` and `factory_repository.default_branch`
in the settings, from any sender, the App included. The `factory-deploy`
plugin starts the auto-deploy job for that push and runs no triage turn, so the
prompt's "events that matter" list leaves it out.

Each route reads its secret from `.env` through a `${LINEAR_WEBHOOK_SECRET}`,
`${GITHUB_WEBHOOK_SECRET}`, or `${EMAIL_WEBHOOK_SECRET}` reference, which
Hermes expands when it loads `config.yaml`.

## The email route

An Email Worker, which another repository owns, forwards each email that the
team's shared inbox receives. It sends one `POST` to `/webhooks/email` per
email. The Worker signs the body with `X-Webhook-Signature-V2`, the hex
HMAC-SHA256 of `<timestamp>.<body>`, and puts the timestamp in
`X-Webhook-Timestamp`. The Worker stores the raw message and each attachment
and lists their object keys, sizes, and SHA-256 values in the body.

The email and its files are untrusted, because anyone can send mail to the
inbox. No model reads them in the gateway.

1. **Signature.** Hermes rejects a delivery with no signature, a wrong one, or
   a timestamp more than 300 seconds old. It answers `401` and runs nothing.
2. **Filter.** The route keeps a body only when it has `message_id`. The
   `watch-gateway` probe body has none, so the filter ignores it.
3. **Route script.** `email-route` runs from `<home>/scripts`. It checks the
   body and rejects a `message_id` that is longer than 255 characters or has
   a character outside a fixed safe set, such as `/`. It claims the email in
   the ledger and asks the gateway's `email-intake` plugin to create the card.
   Then it prints `[SILENT]`, so Hermes starts no agent run. A rejected
   delivery exits 1, and Hermes logs the reason.
4. **Card.** `email-intake` creates one card, assigned to the `mail-worker`
   profile, blocked, with the idempotency key `email:<message_id>`.
5. **Fetch.** `email-intake` holds the route's secret, so the fetch runs only
   in the gateway. For a `message_id` that the ledger holds, it fetches that
   row's objects from the object host in its settings
   (`platforms.email_intake.extra.object_host`). Each `GET` carries
   `X-Webhook-Signature-V2`, the HMAC of `GET.<timestamp>.<path>`. It checks
   each object's size and SHA-256 and writes each file once, mode 0600, into
   the card's attachments directory, beside `email.json`, which holds the
   headers and text. When every file matches, it unblocks the card. A mismatch
   or a failed fetch leaves the card blocked with a comment that names the
   object, and records the failure in the ledger. It takes no URL, host, key,
   or destination from a caller.
6. **Mail run.** The `mail-worker` profile runs Hermes' own agent loop, with no
   terminal and no Claude Code. It reads the files and records one outcome.

### The ledger

The ledger is the only record that decides whether the factory has seen an
email. It is a SQLite table at `<home>/email-intake/ledger.db`, with one row per
`message_id`. The first insert of a `message_id` is the claim, so a second
delivery, a delivery during a gateway restart, or a delivery after the first
card is archived or deleted creates nothing. Hermes' own delivery dedupe lasts
one hour and lives in memory, and Kanban's idempotency key skips archived
cards, so neither of them counts.

Each row moves through `claimed`, `carded`, `fetching`, and then `ready` or
`failed`. Each move is a compare-and-set, so two actors never both make the
same move. When the gateway starts, `email-intake` finishes each row that a
restart interrupted. `check-install` reports a row that failed, a row that
stays unfinished for 30 minutes, and an outcome that stopped partway.

### The three outcomes

The mail run calls `email_record_outcome` once, with one of these outcomes:

| Outcome | When | What it writes |
| --- | --- | --- |
| `duplicate <identifier>` | An open issue already covers the email | A comment on that issue |
| `new-issue <identifier>` | Someone must act, and no issue covers it: an error, a security notice, a deadline, a cost or savings opportunity, or an account action | A Triage issue on the team that `team_key` names |
| `no-action <reason>` | Nothing needs anyone, such as a receipt, a newsletter, or marketing | Nothing in Linear |

The run checks the outcomes in that order: it searches Linear for an issue
that covers the email first. `SOUL.md` in `profiles/mail-worker` gives the
rules. The tool records the outcome in the ledger, then completes the card. It
refuses a second call. Text that the run copies from the email goes below its
own prose, in a fenced block headed `Untrusted email content`.

The tool refuses a write that holds any of these, and the run can retry
without the value:

- A run of 4 to 8 digits that the email gives within 40 characters of `code`,
  `verification`, `OTP`, or `passcode`.
- A link from the email whose path or query holds `reset`, `token`, `verify`,
  or `magic`.
- A string that Hermes' secret redaction would change, such as an API key.

### Tool limits

Hermes adds the full `kanban` toolset to every card run, and the `file`
toolset can write. The `email-outcome` plugin's `pre_tool_call` hook therefore
refuses every tool except these:

- `read_file` and `search_files`, on an absolute path inside the card's
  attachments directory
- `kanban_show`, `kanban_heartbeat`, and `kanban_block`, on the card itself
- `email_issue_search` and `email_record_outcome`

Enable `email-outcome` only in the `mail-worker` home. Its hook would stop
every other home's tools, and `check-install` reports it anywhere else.

## Install the webhook config

```sh
python3 templates/render.py --install-skills
python3 templates/render.py > webhook-config.json
for key in $(python3 -c 'import json, sys; print(*json.load(sys.stdin))' < webhook-config.json); do
  hermes config set "$key" \
    "$(python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]]))' "$key" < webhook-config.json)"
done
```

Run it again after changing the settings, then restart the gateway. Pass
`--settings-json <file>` to render from a file instead of `hermes config get`.

## Check the installed webhook config

```sh
python3 templates/render.py --check
```

It prints one line per route that says whether the installed route matches
the rendered one, and whether its secret is the `${...}` reference. A last
line says whether the installed rate limit matches, and a line per skill says
whether `<home>/skills/<name>/SKILL.md` matches, differs, or is not installed.
It never prints a secret
value. It exits `1` when a route, a skill, or the rate limit differs or is
missing, or a route holds a literal secret.

## Hermes agent memory

`hermes-memory.txt` holds the entries that `memory.py` installs in the memory
file of Hermes' own agent, `<home>/memories/MEMORY.md`. Entries are separated
by a line that holds only `§`, and each starts with a label, such as
`GitHub:`. `<<github_login>>` and `<<gh_config_dir>>` are filled from the
settings and from `--gh-config-dir`. Keep the entries short: Hermes limits the
whole file to 2,200 characters, and the agent adds its own entries.
