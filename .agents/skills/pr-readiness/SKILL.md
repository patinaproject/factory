---
name: pr-readiness
description: Evaluate pull-request evidence against the current candidate and publish the only ready transition.
---

# PR readiness

`pr-readiness` is the single owner of readiness evaluation and publication. It reads GitHub's own signals for the PR and never accepts an observation from a file, a flag, a status, or the PR author. It is an agent's pre-flight, not merge authority. GitHub enforces merge authority through the repository ruleset in [Operator setup](#operator-setup).

```sh
pr-readiness open --repo OWNER/REPO --base BASE --head BRANCH --mode draft|ready
pr-readiness check --repo OWNER/REPO --pr NUMBER
pr-readiness publish --repo OWNER/REPO --pr NUMBER
```

`open` passes the caller's selected creation mode to the forge. `check` is read-only. It prints the JSON report and writes one line per unmet obligation to stderr, naming the command that produces the missing evidence. `publish` evaluates even when the PR is already ready, reads the remote candidate and body again, rejects a race, writes an immutable packet with put-if-absent semantics, and performs the forge ready transition with an expected head. Publishing a draft requires `--takeover=true` (or the trusted automation environment). Neither command posts a status or a check.

## Producers

| Producer | Evidence the evaluator reads | Who can author it |
| --- | --- | --- |
| `checks` | Check runs on the head from the `github-actions` app with the names the policy lists. The newest run in each check suite counts, so a successful re-run replaces a failure. | A GitHub Actions workflow job |
| `approval` | GitHub's `reviewDecision` for the PR, which must be `APPROVED`, plus an approving review whose commit is the head from a `User` with write or admin permission who is not the PR author. Each eligible reviewer's latest decisive review stands until it is dismissed, so a change request on an older commit still fails the obligation. | A `User` with write or admin permission who is not the PR author |

One approval satisfies every obligation that names `approval`. A bot's review never counts. Every factory agent acts as one GitHub App that authors its PRs, so no agent can approve its own work. An agent's code-review result goes on the PR as a `COMMENT` review or as body evidence, and the PR waits for a maintainer's approval. Agents must never hold an operator's credentials, because an approval posted with them would count as the operator's.

## Policy

The policy is `.github/pr-readiness.json`, read through the forge from the default branch. A PR cannot change the policy that judges it, and neither can the parent branch of a stacked PR. It maps each obligation (`standards`, `spec`, `hygiene`, `tests`, `lint`, `behavior`) to a producer.

```json
{ "version": 1, "obligations": { "hygiene": { "producer": "checks", "checks": ["Check diff hygiene"], "allowSkipped": true }, "standards": { "producer": "approval" } } }
```

A repository without the file, and any obligation the file omits, requires `approval`. A malformed file, an unknown obligation, or an unknown producer fails the evaluation. `allowSkipped` makes a check that concluded `skipped` count as not run, and an obligation whose checks all skipped is not applicable. Use it only for jobs whose workflow skips them on purpose, such as the release-please PR. `behavior` does not apply to a documentation-only change, and its evidence from another head is never reused.

## Operator setup

The evaluator fails closed until the default-branch ruleset requires these. Only an operator changes the ruleset.

- **Pull request rule.** Set `required_approving_review_count` to at least 1, and turn on `dismiss_stale_reviews_on_push` and `require_last_push_approval`. Without a required review GitHub computes no `reviewDecision`, and `check` reports that.
- **Required checks.** Require each check the policy names, each pinned to the GitHub Actions integration (`integration_id` 15368).
- **Actions approvals off.** Keep "Allow GitHub Actions to create and approve pull requests" disabled, so `github-actions[bot]` cannot satisfy the required review.

A same-repo PR can still add a workflow job that reports a passing check under a required name, because GitHub runs the PR's own `pull_request` workflows. The required human approval at the head is the control for that, so a reviewer reads workflow changes before approving.

The executable and pure evaluator live under `scripts/`. Forge and packet-store adapters are explicit seams. Production uses the `gh`/`origin` forge adapter and durable filesystem packet store, and tests inject a fake forge.
