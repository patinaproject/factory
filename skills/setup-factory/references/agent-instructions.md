# Agent instructions

This part writes the factory's operating rules into the user-level Claude Code
instructions file, `~/.claude/CLAUDE.md`, of the user who runs the gateway.
Claude Code reads that file in every session, whatever directory the session
starts in. A session in a configured checkout, the home directory, or a scratch
directory then follows the same rules as the worker.

The rules live in one block between two marker lines. Setup replaces the block
in place and leaves the rest of the file alone, so the operator can keep their
own instructions above or below it.

## Inputs

| Input | Example |
| --- | --- |
| Where the factory's private configuration record lives | A document in the operator's tracker or wiki |
| The directory that holds `gh` and the other tools setup installed | `~/.local/bin` |
| Credentials on the machine that Hermes agents must never receive | `<NAME>` of each operator-only token |

The private configuration record holds the factory's real values: hostnames,
App identifiers, secret-source paths, service names, and the answers given to
this runbook. It never lives in this repository. If the operator has none yet,
ask them to create one and record this run's inputs in it, with no secret
values.

## Check

All of these must hold:

- `grep -c '^<!-- factory:begin -->$' ~/.claude/CLAUDE.md` and
  `grep -c '^<!-- factory:end -->$' ~/.claude/CLAUDE.md` each print `1`.
- The text between the markers equals the [block](#block) below with every
  placeholder filled in from this run's values. No unfilled placeholder,
  such as `<worker>` or `<app-slug>`, is left.
- The block contains no secret value.

If all of them hold, skip to [Verify](#verify).

## Block

Fill in each placeholder. `<worker>` and `<home>` are the paths from the
runbook's "Where each value lives" section, written out in full.

```markdown
<!-- factory:begin -->
## Factory machine

This machine runs a Hermes software factory. Its private configuration
record is <record location>. Read it before you change the machine's
configuration, and update it in the same change.

### GitHub

- Act on GitHub only as the factory's GitHub App, `<app-slug>[bot]`. Run
  every `gh` command as `GH_CONFIG_DIR=<worker>/gh gh <command>`.
- Never run `gh auth login`, never set `GH_TOKEN` or `GITHUB_TOKEN`, and
  never ask the operator to sign in to GitHub.
- Publish commits only with
  `GH_CONFIG_DIR=<worker>/gh python3 <worker>/scripts/push-signed`.
  Never use `git push`.
- `Resource not accessible by integration` means the App lacks a
  permission. Report it to the operator. Do not switch identities.
- The App cannot create linked branches, so `gh issue develop` fails with a
  `createLinkedBranch` permission error. Derive the branch name with the
  repository's issue-tracker rule instead, and create the branch from the
  fetched default branch.

### Tools

- `gh` and the other tools setup installed live in <bin dir>. A
  non-login shell can lack that directory on `PATH`, so call a tool by its
  full path when the bare name is not found. Do not install a second copy.

### Secrets

- Hermes loads the factory's secrets from <secret source> when it starts.
  Never print, copy, or move a secret, and never ask for one in chat. When
  a new secret is needed, ask the operator to enter it in their own
  terminal.
- These credentials are for the operator only, and Hermes agents never
  receive them: <operator-only credentials>.

### Factory processes

- The worker profile `<worker>` owns the checkouts at
  <worker checkout paths>. Change them only through a Kanban card.
- The gateway runs as a service. Restart it only when the operator asks,
  from a shell outside Hermes.
- A merged factory change is not live until it is deployed. Follow the
  setup-factory runbook's "Deploy a merged factory change" section.
  `HERMES_HOME=<worker> python3 <worker>/scripts/check-install` lists what is out of
  date.
<!-- factory:end -->
```

Leave out the "Secrets" line about operator-only credentials when the input
names none.

## Write the block

Write the filled-in block to a file in your scratch directory, then run this.
It replaces an existing block in place, or appends the block when the file has
none, and keeps the file's other lines.

```sh
python3 -I - <block file> <<'EOF'
import pathlib, re, sys
block = pathlib.Path(sys.argv[1]).read_text().strip() + "\n"
target = pathlib.Path.home() / ".claude" / "CLAUDE.md"
target.parent.mkdir(parents=True, exist_ok=True)
text = target.read_text() if target.exists() else ""
pattern = re.compile(r"^<!-- factory:begin -->$.*?^<!-- factory:end -->$\n?", re.M | re.S)
if pattern.search(text):
    text = pattern.sub(lambda _: block, text, count=1)
else:
    text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
target.write_text(text)
EOF
```

Run the [Check](#check) again.

## Verify

Start a new Claude Code session outside every checkout and ask it about the
machine's rules:

```sh
cd "$(mktemp -d)" && claude -p 'On this machine, how do you run gh, and how do you publish commits?' </dev/null
```

The answer names `GH_CONFIG_DIR=<worker>/gh` and `push-signed`, and it does
not suggest `gh auth login` or `git push`.
