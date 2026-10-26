from __future__ import annotations

import json
import unittest

from tests import load_plugin

plugin = load_plugin()
tools = plugin.tools

URL = "https://github.com/example-org/example-repo/issues/9"
PATH = "repos/example-org/example-repo/issues/9"


def issue(labels=("Needs triage",), state="open", state_reason=None, title="Old title", body="Old text."):
    return {
        "title": title,
        "body": body,
        "labels": [{"name": n} for n in labels],
        "state": state,
        "state_reason": state_reason,
    }


class FakeGh:
    def __init__(self, current, labels=("Needs triage", "Ready for agent", "bug")):
        self.current = current
        self.labels = labels
        self.calls = []

    def __call__(self, args, stdin):
        self.calls.append((list(args), json.loads(stdin) if stdin else None))
        if args[:2] == ["api", PATH]:
            return json.dumps(self.current)
        if args[:3] == ["api", "--paginate", "--slurp"]:
            return json.dumps([[{"name": n} for n in self.labels]])
        if args[:3] == ["api", "-X", "PATCH"]:
            patch = json.loads(stdin)
            updated = {**self.current, **patch}
            updated["labels"] = [{"name": n} for n in patch.get("labels", [l["name"] for l in self.current["labels"]])]
            return json.dumps(updated)
        raise AssertionError(f"unexpected gh call {args}")

    def patches(self):
        return [(args, stdin) for args, stdin in self.calls if "PATCH" in args]


class GitHubEditTest(unittest.TestCase):
    def edit(self, gh, **args):
        return json.loads(tools.run_github_edit({"url": URL, **args}, gh=gh))

    def test_sends_one_patch_on_stdin_with_only_the_changed_fields(self):
        gh = FakeGh(issue())
        output = self.edit(
            gh, title="New title", body="Old text.", add_labels=["ready-for-agent"], remove_labels=["needs triage"]
        )
        self.assertEqual(
            output,
            {"url": URL, "changed": True, "title": "New title", "labels": ["Ready for agent"], "state": "open",
             "state_reason": None},
        )
        self.assertEqual(
            gh.patches(),
            [(["api", "-X", "PATCH", PATH, "--input", "-"], {"title": "New title", "labels": ["Ready for agent"]})],
        )

    def test_maps_statuses_to_close_and_reopen(self):
        cases = [
            (issue(), "Done", {"state": "closed", "state_reason": "completed"}),
            (issue(), "Canceled", {"state": "closed", "state_reason": "not_planned"}),
            (issue(state="closed", state_reason="completed"), "Canceled", {"state": "closed", "state_reason": "not_planned"}),
            (issue(state="closed", state_reason="completed"), "Todo", {"state": "open"}),
        ]
        for current, state, patch in cases:
            with self.subTest(state=state, current=current["state"]):
                gh = FakeGh(current)
                self.edit(gh, state=state)
                self.assertEqual([stdin for _, stdin in gh.patches()], [patch])

    def test_an_issue_that_already_matches_is_not_patched(self):
        gh = FakeGh(issue(state="closed", state_reason="completed"))
        output = self.edit(gh, title="Old title", add_labels=["needs-triage"], state="done")
        self.assertFalse(output["changed"])
        self.assertEqual(gh.patches(), [])

    def test_an_unknown_label_or_status_changes_nothing(self):
        gh = FakeGh(issue())
        self.assertEqual(
            self.edit(gh, add_labels=["urgent"]),
            {"error": "example-org/example-repo has no label named 'urgent'; it has: Needs triage, Ready for agent, bug"},
        )
        self.assertEqual(
            self.edit(gh, state="In Review"),
            {"error": "a GitHub issue has no status 'In Review'; name Done, Canceled, or Todo"},
        )
        self.assertEqual(gh.patches(), [])

    def test_refuses_people_a_pull_request_and_an_empty_request_before_gh_runs(self):
        gh = FakeGh(issue())
        self.assertEqual(
            self.edit(gh, title="x", assignees=["someone"]),
            {"error": "this tool never changes assignee or delegate; drop assignees"},
        )
        self.assertEqual(
            json.loads(tools.run_github_edit({"url": URL.replace("issues", "pull"), "title": "x"}, gh=gh)),
            {"error": f"{URL.replace('issues', 'pull')} is not a GitHub issue URL"},
        )
        self.assertEqual(self.edit(gh), {"error": "pass title, body, add_labels, remove_labels, or state"})
        self.assertEqual(gh.calls, [])


if __name__ == "__main__":
    unittest.main()
