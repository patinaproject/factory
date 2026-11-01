## Delegation gate

An issue gets an implementation card only after triage marks it
`ready-for-agent` and writes its `<identifier> Agent brief` Document.
`claude-session` refuses to start a card whose issue lacks the label or the
Document, so never create one. The intent's `gate` holds the verdict, and its
`action` already stops at it. When you write to a person, name a label by its
`role_labels` entry from `linear_issue`, never by the role.

- `gate_ready_for_human`: post one `response` on `agent_session_id` saying that
  triage marked the issue `<role_labels.ready-for-human>` for a person, so the
  factory will not implement it. Create no card and stop.
- `gate_not_ready`: an update to the issue, not a delegation or a reply, found
  it not ready. Post one `thought` on `agent_session_id` saying that the work
  starts once the issue passes the readiness check, which marks it
  `<role_labels.ready-for-agent>`. Create no card and stop.

- `gate_start_triage`: the issue has not passed the readiness check and a
  person delegated it or replied in its session, so triage has to run first.
  Create no implementation card and do not move the issue to Todo. Follow
  "Start triage". Then post one `thought` on `agent_session_id`, or on the
  session that "Start triage" chose or opened when it is null, saying that
  triage runs first and that the implementation card starts when triage
  passes. When triage marks the issue `<role_labels.ready-for-agent>`, it
  writes the Agent brief. Stop.

- `gate_needs_brief`: the issue carries `<role_labels.ready-for-agent>` but has
  no Agent brief, so triage has to run first. Create no implementation card and
  do not move the issue to Todo. Follow "Start triage". Then post one `thought`
  on `agent_session_id`, or on the session that "Start triage" chose or opened
  when it is null, saying that triage runs first and writes the issue's Agent
  brief, and that the implementation card starts when triage passes. Do not
  tell anyone to apply `<role_labels.ready-for-agent>`, which the issue already
  carries. Stop.

When `agent_session_id` is null, the other gate actions post nothing. When the gate
passes, after the issue's card is created or reused and when the issue's
`stateType` is `triage`, move it with `linear_issue_to_todo`. That is the one
status change this prompt makes unasked.
