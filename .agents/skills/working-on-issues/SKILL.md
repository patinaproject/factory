---
name: working-on-issues
description: Align one issue-linked engineering session with its live tracker, canonical branch, and isolated worktree. Use before issue-linked work begins or resumes.
---

# Working on issues

Resolve exactly one issue per invocation. Run this preflight before editing,
committing, or opening a pull request. Re-running it must converge on the same
issue, branch, worktree, assignee, status, and task title.

Do not read project Markdown to discover tracker behavior. Resolve the provider
from the issue reference, repository, connected tracker tools, and live tracker
data. Prefer an explicit issue URL or identifier, then an issue linked to the
current branch. Reject multiple explicit issue references. Return `no-issue`
when neither source identifies one issue; the caller decides whether work may
continue without one.

## Resolve the tracker contract

Fetch the issue and its native relationships before changing anything. Record
its identifier, URL, title, state, assignee, blockers, and provider-owned branch
name.

Use these provider rules when they apply:

- A GitHub URL or `#N` in a GitHub repository resolves to that repository's
  GitHub issue. Treat GitHub as authoritative even when a Linear mirror exists.
  Read linked development branches with `gh issue develop --list`. If none
  exists, record that the branch must be created after the read-only gates.
- A Linear URL or identifier resolves to Linear. Use the fetched issue's
  `gitBranchName` unchanged.
- For another tracker, use its native issue state, dependency relationships,
  current-user identity, started transition, and recommended development branch.
  Stop if any required operation cannot be resolved unambiguously. Do not invent
  a branch slug or lifecycle mapping.

Resolve a bare reference against the current repository and authenticated
tracker. When more than one provider or issue remains possible, stop and ask the
operator for one explicit issue URL.

## Pass the read-only gates

Apply these gates before assignment, status, branch creation, checkout, or task
renaming:

1. Stop if the issue is missing or completed. An instruction to continue does
   not reopen completed issue work.
2. Fetch native blocking relationships. Stop if their state cannot be read.
   Stop when any blocker is incomplete unless the operator explicitly permits
   blocked work for this invocation.
3. Require a clean isolated checkout. An interactive local session must run in
   a linked Git worktree, not the repository's primary checkout. A disposable
   CI checkout is acceptable only when the CI controller explicitly fixes the
   branch and forbids branch changes. Stop on uncommitted changes.

These gates override general autonomy instructions. Do not turn a blocker into
a guessed workaround.

## Align the branch and worktree

Use the provider-owned branch name exactly. Never add a username, issue key,
prefix, or regenerated title slug.

1. When a GitHub issue has no linked development branch, let
   `gh issue develop` create one without passing `--name`. Use the returned
   branch name.
2. Resolve the default branch from `refs/remotes/origin/HEAD` and fetch it.
3. Check whether the issue branch exists on `origin`. Fetch it when available;
   stop if an existing remote branch cannot be fetched.
4. Inspect `git worktree list --porcelain` before switching branches.
5. If the issue branch belongs to another worktree, stop and ask whether the
   operator wants to run the bundled `move-branch-here` skill. Do not move it
   automatically.
6. If the current worktree already owns the branch, stay there. Otherwise,
   switch to the local branch, create it from the fetched remote branch, or
   create it from the fetched default branch, in that order.
7. Fast-forward from the fetched remote issue branch when it exists. Stop on a
   non-fast-forward update or conflict.

Do not stash, commit, push, force-update, or delete a branch during preflight.

## Mark the work started

Only mutate tracker and session state after every gate and branch check passes.

1. If the issue is unassigned and the authenticated operator is doing the work,
   assign it to that operator. Preserve every existing assignee.
2. If the tracker exposes a native started state, move an active issue to that
   state. For Linear, use `In Progress`. For GitHub, update an unambiguous
   project `Status` field to its in-progress option when one exists. GitHub
   Issues without a project workflow have no started state; keep the issue open
   and report that assignment plus the linked development branch are the
   available start signals.
3. If the host can rename the current chat or task, set its title to
   `<Issue ID> <Issue title>`. Do not duplicate an existing issue-ID prefix.

Retry safe transient failures. If assignment or a supported status transition
still fails, stop and report the failed operation. A missing task-rename
capability is not a blocker; report other rename failures.

## Report

Return the issue identifier, URL, title, provider, ending branch, worktree path,
assignee result, status result, and task-title result. Name any blocker or
unsupported provider operation that stopped the preflight. Never edit the issue
body or decide whether its requested work is sufficiently specified.

When **patina-mode** invokes this skill, also return the machine result defined
in [`../patina-mode/references/issue-handoff.md`](../patina-mode/references/issue-handoff.md).
Return `passed` only after every gate, branch alignment, and supported tracker
mutation completes. Return `failed` before stopping on any gate or failed
mutation. Return `no-issue` when no issue resolves. The caller passes this
result unchanged to the issue handoff gate.
