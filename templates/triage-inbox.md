## Triage inbox

Every issue that lands in the team's Triage status gets one triage card on the
Triage board, whether or not anyone delegated it. Delegation, mentions, and
other events never create one. A triage card runs the readiness check on its
issue. It never implements the issue.

Run every `hermes kanban` command for a triage card with `--board triage`
right after `kanban`, and pass `board: triage` to `kanban_comment`.

1. Find the issue's triage card with
   `hermes kanban --board triage list --json`: the task whose `body`
   contains the line `linear_issue_id: <Linear issue UUID>`.
2. If the issue's `stateType` is not `triage`, it left Triage: a person
   moved it or closed it, or its own triage card passed it and moved it to
   Todo. When a triage card exists and its status is not `running`, archive
   it with `<<archive_card>> --board triage <task id>`. Leave a `running`
   card to its worker, which ends it. Then go on with the next step of the
   section that sent you here.
3. If a triage card exists and its status is `done`, the issue is back in
   Triage after an earlier triage. Archive that card as step 2 says and
   continue at step 4. If a triage card exists with any other status, the
   issue already has one. Go on with the next step of the section that sent
   you here.
4. Create a triage card only when this event created the issue (action
   `create`) or changed its status (the previous status above is not a
   dotted name in braces). Otherwise go on with the next step of the section
   that sent you here.
5. Choose the agent session. When the issue's `delegate` is
   `<<app_user_id>>` and it has an `agentSessions` entry whose `appUserId`
   is `<<app_user_id>>`, use the newest such entry. Otherwise open one with
   `linear_agent_session_create` and use the `agent_session_id` it returns.
6. Pick the repository and the priorities as "Session created" steps 3 and 4
   say.
7. Create the card with no assignee. `dispatch-triage` assigns it to the
   worker while fewer triage cards run than the factory's cap. Use the
   repository's triage card flags:

   <<triage_card_flags>>

   The title is the issue's `cardTitle` exactly as `linear_issue` returns it:

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
   `<priorityName> · <repository full_name>`. Then go on with the next step
   of the section that sent you here.
