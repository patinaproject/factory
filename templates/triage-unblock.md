## Unblock a card

Read the card's `branch:` and `repo:` lines and run
`<<card_transition>> unblock <task id> --repo '<repo>' --branch '<branch>' --reason '<reason>'`,
with the reason the rule that sent you here names. When the branch has a pull
request, it also hands the card through review, because Hermes holds a `ready`
card for 24 hours after a card comment that names a pull request and only the
review reopen lifts the hold. It prints one JSON line. When it prints `error`,
read `linear_agent_session_id` from the card body and post that error as one
`thought` on that session with `linear_agent_activity`.
