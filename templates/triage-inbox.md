## Triage inbox

Every issue that lands in the team's Triage status gets one triage card on the
Triage board, whether or not anyone delegated it. Delegation, mentions, and
other events never create one. A triage card runs the readiness check on its
issue. It never implements the issue.

Run every `hermes kanban` command for a triage card with `--board triage`
right after `kanban`, and pass `board: triage` to `kanban_comment`.

1. The issue's triage card is the intent's `triage_card`.
2. If the issue's `state_type` is not `triage`, it left Triage: a person
   moved it or closed it, or its own triage card passed it and moved it to
   Todo. When a triage card exists and its status is not `running`, archive
   it with `<<archive_card>> --board triage <task id>`. Leave a `running`
   card to its worker, which ends it. Then stop this section.
3. If a triage card exists and its status is `done`, the issue is back in
   Triage after an earlier triage. Archive that card as step 2 says and
   continue at step 4. If a triage card exists with any other status, the
   issue already has one. Stop this section.
4. Create a triage card only when this event created the issue (action
   `create`) or changed its status (the previous status above is not a
   dotted name in braces). Otherwise stop this section.
5. Choose the agent session. When `delegated` is true and the issue has an
   `agentSessions` entry whose `appUserId` is `<<app_user_id>>`, use the newest
   such entry. Otherwise open one with
   `linear_agent_session_create` with `purpose` `triage` and use the
   `agent_session_id` it returns.
6. Pick the repository and the priorities as "Implementation card" steps 2
   and 3 say.
7. Create the card with no assignee. `dispatch-triage` assigns it to the
   worker while fewer triage cards run than the factory's cap. Use the
   repository's triage card flags:

   <<triage_card_flags>>

   The title is the identifier, a colon, and the issue title with every
   character except letters, digits, spaces, `.` and `-` removed:

   ````sh
   hermes kanban --board triage create '<title>' \
     --priority <cardPriority> \
     <triage card flags> \
     --idempotency-key 'linear-triage:<issue id>' \
     --body-file - \
     --json <<'CARD'
   ```text
   card: triage
   linear_issue_id: <issue id>
   linear_issue_identifier: <issue identifier>
   linear_issue_url: <issue url>
   linear_agent_session_id: <agent session id>
   repo: <repository full_name>
   ```
   CARD
   ````

8. Post one `action` activity on that agent session with `action`
   `Queued for triage as <task id>` and `parameter`
   `<priorityName> · <repository full_name>`.
