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
`Pull request` whose URL is the pull request URL. When it is refused, run
`gh pr view '<pull request URL>' --json headRefOid` and
`hermes kanban show <task id> --json`, and read the newest event whose `kind`
is `pr_acceptance`. If its payload's `pr_url` is the pull request URL, its
`head_sha` is that `headRefOid`, and an entry in its `checks` has a
`classification` other than `success` or `pending`, finish the card as
"A refused merge" says. Otherwise comment
the refusal with the pull request URL and stop. Never assign the card after
`specify` succeeded.

### A refused merge

A merged pull request never runs its checks again, so Hermes would refuse every
retry. Hermes keeps the latest run of each check suite, not of each check name,
so it can count a failed run that a newer passing run of the same check
replaced. Find out which case this is before you write anything.

The refused checks are the `name` of each entry in that event's `checks` whose
`classification` is not `success` or `pending`. Run
`<<merged_checks>> '<pull request URL>' '<name>' ...` with each distinct refused
name, quoted. It reads the pull request's newest run of each check and prints
JSON with `head_sha`, `real`, and `superseded`. If it prints an error, run it once more.
If that fails too, comment the error on the card and stop. If `head_sha` is not
the event's `head_sha`, comment the refusal with the pull request URL and stop.

- When `real` is not empty, a check's newest run failed. The names are `real`.
- When `real` is empty, every refused check passed on its newest run. Hermes counted
  failed runs that newer passing runs replaced. The names are `superseded`.

1. Comment on the card, followed by the pull request as repository and number.
   Linear triage reads this line when the issue is marked done, and when a
   person replies. For a real failure, comment
   `Merge refused by Hermes: required checks failed: <names>.`. For superseded
   runs only, comment
   `Merge refused by Hermes: Hermes counted failed runs that newer passing runs replaced: <names>.`
2. Read `linear_issue_id` from the card body and read the issue with
   `linear_issue`. Only a read that returns a `stateType` of `completed`
   archives the card: run `<<archive_card>> <task id>` and stop. If the read
   prints an error, returns nothing, or has no `stateType`, run it once more.
   If that fails too, the issue is not known to be done, so never archive the
   card. Go to step 3 and block it, and say in the card comment that the issue
   could not be read.
3. Otherwise block the card once for a person. Run
   `hermes kanban assign <task id> none` first, before any state command,
   because the dispatcher skips an unassigned card. After it, run only the
   commands below. Never hand the card off or assign it: a handoff or an
   assignment writes an event that lifts the `active_pr` hold, and a worker
   would start on a merged pull request. Use the reason for the case:
   - Real failure:
     `Pull request <repository>#<number> merged, but required checks failed: <names>. Is the work done? If it is, mark the Linear issue Done, and the card is archived.`
   - Superseded runs only:
     `Pull request <repository>#<number> merged and its required checks passed on their newest runs. Hermes counted failed runs that newer passing runs replaced: <names>, so it cannot finish the card. Mark the Linear issue Done, and the card is archived.`

   Act on the status from the last `hermes kanban show`:
   - `ready`: run `hermes kanban block <task id> '<reason>' --kind needs_input`.
   - `review`: leave the card in `review`. The review lane skips an unassigned
     card, so nothing starts on it. Change nothing else on the card, then
     post the reason as below.
   - `blocked`: Hermes refuses to block a blocked card again. If the task's
     `block_kind` is null, run the `ready` command, which records the kind
     and posts nothing. Then post the reason as below.

   The block posts the reason on the issue's Linear agent session as an
   `elicitation`. A card blocked for `needs_input` before goes to `triage`
   instead, and that post carries the reason too. When the card was
   `review` or `blocked`, or the block command failed, read
   `linear_agent_session_id` from the card body and post an `elicitation` on
   that session with `linear_agent_activity`, whose body is the reason. Post
   the reason once.
