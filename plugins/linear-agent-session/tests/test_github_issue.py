from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

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
    def __init__(self, current, labels=("Needs triage", "Ready for agent", "bug"), visibility="public"):
        self.current = current
        self.labels = labels
        self.visibility = visibility
        self.calls = []

    def __call__(self, args, stdin):
        self.calls.append((list(args), json.loads(stdin) if stdin else None))
        if args[:2] == ["api", "repos/example-org/example-repo"]:
            return json.dumps({"visibility": self.visibility})
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

    def test_a_label_name_two_labels_match_changes_nothing(self):
        gh = FakeGh(issue(), labels=("Bug", "bug"))
        self.assertEqual(
            self.edit(gh, add_labels=["BUG"]),
            {"error": "more than one label of example-org/example-repo matches 'BUG': Bug, bug"},
        )
        self.assertEqual(gh.patches(), [])

    def test_an_issue_of_a_private_repository_is_refused(self):
        for visibility in ("private", "internal"):
            with self.subTest(visibility=visibility):
                gh = FakeGh(issue(), visibility=visibility)
                self.assertEqual(
                    self.edit(gh, title="New title"),
                    {"error": f"example-org/example-repo is {visibility}; edit its issues in Linear"},
                )
                self.assertEqual([args for args, _ in gh.calls], [["api", "repos/example-org/example-repo"]])

    def test_gh_sees_only_its_own_environment(self):
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded.update(cmd=cmd, env=kwargs["env"])
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        environ = {"PATH": "/bin", "HOME": "/home/f", "GH_CONFIG_DIR": "/gh", "LINEAR_CLIENT_SECRET": "s", "SENTRY_TOKEN": "t"}
        with mock.patch.dict(os.environ, environ, clear=True), mock.patch.object(subprocess, "run", fake_run):
            tools.github_issue.run_gh(["api", "repos/o/r"])
        self.assertEqual(recorded["env"], {"PATH": "/bin", "HOME": "/home/f", "GH_CONFIG_DIR": "/gh"})
        self.assertEqual(recorded["cmd"][1:], ["api", "repos/o/r"])
        self.assertTrue(recorded["cmd"][0].endswith("/.local/bin/gh"))

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


KEY = "5b0e3a1c-8d41-4f6e-9a57-2c1d0e7f9b34:1"


class CreateGh:
    def __init__(self, labels=("Needs triage", "Ready for agent", "bug"), visibility="public"):
        self.labels = labels
        self.visibility = visibility
        self.calls = []

    def __call__(self, args, stdin):
        self.calls.append((list(args), json.loads(stdin) if stdin else None))
        if args[:2] == ["api", "repos/example-org/example-repo"]:
            return json.dumps({"visibility": self.visibility})
        if args[:3] == ["api", "--paginate", "--slurp"]:
            return json.dumps([[{"name": n} for n in self.labels]])
        if args[:3] == ["api", "-X", "POST"]:
            return json.dumps({"html_url": "https://github.com/example-org/example-repo/issues/31", "number": 31})
        raise AssertionError(f"unexpected gh call {args}")

    def posts(self):
        return [(args, stdin) for args, stdin in self.calls if "POST" in args]


class GitHubCreateTest(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.store)

    def create(self, gh, **args):
        args = {"repo": "example-org/example-repo", "title": "New", "body": "Text.", "idempotency_key": KEY, **args}
        return json.loads(tools.run_github_create(args, self.store, gh=gh))

    def test_sends_one_post_on_stdin_with_resolved_labels_and_no_assignee(self):
        gh = CreateGh()
        output = self.create(gh, labels=["needs-triage"])
        self.assertEqual(output, {"url": "https://github.com/example-org/example-repo/issues/31", "number": 31})
        self.assertEqual(
            gh.posts(),
            [(
                ["api", "-X", "POST", "repos/example-org/example-repo/issues", "--input", "-"],
                {"title": "New", "body": "Text.", "labels": ["Needs triage"]},
            )],
        )

    def test_a_repeat_with_the_same_key_creates_nothing_more(self):
        gh = CreateGh()
        first = self.create(gh)
        self.assertEqual(self.create(gh), first)
        self.assertEqual(len(gh.posts()), 1)

    def test_a_private_repository_or_a_bad_repo_creates_nothing(self):
        for visibility in ("private", "internal"):
            with self.subTest(visibility=visibility):
                gh = CreateGh(visibility=visibility)
                self.assertEqual(
                    self.create(gh, idempotency_key=f"{KEY}{visibility}"),
                    {"error": f"example-org/example-repo is {visibility}; create its issues in Linear"},
                )
                self.assertEqual(gh.posts(), [])
        for repo in ("example-org", "a/b/c", "a/..", "a b/c", "https://github.com/a/b"):
            with self.subTest(repo=repo):
                gh = CreateGh()
                self.assertEqual(
                    self.create(gh, repo=repo, idempotency_key=f"{KEY}{repo}"),
                    {"error": f"{repo!r} is not a repository; pass owner/name"},
                )
                self.assertEqual(gh.calls, [])

    def test_an_unknown_or_ambiguous_label_creates_nothing(self):
        gh = CreateGh()
        self.assertEqual(
            self.create(gh, labels=["urgent"]),
            {"error": "example-org/example-repo has no label named 'urgent'; it has: Needs triage, Ready for agent, bug"},
        )
        gh = CreateGh(labels=("Bug", "bug"))
        self.assertEqual(
            self.create(gh, labels=["BUG"], idempotency_key=f"{KEY}b"),
            {"error": "more than one label of example-org/example-repo matches 'BUG': Bug, bug"},
        )
        self.assertEqual(gh.posts(), [])

    def test_people_fields_are_refused_before_gh_runs(self):
        gh = CreateGh()
        self.assertEqual(
            self.create(gh, assignees=["someone"]),
            {"error": "this tool never changes assignee or delegate; drop assignees"},
        )
        self.assertEqual(gh.calls, [])

    def test_a_missing_or_malformed_key_is_refused_before_gh_runs(self):
        message = "pass idempotency_key, a string that starts with a UUID, such as <Activity id>:<item number>"
        for key in (None, "", "not-a-uuid:1", 7):
            with self.subTest(key=key):
                gh = CreateGh()
                self.assertEqual(self.create(gh, idempotency_key=key), {"error": message})
                self.assertEqual(gh.calls, [])

    def test_a_failed_create_is_not_cached(self):
        class Failing(CreateGh):
            def __call__(self, args, stdin):
                if "POST" in args and not self.calls_post:
                    self.calls_post = True
                    raise tools.LinearError("gh api failed")
                return super().__call__(args, stdin)

            calls_post = False

        gh = Failing()
        self.assertEqual(self.create(gh), {"error": "gh api failed"})
        self.assertEqual(self.create(gh)["number"], 31)

    def test_the_schema_requires_the_key_and_defines_no_assignee(self):
        self.assertEqual(
            tools.GITHUB_CREATE_SCHEMA["parameters"]["required"], ["repo", "title", "body", "idempotency_key"]
        )
        self.assertEqual(
            sorted(tools.GITHUB_CREATE_SCHEMA["parameters"]["properties"]),
            ["body", "idempotency_key", "labels", "repo", "title"],
        )


if __name__ == "__main__":
    unittest.main()
