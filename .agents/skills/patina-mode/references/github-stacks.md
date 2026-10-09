# Create and verify GitHub stacks

Use GitHub's UI or REST API by default for GitHub-native stacks. Graphite is
optional, never a prerequisite. Keep the resolved Origin workflow for Origin
repositories and its PR operations. Origin operations alone do not prove GitHub
stack membership.

A dependent chain has Git ancestry and PR base targets. A GitHub-native stack
also has explicit stack metadata. A recommendation banner saying PRs can be
stacked is only a suggestion until you confirm creation.

## Check the topology first

Freeze the intended PR numbers in bottom-to-top order. Confirm each child
contains its parent's current tip and inspect the live head and base repositories.

- Same repository. The root PR targets trunk. Each child targets its parent's
  head branch. This chain can become a native stack.
- Fork heads targeting an upstream repository. Keep parent ancestry in the fork,
  but target upstream trunk for every PR. A fork-only parent cannot be a base
  branch in the upstream repository. This remains a dependent chain. GitHub's
  native stacks require all branches in the same repository and do not support
  cross-fork stacks.

Use [Opening a PR](../playbooks/opening-a-pr.md) for PR creation, fork head
parameters, readiness, and the resolved Origin commands. Independent work
branches from trunk and stays outside the chain.

## Create metadata for existing PRs

Choose either the UI or REST route after the topology check. Read existing
membership for every intended PR first. If these PRs already share the intended stack, verify it
instead of creating another. If membership differs, stop and reconcile the
intended order with the topology owner.

In the GitHub UI, open an eligible PR and select the recommendation banner.
Review the dialog, which lists PRs from top to bottom, against your frozen
bottom-to-top list. Confirm to create the stack. Verify that the stack map
appears on each PR and contains exactly the intended PRs in dependency order.

For REST, replace every angle-bracket placeholder before running these commands.
Use open PR numbers from the validated same-repository chain. Creation requires
pull-request write permission. These examples need `gh`, not Graphite or a CLI
extension.

```sh
base_repo='<owner>/<repo>'
bottom_pr='<bottom-pr-number>'
top_pr='<top-pr-number>'

gh api --method GET "repos/$base_repo/stacks" \
  -F "pull_request=$bottom_pr"
```

Repeat that GET for the top PR and every intermediate PR. Proceed only when
every result is empty. If an existing stack matches, skip creation and verify it.

```sh

gh api --method POST "repos/$base_repo/stacks" \
 -F "pull_requests[]=$bottom_pr" \
 -F "pull_requests[]=$top_pr" \
 --jq '{number, base, pull_requests: [.pull_requests[].number]}'
```

Add one `-F "pull_requests[]=<pr-number>"` per intermediate PR, in bottom-to-top
order. The endpoint expects integers. Each child's base ref must equal the
preceding PR's head ref. Record the returned stack `number`.

## Verify membership and order

Read the stack and each member's PR resource after creation. Replace the stack
number with the returned value and repeat the PR command for every member.

```sh
stack_number='<stack-number>'
pr='<pr-number>'

gh api "repos/$base_repo/stacks/$stack_number" \
 --jq '{number, base, pull_requests: [.pull_requests[] | {number, head, base}]}'

gh api "repos/$base_repo/pulls/$pr" \
 --jq '{number, stack, head: {repo: .head.repo.full_name, ref: .head.ref, sha: .head.sha}, base: {repo: .base.repo.full_name, ref: .base.ref, sha: .base.sha}}'
```

Compare the returned `pull_requests` order and membership with the frozen list.
Check the stack trunk, each PR's `stack` membership, head SHA, and base target.
Require agreement between metadata, live refs, and parent ancestry. Missing
metadata, an unexpected member, or a different order leaves the stack unverified.
If the preview feature is unavailable, report that limitation and retain the
dependent chain without claiming a native stack exists.

## Recheck after a topology change

Before retargeting, rebasing, or force-pushing, obtain the required authority and
disarm merge automation for the affected PR and its descendants. Confirm that
they are no longer queued or armed through the active forge before changing refs.
Native stack operations can affect multiple PRs. Keep the existing merge owner
and readiness safeguards.

After any such change, fetch the live refs and repeat the topology and metadata
checks for the affected chain. Record each current head SHA, base SHA, and stable
base-to-head patch ID. Follow [Shipping](../playbooks/shipping.md) step 3.
Changed patches require renewed independent verification. Unchanged patches
retain their code verdict but still require current-head CI and mergeability.
Stack membership and green checks alone never authorize a merge.

GitHub's native merge API is asynchronous. Selecting an upper PR can merge or
queue every PR through that layer. Keep the existing one-at-a-time frontier at
the lowest unmerged PR, and preserve operator-only merge gates where required.
Do not apply legacy synchronous merge endpoints to native stacks.

## Official references

- [Create stacked pull requests](https://docs.github.com/en/pull-requests/how-tos/create-pull-requests/creating-stacked-pull-requests).
- [Stack requirements and fork limitations](https://docs.github.com/en/pull-requests/reference/stacked-pull-requests).
- [REST stack endpoints and schemas](https://docs.github.com/en/rest/pulls/stacks).
- [Stack APIs and asynchronous merge behavior](https://docs.github.com/en/pull-requests/reference/stacked-pull-requests-apis-and-webhooks).
