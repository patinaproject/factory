## Resume a parked card

Hermes parks a card whose block repeats in `triage`. Resume it with
`<<card_transition>> resume <task id>`. It runs Hermes' `kanban specify`, the
only Kanban command that leaves `triage`, with the card unassigned, restores
the card's own title and body, and only then assigns it to `claude-worker`, so
the worker starts on the next dispatcher tick. It prints one JSON line. When
it prints `error`, read `linear_agent_session_id` from the card body and post
that error as one `thought` on that session with `linear_agent_activity`,
saying the card must be reopened from the Kanban dashboard.
