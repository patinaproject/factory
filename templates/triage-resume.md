## Resume a parked card

Hermes parks a card whose block repeats in `triage`. Hermes' `kanban specify`
is the only Kanban CLI command that leaves `triage`. It asks an auxiliary model
to rewrite the card's title and body, and it moves the card to `ready` before
it exits, so the dispatcher could start a worker on the rewritten text. The
dispatcher skips a `ready` card that has no assignee, so unassign the card
first and assign it again only after the title and body are restored. Run
these commands in this order:

1. `hermes kanban show <task id> --json`. Keep the card's `title` and `body`
   exactly.
2. `hermes kanban assign <task id> none`.
3. `hermes kanban specify <task id>`. If it fails, run
   `hermes kanban assign <task id> claude-worker` and leave the card in
   `triage`.
4. `hermes kanban edit <task id> --title '<kept title>' --body '<kept body>'`,
   so the card keeps the key block that triage and the worker read. Quote
   the text so an apostrophe in it survives the shell. If the command fails,
   run it again. Never assign the card while its title and body are
   the rewritten ones.
5. `hermes kanban assign <task id> claude-worker`. The assignment from no
   assignee is the handoff that lifts Hermes' hold on a card whose comments
   name a pull request, so the worker starts on the next dispatcher tick
   with or without a pull request.
