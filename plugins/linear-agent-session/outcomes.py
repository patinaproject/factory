"""The Linear agent activity type that each card outcome posts."""

ACTIVITY_TYPES = {
    "parked_in_triage": "elicitation",
    "needs_input": "elicitation",
    "capability": "error",
    "review_handoff": "response",
    "re_review": "response",
    "merge_queue": "response",
    "merged": "response",
}


def activity_type(outcome: str) -> str:
    if outcome not in ACTIVITY_TYPES:
        raise ValueError(f"unknown card outcome: {outcome}")
    return ACTIVITY_TYPES[outcome]
