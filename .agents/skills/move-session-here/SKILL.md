---
name: move-session-here
description: Ingest a Claude Code, Codex, or T3 Code conversation by thread or session ID and continue it in the current chat. Use when the user asks to move, load, absorb, hand off, or pick up one specific agent session here.
---

# Move a session here

Turn one stored Claude Code or Codex transcript into working context for this
chat. Resolve a T3 Code thread to its provider transcript when needed. This is
an ingestion workflow. Preserve the stored database, WAL, and transcript content.

```text
/move-session-here 9e362a1e-b606-4d60-b3fa-4b6a03d2fcf1
/move-session-here codex://threads/019a1111-2222-7333-8444-555555555555
/move-session-here t3://threads/11111111-1111-4111-8111-111111111111
```

Require exactly one Claude session ID, Codex thread deeplink, or T3 Code thread
deeplink. A bare ID means Claude. A `codex://threads/<id>` deeplink means Codex.
A `t3://threads/<id>` deeplink means T3 Code. Translate a natural-language
request that names a T3 Code thread ID to the T3 Code form.

## Extract the session

Resolve `<skill-directory>` to this skill's installed directory. Create two
temporary files, then run:

```sh
handoff_path="$(mktemp "${TMPDIR:-/tmp}/move-session-here.XXXXXX")"
handoff_error_path="$(mktemp "${TMPDIR:-/tmp}/move-session-here-error.XXXXXX")"
node <skill-directory>/scripts/session-handoff.mjs \
  '<session-reference>' >"$handoff_path" 2>"$handoff_error_path"
```

The helper enumerates directory-guarded config homes with `find`, including
`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `~/.claude*`, and `~/.codex*`. It normalizes
the owning transcript and any Claude subagent sidecars into one JSON document.
It scopes Codex lookup to `sessions/**/rollout-*.jsonl`.

For a T3 Code reference, use a current upstream SQLite CLI (3.33.0 or later,
with `-json` support) as `sqlite3` on `PATH`. The helper queries `~/.t3/userdata/state.sqlite` in one read-only
transaction and resolves its exact provider binding. Set `T3_STATE_DB`
to an absolute regular file to use one custom state database instead. The
helper does not scan other T3 stores. It keeps the T3 thread ID and database
path under `t3`; `format` and `sessionId` still identify the provider transcript.
Some bundled macOS SQLite builds cannot read a WAL-mode database when its
sidecars are absent. On macOS, install upstream SQLite with `brew install sqlite`
and prepend `$(brew --prefix sqlite)/bin` to `PATH` for the handoff command.
A version number alone does not establish that a vendor build supports this case.

Query output is limited to 1 MiB; oversized mappings return `invalid_t3_store`.
SQLite may create an empty `-wal` file and create or update the database's
`-shm` WAL index and locking state. The helper leaves that shared bookkeeping
to SQLite and never checkpoints,
migrates, or writes stored database, WAL, or transcript content.

On a nonzero exit, read the JSON error and stop:

- `not_found` means report the ID and every `searchedHomes` entry.
- `ambiguous_session` means report every match and ask for a unique source.
- `t3_store_not_found` or `t3_mapping_not_found` means report the T3 thread and
  database path.
- `t3_sqlite_unavailable` means install the SQLite CLI and check `PATH`.
- `t3_stale_binding` means the mapped provider transcript is missing.
- `t3_mapping_ambiguous` or `t3_ambiguous_binding` means report the identifying
  details and ask for one unambiguous source.
- Any other error means report its code and identifying details.

The helper's result owns session identity. Do not replace it with substring
search or pick a transcript that merely mentions the ID. Treat a Claude
`bridgeSessionId` as provenance only. Native bridge and resume mechanisms do
not provide a cross-agent handoff.

## Build the continuation brief

Read the normalized document through the final event of every track. Process a
large document in chronological chunks and carry one cumulative brief forward;
skip no chunk. Tool calls show intent, while tool outputs and later messages
show what happened. Prefer the later evidence when they disagree.

Build one private continuation brief with all of these fields:

- Source format, provider session ID, transcript paths, cwd, branch, and model.
- T3 Code thread ID and database path when the result contains `t3`.
- The user's goal and explicit constraints.
- Decisions already made and their recorded rationale.
- Files and other artifacts read, changed, created, or proposed, with their
  last known state.
- Commands, tests, and external operations already run, with outcomes.
- Failed approaches, unresolved risks, blockers, and open questions.
- Active skills, plugins, MCP tools, and subagent findings that affect the work.
- The last completed step and the single next action.

Separate transcript facts from your inferences. Resolve contradictions in favor
of the latest direct result. Replace credentials and other secrets with
`[redacted]` in anything you show or persist.

## Reconcile live state and continue

Inspect the current cwd, branch, worktree status, and every artifact needed for
the next action. The transcript is historical evidence, not current state. Name
any mismatch in the brief and follow the repository's workflow before changing
branches or files.

Tell the user the recovered goal, current state, and next action in a short
capsule. Then continue from that action without asking the user to restate the
session. Stop only for a real permission, product-decision, or safety boundary.

Remove the two temporary files after the brief is established. Leave the T3
state database, its sidecars, and every source transcript in place. Do not clean
up SQLite sidecars. Continue from the derived artifact without a native resume
command.
