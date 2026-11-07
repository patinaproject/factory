---
name: pr-readiness
description: Evaluate pull-request evidence against the current candidate and publish the only ready transition.
---

# PR readiness

`pr-readiness` is the single owner of readiness evaluation and publication. It reads GitHub's own signals for the PR and never accepts an observation from a file, a flag, a status, or the PR author. It is an agent's pre-flight, not merge authority. GitHub enforces merge authority through the repository ruleset in [Operator setup](#operator-setup). Under the [auto-merge](#auto-merge) opt-in, `publish` asks GitHub to merge once that ruleset passes. It never merges directly.

```sh
pr-readiness open --repo OWNER/REPO --base BASE --head BRANCH --mode draft|ready
pr-readiness check --repo OWNER/REPO --pr NUMBER
pr-readiness publish --repo OWNER/REPO --pr NUMBER
```

`open` passes the caller's selected creation mode to the forge. `check` is read-only. It prints the JSON report and writes one line per unmet obligation to stderr, naming the command that produces the missing evidence. `publish` evaluates even when the PR is already ready, reads the remote candidate and body again, rejects a race, writes an immutable packet with put-if-absent semantics, and performs the forge ready transition with an expected head. Publishing a draft requires `--takeover=true` (or the trusted automation environment). Neither command posts a status or a check. With `autoMerge: true`, `publish` then arms auto-merge as [Auto-merge](#auto-merge) describes.

## Producers

| Producer | Evidence the evaluator reads | Who can author it |
| --- | --- | --- |
| `checks` | Check runs on the head from the `github-actions` app with the names the policy lists. The newest run in each check suite counts, so a successful re-run replaces a failure. | A GitHub Actions workflow job |
| `approval` | GitHub's `reviewDecision` for the PR, which must be `APPROVED`, plus an approving review whose commit is the head from a `User` with write or admin permission who is not the PR author. Each eligible reviewer's latest decisive review stands until it is dismissed, so a change request on an older commit still fails the obligation. | A `User` with write or admin permission who is not the PR author |

One approval satisfies every obligation that names `approval`. A bot's review never counts. Every factory agent acts as one GitHub App that authors its PRs, so no agent can approve its own work. An agent keeps its code-review result in its session, and resolves the findings. The result stays off the PR. In an approval-gated repository the PR waits for a maintainer's approval at the head. Agents must never hold an operator's credentials, because an approval posted with them would count as the operator's.

## Policy

The policy is `.github/pr-readiness.json`, read through the forge from the default branch. A PR cannot change the policy that judges it, and neither can the parent branch of a stacked PR. It maps each obligation (`standards`, `spec`, `hygiene`, `tests`, `lint`, `behavior`) to a producer.

```json
{ "version": 1, "obligations": { "hygiene": { "producer": "checks", "checks": ["Check diff hygiene"], "allowSkipped": true }, "standards": { "producer": "approval" } } }
```

A repository without the file, and any obligation the file omits, requires `approval`. A malformed file, an unknown obligation, or an unknown producer fails the evaluation. `allowSkipped` makes a check that concluded `skipped` count as not run, and an obligation whose checks all skipped is not applicable. Use it only for jobs whose workflow skips them on purpose, such as the release-please PR. `behavior` does not apply to a documentation-only change, and its evidence from another head is never reused.

The optional top-level `autoMerge` boolean turns on auto-merge arming. A missing key means off. A value that is not a boolean fails the evaluation.

The policy puts the repository in one of two modes:

- **Approval-gated.** The default. Each obligation without a `checks` mapping requires a maintainer's approval at the head.
- **Checks-only.** Every obligation, `standards` and `spec` included, maps to `checks`. No approval is needed. With `autoMerge: true`, the PR merges once every required check passes.

## Auto-merge

With `autoMerge: true`, `publish` arms GitHub squash auto-merge after a successful ready transition. It calls the GraphQL `enablePullRequestAutoMerge` mutation with `expectedHeadOid` pinned to the published head. It never merges directly.

- **Live rules.** When `publish` will arm, it first reads the base branch's live rules from `repos/{repo}/rules/branches/{base}`, before the ready transition. If a check the policy names is not a required status check there, or the read fails, `publish` fails and leaves the PR unchanged.
- **Default branch only.** It arms only when the PR's base is the default branch. A stacked child publishes without arming.
- **Workflow and policy paths.** It does not arm when the diff touches `.github/workflows/**` or `.github/pr-readiness.json`. It publishes, prints that it skipped auto-merge and why, and exits 0. A person merges that PR.
- **Re-runs.** A PR already armed for squash at the same head is a no-op success. An arm at an older head or with another merge method is replaced. The factory worker hook may already have armed the PR at creation.
- **Arm failure.** If arming fails after the ready transition, `publish` exits non-zero and reports that the PR is ready but auto-merge is not on.

A later push by a writer likely keeps auto-merge on, and GitHub then merges the new head once its required checks pass. Strict up-to-date checks stall auto-merge when the base moves. Babysit or the operator updates the branch, because nothing here does.

No other agent path arms auto-merge. Only `publish` arms it, and only under this opt-in.

## Operator setup

The evaluator fails closed until the default-branch ruleset requires what the mode needs. Only an operator changes the ruleset.

- **Pull request rule.** In an approval-gated repository, set `required_approving_review_count` to at least 1, and turn on `dismiss_stale_reviews_on_push` and `require_last_push_approval`. Without a required review GitHub computes no `reviewDecision`, and `check` reports that.
- **Required checks.** Require each check the policy names, each pinned to the GitHub Actions integration (`integration_id` 15368). A checks-only repository rests on this rule, and `publish` refuses to arm auto-merge until the live rules require every named check.
- **Actions approvals off.** Keep "Allow GitHub Actions to create and approve pull requests" disabled, so `github-actions[bot]` cannot satisfy the required review.

A same-repo PR can still add a workflow job that reports a passing check under a required name, because GitHub runs the PR's own `pull_request` workflows. In an approval-gated repository the required human approval at the head is the control for that, so a reviewer reads workflow changes before approving. Checks-only mode removes that control. So `publish` never arms auto-merge on a diff that touches `.github/workflows/**` or `.github/pr-readiness.json`, and a person merges it.

The executable and pure evaluator live under `scripts/`. Forge and packet-store adapters are explicit seams. Production uses the `gh`/`origin` forge adapter and durable filesystem packet store, and tests inject a fake forge.
