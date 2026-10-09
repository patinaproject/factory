# Issue Publishing

Canonical tracker-agnostic rules for filing and publishing patinaproject
issues. Provider mechanics and ready-state transitions live only in
[the issue-tracker adapter](issue-tracker.md).

## Who applies these rules

These rules describe this repository's tracker adapter and issue body conventions.
Skills may add their own workflow, but provider mechanics and readiness remain
owned by the adapter. Work that is not yet
[ready](issue-tracker.md#when-an-issue-becomes-ready) takes the triage state
instead, and routine work that is not feature-shaped takes the body framing
below rather than a full spec template.

## Body framing

Describe the problem, desired outcome, and context needed for triage. A proposal
and non-goals are optional. Avoid converting incidental implementation details
into requirements. Keep a supplied complete body verbatim.

Acceptance criteria use Given / When / Then and describe observable behavior.

## Fields

- **Title:** short and outcome-oriented, with no identifier prefix or trailing
  period.
- **Labels:** load the live inventory and never invent one.
- **Assignee:** blank unless the filer is taking the work or assignment was
  explicitly confirmed.
- **Lifecycle:** new work enters the adapter's triage state unless it is
  already [ready](issue-tracker.md#when-an-issue-becomes-ready).
- **Planning:** set the adapter's planning fields only for a named delivery
  effort.
- **Relationships:** use native parent, blocker, related, and duplicate fields
  only when explicitly stated.

## Publishing guardrails

- Resolve the destination team and every referenced remote entity.
- Search titles and full text for duplicates before mutation.
- Refuse confirmed credential, private-repository, or customer-data leaks;
  surface ambiguous sensitive content for review.
- Verify the created or updated issue and its native relationships afterward.

Interactive publishing presents the final mutation for approval. Unattended
publishing proceeds only when every value is already unambiguous.
