## Delegation gate

An issue gets an implementation card only after triage marks it
`ready-for-agent`. `claude-session` refuses to start a card whose issue lacks
the label, so never create one. Read the issue's `labels` from
`linear_issue`, then take the first rule that matches:

1. The labels include `ready-for-human`. Post one `response` on this agent
   session saying that triage marked the issue for a person, so the factory
   will not implement it. Create no card and stop.
2. The labels do not include `ready-for-agent`. Post one `thought` on this
   agent session saying that the work starts once the issue passes the
   readiness check, which marks it `ready-for-agent`. Create no card and
   stop.
3. Otherwise the gate passes. After the issue's card is created or reused,
   when the issue's `stateType` is `triage`, move it with
   `linear_issue_to_todo`. That is the one status change this prompt makes.
