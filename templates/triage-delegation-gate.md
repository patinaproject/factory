## Delegation gate

An issue gets an implementation card only after triage marks it
`ready-for-agent`. `claude-session` refuses to start a card whose issue lacks
the label, so never create one. The intent's `gate` holds the verdict, and its
`action` already stops at it. When you write to a person, name a label by its
`role_labels` entry from `linear_issue`, never by the role.

- `gate_ready_for_human`: post one `response` on `agent_session_id` saying that
  triage marked the issue `<role_labels.ready-for-human>` for a person, so the
  factory will not implement it. Create no card and stop.
- `gate_not_ready`: post one `thought` on `agent_session_id` saying that the
  work starts once the issue passes the readiness check, which marks it
  `<role_labels.ready-for-agent>`. Create no card and stop.

When `agent_session_id` is null, a gate action posts nothing. When the gate
passes, after the issue's card is created or reused and when the issue's
`stateType` is `triage`, move it with `linear_issue_to_todo`. That is the one
status change this prompt makes unasked.
