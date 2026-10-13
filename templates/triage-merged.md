## Complete a card whose pull request merged

This applies to a `pull_request` event with action `closed` and merged `True`.
A pull request closed without merging is not a merge: the comment from step 1
is enough, and the card stays as it is.

`hermes kanban complete` and `kanban_complete` accept only a `running`, `ready`,
`blocked`, or `review` card, and refuse a `triage` or `todo` card. So the
commands depend on the card's status. Run `hermes kanban show <task id> --json`
just before you act and read `status` from it:

- `running`: do nothing more. Its worker sees the merged state and completes
  the card. Never complete, reclaim, or force a running card.
- `review`, `blocked`, or `ready`: make the completion call below.
- `todo`: run `hermes kanban promote <task id> pull request merged`. If it
  refuses because a parent card is unfinished, comment the refusal on the card
  and stop. Otherwise make the completion call.
- `triage`: Hermes' `kanban specify` is the only Kanban CLI command that leaves
  `triage`, and it rewrites the card's title and body. Run these commands in
  this order:
  1. `hermes kanban show <task id> --json`. Keep the card's `title` and
     `body` exactly.
  2. `hermes kanban assign <task id> none`, so the dispatcher skips the card.
  3. `hermes kanban specify <task id>`. If it fails, run
     `hermes kanban assign <task id> claude-worker`, comment the failure on
     the card, and stop.
  4. `hermes kanban edit <task id> --title '<kept title>' --body '<kept body>'`,
     quoted so an apostrophe survives the shell. If it fails, run it again.
     Never complete the card while its title and body are the rewritten ones.
  5. `hermes kanban show <task id> --json`. If `status` is `todo`, run
     `hermes kanban promote <task id> pull request merged`. If it refuses,
     comment the refusal with the pull request URL and stop. Leave the card
     unassigned: an `assigned` event lifts the `active_pr` hold, and a worker
     would start on a merged pull request.
  6. Make the completion call.
- Any other status, such as `done` or `archived`: the comment from step 1 is
  enough.

The completion call is `kanban_complete` with `task_id`, a one-sentence
`summary` saying the pull request merged, and `metadata` whose `published_pr`
is the pull request URL. Hermes checks the merged pull request's required
checks before it moves the card to `done`, and refuses while a live worker
holds the card. When it is accepted, read `linear_agent_session_id` from the
card body and post a `response` on that session with `linear_agent_activity`,
saying the pull request merged. Set `external_urls` to one link labeled
`Pull request` whose URL is the pull request URL. When it is refused, comment
the refusal with the pull request URL and stop. Never assign the card after
`specify` succeeded.
