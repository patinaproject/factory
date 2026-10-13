from __future__ import annotations

import itertools
import json
import subprocess
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

from tests.support import SCRIPTS, FakeGithub

sys.path.insert(0, str(SCRIPTS))
import card_branch  # noqa: E402
from card_branch import Blocked, CardBranch, Owner, Pull, Source  # noqa: E402

X = "pat-12-fix-x"
T = "pat-12-fix-x-takeover"
APP = "factory-app[bot]"
REPO = "example-org/example-repo"
URL = "https://github.com/example-org/example-repo/pull/{}"
F, P = Owner.FACTORY, Owner.PERSON


def pull(number: int, head: str, *, state: str = "OPEN", cross: bool = False, at: str = "2026-10-01") -> Pull:
    return Pull(URL.format(number), state, head, cross, at)


class ChooseTest(unittest.TestCase):
    def test_each_case_of_the_design_gives_one_branch(self) -> None:
        cases = {
            "A no pull request, branch absent": ([], {}, CardBranch(X, None)),
            "B the App created the branch, a person pushed more": ([], {X: F}, CardBranch(X, None)),
            "C a person's pull request on the branch": ([pull(7, X)], {X: P}, CardBranch(T, URL.format(7))),
            "C later, the takeover branch exists": ([pull(7, X)], {X: P, T: F}, CardBranch(T, URL.format(7))),
            "C later, the App's pull request on the takeover branch": (
                [pull(7, X), pull(8, T)], {X: P, T: F}, CardBranch(T, None)),
            "D a person's pull request on another branch": ([pull(7, "y")], {"y": P}, CardBranch(X, URL.format(7))),
            "D with a person on the branch name": (
                [pull(7, "y")], {"y": P, X: P}, CardBranch(T, URL.format(7))),
            "F the App's pull request, first commit by a person": (
                [pull(9, "goodyear/pat-4075-bot-recreated")], {"goodyear/pat-4075-bot-recreated": F},
                CardBranch("goodyear/pat-4075-bot-recreated", None)),
            "G the person's pull request closed": ([pull(7, X, state="CLOSED")], {X: P, T: F}, CardBranch(T, None)),
            "G the person's pull request merged, takeover branch absent": (
                [pull(7, X, state="MERGED")], {X: P}, Blocked(
                    f"A person created origin/{X} and no open pull request exists for the issue. "
                    f"Open a pull request from {X} so the factory takes it over, or rename or delete the branch.")),
            "a person's bare branch, no pull request": ([], {X: P}, Blocked(
                f"A person created origin/{X} and no open pull request exists for the issue. "
                f"Open a pull request from {X} so the factory takes it over, or rename or delete the branch.")),
            "a person holds both names": ([pull(7, X)], {X: P, T: P}, Blocked(
                f"A person created both origin/{X} and origin/{T}, and no pull request "
                "from the factory exists. Rename or delete one of them.")),
            "the newest open pull request is the takeover": (
                [pull(7, X, at="2026-10-01"), pull(8, "y", at="2026-10-05")], {X: P, "y": P},
                CardBranch(T, URL.format(8))),
            "the newest own pull request wins": (
                [pull(8, "a", at="2026-10-01"), pull(9, "b", at="2026-10-05")], {"a": F, "b": F}, CardBranch("b", None)),
        }
        for name, (pulls, owners, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(card_branch.choose(X, pulls, owners), expected)

    def test_choose_never_hands_the_worker_a_branch_it_refuses(self) -> None:
        pull_sets = {
            "none": [],
            "person's on X": [pull(7, X)],
            "person's on Y": [pull(7, "y")],
            "own on X": [pull(7, X)],
            "own on X-takeover": [pull(7, T)],
            "own on a legacy name": [pull(7, "legacy")],
            "closed person's on X": [pull(7, X, state="CLOSED")],
            "cross-repository": [pull(7, X, cross=True)],
        }
        creators = {"own on X": {X: F}, "own on X-takeover": {T: F}, "own on a legacy name": {"legacy": F}}
        options = [None, F, P]
        for (name, pulls), x_owner, t_owner in itertools.product(pull_sets.items(), options, options):
            owners = {**creators.get(name, {}), "y": P}
            owners = {k: v for k, v in owners.items() if v is not None}
            for key, value in ((X, x_owner), (T, t_owner)):
                if value is not None and key not in creators.get(name, {}):
                    owners[key] = value
            with self.subTest(pulls=name, x=x_owner, takeover=t_owner):
                result = card_branch.choose(X, pulls, owners)
                has_own = any(
                    p.state == "OPEN" and not p.cross_repository and owners.get(p.head) is F for p in pulls
                )
                has_takeover_pr = any(p.state == "OPEN" for p in pulls) and not has_own
                x, t = owners.get(X), owners.get(T)
                blocked = not has_own and x is P and t is not F and (not has_takeover_pr or t is P)
                self.assertEqual(isinstance(result, Blocked), blocked)
                if isinstance(result, CardBranch):
                    source = card_branch.adoption(result.takeover_pr is not None, owners.get(result.branch))
                    self.assertIsNot(source, Source.REFUSE)
                    self.assertEqual(result.takeover_pr is not None, has_takeover_pr)

    def test_a_pull_requests_head_is_never_given_the_takeover_suffix(self) -> None:
        result = card_branch.choose(X, [pull(7, T)], {T: P, X: P})
        self.assertEqual(result, Blocked(
            f"A person created both origin/{X} and origin/{T}, and no pull request "
            "from the factory exists. Rename or delete one of them."
        ))

    def test_the_apps_login_matches_in_every_form(self) -> None:
        for login in ("factory-app[bot]", "app/factory-app", "Factory-App[bot]"):
            with self.subTest(login):
                self.assertTrue(card_branch.is_app(login, "factory-app[bot]"))
        for login in ("tlmader", "factory-app", "Factory-App", "factory-app-2[bot]", "other/factory-app"):
            with self.subTest(login):
                self.assertFalse(card_branch.is_app(login, "factory-app[bot]"))

    def test_the_refusal_names_the_branch_triage_would_pick(self) -> None:
        message = card_branch.refusal(X, APP)

        self.assertTrue(message.endswith(f"Triage picks {T} for this state; give the card that branch."))
        self.assertEqual(card_branch.choose(X, [pull(7, X)], {X: P}), CardBranch(T, URL.format(7)))

    def test_the_refusal_for_a_takeover_branch_does_not_name_another_suffix(self) -> None:
        message = card_branch.refusal(T, APP)

        self.assertTrue(message.endswith("A person must rename or delete it, then the card can run again."))
        self.assertNotIn("-takeover-takeover", message)


class OwnerTest(unittest.TestCase):
    @staticmethod
    def gh_with(branches: dict):
        calls = []

        def gh(endpoint: str):
            calls.append(endpoint)
            path = endpoint.split("?")[0]
            if "/branches/" in path:
                if path.rsplit("/branches/", 1)[1] not in branches:
                    raise card_branch.BranchNotFound("Branch not found")
                return {}
            ref = endpoint.split("ref=refs/heads/")[1].split("&")[0]
            return [{"actor": {"login": login}} for login in reversed(branches[ref])][:1]

        gh.calls = calls
        return gh

    def test_a_missing_branch_has_no_owner(self) -> None:
        self.assertIsNone(card_branch.owner(self.gh_with({}), REPO, X, APP))

    def test_the_branch_the_app_created_is_the_factorys(self) -> None:
        for actor in ("factory-app[bot]", "app/factory-app"):
            with self.subTest(actor):
                self.assertIs(card_branch.owner(self.gh_with({X: [actor]}), REPO, X, APP), Owner.FACTORY)

    def test_a_branch_a_person_created_is_a_persons(self) -> None:
        self.assertIs(card_branch.owner(self.gh_with({X: ["tlmader"]}), REPO, X, APP), Owner.PERSON)

    def test_a_person_who_recreated_the_branch_owns_it(self) -> None:
        self.assertIs(card_branch.owner(self.gh_with({X: [APP, "tlmader"]}), REPO, X, APP), Owner.PERSON)

    def test_a_branch_with_no_creation_record_is_a_persons(self) -> None:
        self.assertIs(card_branch.owner(self.gh_with({X: []}), REPO, X, APP), Owner.PERSON)

    def test_a_branch_name_with_a_slash_reads_its_own_creation_record(self) -> None:
        gh = self.gh_with({"goodyear/pat-1-fix": [APP]})

        self.assertIs(card_branch.owner(gh, REPO, "goodyear/pat-1-fix", APP), Owner.FACTORY)
        self.assertEqual(gh.calls, [
            f"repos/{REPO}/branches/goodyear/pat-1-fix",
            f"repos/{REPO}/activity?ref=refs/heads/goodyear/pat-1-fix&activity_type=branch_creation&per_page=1",
        ])

    def test_pull_requests_are_parsed_once_at_the_boundary(self) -> None:
        data = {
            "1": {"state": "open", "merged": False, "updated_at": "t1", "head": {"ref": "a", "repo": {"full_name": REPO}}},
            "2": {"state": "closed", "merged": True, "updated_at": "t2", "head": {"ref": "b", "repo": {"full_name": REPO}}},
            "3": {"state": "closed", "merged": False, "updated_at": "t3", "head": {"ref": "c", "repo": {"full_name": REPO}}},
            "4": {"state": "open", "merged": False, "updated_at": "t4", "head": {"ref": "d", "repo": {"full_name": "fork/x"}}},
            "5": {"state": "open", "merged": False, "updated_at": "t5", "head": {"ref": "e", "repo": None}},
        }

        pulls = card_branch.read_pulls(lambda endpoint: data[endpoint.rsplit("/", 1)[1]], REPO, [URL.format(n) for n in data])

        self.assertEqual(pulls, [
            Pull(URL.format(1), "OPEN", "a", False, "t1"),
            Pull(URL.format(2), "MERGED", "b", False, "t2"),
            Pull(URL.format(3), "CLOSED", "c", False, "t3"),
            Pull(URL.format(4), "OPEN", "d", True, "t4"),
            Pull(URL.format(5), "OPEN", "e", True, "t5"),
        ])

    def test_a_pull_request_of_another_repository_is_refused(self) -> None:
        with self.assertRaises(card_branch.GhFailed):
            card_branch.read_pulls(lambda endpoint: {}, REPO, ["https://github.com/someone/else/pull/1"])


class CommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.github = FakeGithub(self.tmp, REPO)

    def run_command(self, *extra: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "card_branch.py"), "--app", APP, "--repo", REPO, "--git-branch", X, *extra],
            env={"PATH": "/usr/bin:/bin", **self.github.env()}, capture_output=True, text=True, timeout=60,
        )
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        return proc.returncode, json.loads(lines[0])

    def test_a_missing_gh_gives_a_json_error_and_exit_1(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "card_branch.py"), "--app", APP, "--repo", REPO, "--git-branch", X],
            env={"PATH": str(self.tmp)}, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(
            json.loads(proc.stdout), {"error": "gh could not run: No such file or directory"})

    def test_a_gh_that_is_not_executable_gives_a_json_error_and_exit_1(self) -> None:
        gh = self.tmp / "gh-not-executable"
        gh.write_text("#!/bin/sh\n")
        gh.chmod(0o644)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "card_branch.py"), "--app", APP, "--repo", REPO, "--git-branch", X],
            env={"PATH": "/usr/bin:/bin", "GH_BIN": str(gh)}, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), {"error": f"{gh} could not run: Permission denied"})

    def test_a_branch_with_no_history_is_picked_as_it_is(self) -> None:
        self.assertEqual(self.run_command(), (0, {"branch": X, "takeover_pr": None}))

    def test_a_persons_pull_request_moves_the_card_to_the_takeover_branch(self) -> None:
        self.github.create_branch(X, "tlmader")
        self.github.add_pull(7, X)

        self.assertEqual(self.run_command("--pull", URL.format(7)), (0, {"branch": T, "takeover_pr": URL.format(7)}))

    def test_the_apps_own_pull_request_is_picked_even_when_a_person_started_the_branch(self) -> None:
        self.github.create_branch("goodyear/pat-4075-bot-recreated", "factory-app[bot]")
        self.github.create_branch(X, "tlmader")
        self.github.add_pull(7, X, state="closed")
        self.github.add_pull(9, "goodyear/pat-4075-bot-recreated")

        status, report = self.run_command("--pull", URL.format(7), "--pull", URL.format(9))

        self.assertEqual((status, report), (0, {"branch": "goodyear/pat-4075-bot-recreated", "takeover_pr": None}))

    def test_a_blocked_card_exits_2_with_the_reason(self) -> None:
        self.github.create_branch(X, "tlmader")

        status, report = self.run_command()

        self.assertEqual(status, 2)
        self.assertEqual(report, {"blocked": (
            f"A person created origin/{X} and no open pull request exists for the issue. "
            f"Open a pull request from {X} so the factory takes it over, or rename or delete the branch."
        )})

    def test_a_failed_read_exits_1_and_never_picks_a_branch(self) -> None:
        status, report = self.run_command("--pull", "https://github.com/someone/else/pull/1")

        self.assertEqual(status, 1)
        self.assertEqual(report, {"error": f"--pull https://github.com/someone/else/pull/1 is not a pull request URL in {REPO}"})


if __name__ == "__main__":
    unittest.main()
