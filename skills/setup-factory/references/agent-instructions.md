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
| Credentials on the machine that Hermes agents must never receive | `<NAME>` of each operator-only token, such as `OP_SERVICE_ACCOUNT_TOKEN` |
| The operator's SST app that owns the ingress, and what deploys it | A repository path, and "merging to the default branch" |
| The SSH hostname and the account people SSH into, when SSH is used | `<ssh-hostname>`, `<account>` |
| The path of this repository's checkout on the machine | `~/factory` |

The private configuration record holds the factory's real values: hostnames,
App identifiers, secret-source paths, service names, and the answers given to
this runbook. It never lives in this repository. If the operator has none yet,
ask them to create one and record this run's inputs in it, with no secret
values.

## Values

Write this factory's values to a JSON file in your scratch directory. The
keys are in
[`agent-instructions.values.example.json`](agent-instructions.values.example.json),
which holds placeholders only. The real file holds this factory's paths and
names, so it never goes in this repository. Keep its contents in the private
configuration record.

A value may span lines. Its continuation lines carry their own indent, so the
render keeps the line breaks the live block has. `worker` and `gateway_home`
are the paths from the runbook's "Where each value lives" section, written out
as the block should show them.

Set these to an empty string or `false` to leave a part out:

- `operator_only`, when no credential is operator-only.
- `onepassword`, when the factory does not use [1Password](1password.md).
- `ssh_account`, when nobody SSHs into the machine. The SSH lines and
  `--ssh-users` then drop out. The other `ssh_*` values stay in the file, empty.

## Check

The block between the markers is rendered from
[`agent-instructions.block.tmpl`](agent-instructions.block.tmpl) and the values
file, and from nothing else. Never edit the live block by hand: change the
template and render it.

```sh
python3 <repo>/skills/setup-factory/scripts/agent-block --values <values file>
```

It prints `PASS` when the live block equals the render, byte for byte. Otherwise
it prints `FAIL` and a diff of the live block against the render, and exits 1.
A missing file, a missing or doubled marker, and a missing or unknown value are
also `FAIL`. If it prints `PASS`, skip to [Verify](#verify).

## Write the block

This replaces the block between the markers, or appends it when the file has
none. It keeps every other line, and it backs up `CLAUDE.md` first when it
changes anything. Then it runs the check.

```sh
python3 <repo>/skills/setup-factory/scripts/agent-block --values <values file> --write
```

`--print` writes the render to stdout and changes nothing.

## Verify

Start a new Claude Code session outside every checkout and ask it about the
machine's rules:

```sh
cd "$(mktemp -d)" && claude -p 'On this machine, how do you run gh, and how do you publish commits?' </dev/null
```

The answer names `GH_CONFIG_DIR=<worker>/gh` and `push-signed`, and it does
not suggest `gh auth login` or `git push`.

Ask a second question in a new session:

```sh
cd "$(mktemp -d)" && claude -p 'On this machine, how do I give a teammate SSH access, and how do you run a command that needs sudo?' </dev/null
```

The answer changes both the SST allowlist and the principals file, runs
`check-host-exposure`, and asks the operator to run the privileged script.
It does not offer to run `sudo` itself.
