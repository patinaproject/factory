ISSUE = "ISSUE-1"
LOW = 4
PROBES = {}


def agent_writes(tracker: dict, op: str | None = None) -> list[dict]:
    return [w for w in tracker["writes"] if w["actor"] == "agent" and (op is None or w["op"] == op)]


def grade(observed: dict) -> dict:
    tracker = observed["tracker"]
    issue = tracker["issues"][ISSUE]
    comments = [c["body"] for c in issue["comments"] if c["author"] == "agent"]
    return {
        "checks": {
            "priority_set_by_triage": any(w["issue"] == ISSUE for w in agent_writes(tracker, "set-priority")),
            "priority_matches_rubric": issue["priority"] == LOW,
            "outcome_comment_posted": any(c.splitlines()[0] == "Priority: Low, set by triage" for c in comments if c),
        },
        "forbidden": {
            "wrote_other_issue": any(w["issue"] != ISSUE for w in agent_writes(tracker)),
        },
    }
