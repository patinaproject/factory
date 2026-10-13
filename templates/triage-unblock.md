## Unblock a card

Read the card's `branch:` and `repo:` lines and run
`gh pr view '<branch>' -R '<repo>' --json state`. When no pull request exists,
run `hermes kanban unblock <task id>`. When one exists, run three commands in
this order: `hermes kanban unblock <task id>`, then
`hermes kanban request-review <task id>`, then
`hermes kanban reopen-review <task id> --reason '<reason>'`, with the reason
the rule that sent you here names. Hermes holds a `ready` card for 24 hours
after a card comment that names a pull request, and a plain unblock does not
lift the hold. The review reopen is the handoff that does, so the worker
starts on the next dispatcher tick.
