## Complete a card whose pull request merged

This applies to a `pull_request` event with action `closed` and merged `True`.
A pull request closed without merging is not a merge: the comment from step 1
is enough, and the card stays as it is.

1. Run `<<card_transition>> merged <task id> --pr '<pull request URL>'`.
   It reads the pull request from GitHub and finishes the card whatever its
   check runs say. It completes the card, or leaves a running card to its
   worker, and comments on the card when required checks did not pass. Change
   the card no other way, and never archive it.
2. It prints one JSON line. Read `linear_agent_session_id` from the card body
   and act on the line's `post`:
   - `response`: post a `response` on that session with
     `linear_agent_activity`, saying the pull request merged. Add one
     link with `external_urls`, labeled `Pull request`, whose URL is the pull
     request URL.
   - Null: post nothing.

   When it prints `error`, post that error as one `thought` on that session.
   The `finish-merged` cron job tries the card again every 5 minutes.
