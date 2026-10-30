## Complete a card whose pull request merged

This applies to a `pull_request` event with action `closed` and merged `True`.
A pull request closed without merging is not a merge: the comment from step 1
is enough, and the card stays as it is.

1. Read `linear_issue_id` from the card body and read the issue with
   `linear_issue`. If the read prints an error, returns nothing, or has no
   `stateType`, run it once more. If that fails too, the state is
   `unreadable`.
2. Run
   `<<card_transition>> merged <task id> --pr '<pull request URL>' --linear-state '<stateType>'`.
   It reads the card's status and completes it, leaves a running card to its
   worker, or, when Hermes refuses the merged pull request, comments why and
   archives the card or holds it for a person. Change the card no other way.
3. It prints one JSON line. Read `linear_agent_session_id` from the card body
   and act on the line's `post`:
   - `response`: post a `response` on that session with
     `linear_agent_activity`, saying the pull request merged. Add one
     link with `external_urls`, labeled `Pull request`, whose URL is the pull
     request URL.
   - `elicitation`: post an `elicitation` on that session whose body is the
     line's `reason`, verbatim.
   - Null: post nothing.

   When it prints `error`, post that error as one `thought` on that session.
