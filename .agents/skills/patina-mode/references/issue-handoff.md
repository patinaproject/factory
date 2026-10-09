# Gate issue-linked routing

Issue-linked Patina routing has two executable entries. Direct work enters
through `scripts/issue-routes/direct.ts`. Session pickup enters through
`scripts/issue-routes/session-pickup.ts`. Both call the same handoff gate before
returning a continuation decision. They control the transition from
**working-on-issues** to another Patina playbook. They do not intercept host
tools used outside Patina routing.

## Run the preflight

Run **working-on-issues** for that issue. Keep its normal report and its
machine result. The machine result has one of these shapes:

```json
{
  "kind": "passed",
  "issue": {
    "id": "#123",
    "provider": "github",
    "url": "https://github.com/example/project/issues/123"
  },
  "endingBranch": "123-provider-owned-branch",
  "worktreePath": "/absolute/physical/worktree/path"
}
```

```json
{ "kind": "failed", "issue": "#123", "gate": "blocker" }
```

```json
{ "kind": "no-issue" }
```

Use `failed` for every preflight stop or failed supported mutation. Name the
specific gate or operation in `gate`. Preserve `no-issue` as an explicit
result.

## Authorize the transition

Before another playbook changes a branch, edits a file, creates a commit,
changes a pull request, or starts an operational run, send one route request
as JSON on standard input to the entry that owns the route:

```sh
bun <patina-mode-directory>/scripts/issue-routes/direct.ts
bun <patina-mode-directory>/scripts/issue-routes/session-pickup.ts
```

The executable fixes the entry identity. The request names the next protected
operation and the exact machine result from **working-on-issues**:

```json
{
  "operation": "edit",
  "preflight": {
    "kind": "passed",
    "issue": {
      "id": "#123",
      "provider": "github",
      "url": "https://github.com/example/project/issues/123"
    },
    "endingBranch": "123-provider-owned-branch",
    "worktreePath": "/absolute/physical/worktree/path"
  }
}
```

Do not run `scripts/issue-handoff.ts` directly. It is the shared policy module,
not a route entry. Continue only when the selected entry command exits `0` and
returns `kind: "continue"`.
Keep the returned `receiptPath` with the task evidence. Exit `2` records and
refuses a missing, failed, `no-issue`, or stale-checkout handoff. Exit `1`
means that the request or runtime boundary is invalid.

The gate compares a passed result with the live physical worktree and branch.
Let **working-on-issues** finish canonical branch handling before calling the
gate. A `no-issue` receipt ends the issue-linked route. The caller can then
classify and start a separate nonissue workflow.
