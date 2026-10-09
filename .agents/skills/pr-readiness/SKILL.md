---
name: pr-readiness
description: Evaluate pull-request evidence against the current candidate and publish the only ready transition.
---

# PR readiness

`pr-readiness` is the single owner of readiness evaluation and publication. Evidence producers emit typed observations; this skill selects applicable obligations, validates current or equivalent evidence, checks machine-readable body claims, and stores an immutable readiness packet.

```sh
pr-readiness open --repo OWNER/REPO --base BASE --head BRANCH --mode draft|ready
pr-readiness check --repo OWNER/REPO --pr NUMBER
pr-readiness publish --repo OWNER/REPO --pr NUMBER
```

`open` passes the caller's selected creation mode to the forge. `check` is read-only; the trusted workflow publishes the required `PR readiness` status from its result. `publish` evaluates even when the PR is already ready, reads the remote candidate and body again, rejects a race, writes an immutable packet with put-if-absent semantics, and performs the forge ready transition with an expected head. Publishing a draft requires `--takeover=true` (or the trusted automation environment). A missing body, receipt, observation, or proof fails closed.

The executable and pure evaluator live under `scripts/`. Forge and packet-store adapters are explicit seams; production uses the `gh`/`origin` forge adapter and durable filesystem packet store, while tests inject fakes. Receipts are producer-bound and runtime reuse requires executable artifact, target, runtime, environment, fixtures, scope, and freshness to match. The trusted workflow evaluates base-branch code and ingests an immutable Actions evidence bundle; GitHub's final read is not an atomic CAS, so the required status is re-run on every head change. Missing, expired, mismatched, or unproven observations fail closed. Historical observations accepted through an equivalence rule retain their capture SHA and are reported as `valid-by-equivalence`.

`mark-ready` remains a compatibility wrapper. It translates the legacy Markdown review record into standards and spec observations and delegates to this evaluator; it must not implement a second readiness predicate or own forge operations.
