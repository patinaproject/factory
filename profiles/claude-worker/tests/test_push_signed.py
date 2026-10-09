from __future__ import annotations

import base64
import json
import os
import subprocess

from tests.support import SCRIPTS, GitFixture, git

REPO = "example-org/example-repo"
GITHUB_URL = f"https://github.com/{REPO}.git"
BOT_NAME = "factory-app[bot]"
BOT_EMAIL = "123+factory-app[bot]@users.noreply.github.com"

FAKE_GH = r"""#!/usr/bin/env python3
import base64, json, os, subprocess, sys

args = sys.argv[1:]
origin = os.environ["FAKE_GH_ORIGIN"]
entry = {"argv": args}
if args[:2] == ["api", "graphql"]:
    with open(args[args.index("--input") + 1]) as handle:
        entry["request"] = json.load(handle)
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")

BOT_ENV = {
    "GIT_AUTHOR_NAME": "factory-app[bot]",
    "GIT_AUTHOR_EMAIL": "123+factory-app[bot]@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "GitHub",
    "GIT_COMMITTER_EMAIL": "noreply@github.com",
}


def git(*a, env=None, data=None):
    proc = subprocess.run(
        ["git", "--git-dir", origin, *a], capture_output=True, input=data,
        env={**os.environ, **BOT_ENV, **(env or {})},
    )
    if proc.returncode != 0:
        fail(proc.stderr.decode().strip())
    return proc.stdout.decode().strip()


def fail(message):
    sys.stderr.write(f"gh: {message}\n")
    sys.exit(1)


def reply(body):
    print(json.dumps(body))
    sys.exit(0)


def ref_sha(ref):
    proc = subprocess.run(["git", "--git-dir", origin, "rev-parse", "--verify", "--quiet", ref], capture_output=True, text=True)
    return proc.stdout.strip() or None


def create_commit(change):
    ref = "refs/heads/" + change["branch"]["branchName"]
    move_to = os.environ.get("FAKE_GH_MOVE_REF")
    if move_to:
        git("update-ref", ref, move_to)
    expected = change["expectedHeadOid"]
    current = ref_sha(ref)
    if current != expected:
        fail(f'Expected branch to point to "{expected}" but it did not. Pull and try again.')
    index = {"GIT_INDEX_FILE": os.environ["FAKE_GH_LOG"] + ".index"}
    git("read-tree", expected + "^{tree}", env=index)
    files = change["fileChanges"]
    for addition in files.get("additions", []):
        blob = git("hash-object", "-w", "--stdin", data=base64.b64decode(addition["contents"]))
        existing = git("ls-tree", expected, "--", addition["path"]).split(" ")[0]
        git("update-index", "--add", "--cacheinfo", f"{existing or '100644'},{blob},{addition['path']}", env=index)
    if os.environ.get("FAKE_GH_STRAY_FILE"):
        blob = git("hash-object", "-w", "--stdin", data=b"stray\n")
        git("update-index", "--add", "--cacheinfo", f"100644,{blob},stray.txt", env=index)
    for deletion in files.get("deletions", []):
        git("update-index", "--index-info", env=index, data=f"0 {'0' * 40}\t{deletion['path']}\n".encode())
    tree = git("write-tree", env=index)
    message = change["message"]["headline"]
    if "body" in change["message"]:
        message += "\n\n" + change["message"]["body"]
    oid = git("commit-tree", tree, "-p", expected, "-m", message)
    git("update-ref", ref, oid, expected)
    reply({"data": {"createCommitOnBranch": {"commit": {"oid": oid}}}})


if args[:2] == ["api", "graphql"]:
    create_commit(entry["request"]["variables"]["input"])
method = args[args.index("--method") + 1] if "--method" in args else "GET"
path = next(a for a in args[1:] if a.startswith("repos/"))
fields = dict(v.split("=", 1) for f, v in zip(args, args[1:]) if f in ("-f", "-F"))
prefix = "repos/" + os.environ["FAKE_GH_REPO"] + "/git/"
if not path.startswith(prefix):
    fail("Not Found (HTTP 404)")
resource = path[len(prefix):]
if method == "GET" and resource.startswith("ref/heads/"):
    sha = ref_sha("refs/" + resource[len("ref/"):])
    if not sha:
        fail("Not Found (HTTP 404)")
    reply({"ref": "refs/" + resource[len("ref/"):], "object": {"sha": sha, "type": "commit"}})
if method == "POST" and resource == "refs":
    git("update-ref", fields["ref"], fields["sha"], "0" * 40)
    reply({"ref": fields["ref"], "object": {"sha": fields["sha"]}})
if method == "PATCH" and resource.startswith("refs/heads/") and fields.get("force") == "true":
    git("update-ref", resource, fields["sha"])
    reply({"ref": resource, "object": {"sha": fields["sha"]}})
if method == "DELETE" and resource.startswith("refs/heads/"):
    git("update-ref", "-d", resource)
    sys.exit(0)
if method == "GET" and resource.startswith("commits/"):
    oid = resource[len("commits/"):]
    reply({"sha": oid, "tree": {"sha": git("rev-parse", oid + "^{tree}")}})
fail(f"unexpected call {args}")
"""


class PushSignedTest(GitFixture):
    def setUp(self) -> None:
        super().setUp()
        git(self.main, "remote", "set-url", "origin", GITHUB_URL)
        git(self.main, "config", f"url.{self.origin}.insteadOf", GITHUB_URL)
        self.seed = git(self.main, "rev-parse", "HEAD")
        self.gh = self.tmp / "fake-gh"
        self.gh.write_text(FAKE_GH)
        os.chmod(self.gh, 0o755)
        self.log = self.tmp / "gh-calls.jsonl"

    def push(self, *extra: str, env: dict | None = None):
        proc = subprocess.run(
            [str(SCRIPTS / "push-signed"), *extra],
            cwd=self.main,
            env={
                **os.environ,
                "GH_BIN": str(self.gh),
                "FAKE_GH_ORIGIN": str(self.origin),
                "FAKE_GH_REPO": REPO,
                "FAKE_GH_LOG": str(self.log),
                **(env or {}),
            },
            capture_output=True, text=True,
        )
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        return proc.returncode, json.loads(lines[0])

    def gh_calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def gh_argv(self) -> list:
        return [call["argv"] for call in self.gh_calls()]

    def graphql_inputs(self) -> list:
        return [call["request"]["variables"]["input"] for call in self.gh_calls() if "request" in call]

    def commit(self, path: str, content: str, message: str, mode: int = 0o644) -> str:
        file = self.main / path
        file.write_text(content)
        os.chmod(file, mode)
        git(self.main, "add", path)
        git(self.main, "commit", "-q", "-m", message)
        return git(self.main, "rev-parse", "HEAD")

    def origin_git(self, *args: str, env: dict | None = None) -> str:
        return subprocess.run(
            ["git", "--git-dir", str(self.origin), *args],
            env={**os.environ, **(env or {})}, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def origin_commit(self, tree: str, parent: str, message: str, name: str, email: str) -> str:
        identity = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                    "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
        return self.origin_git("commit-tree", tree, "-p", parent, "-m", message, env=identity)

    def origin_branch(self, name: str = "feature") -> str | None:
        proc = subprocess.run(["git", "--git-dir", str(self.origin), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
                              capture_output=True, text=True)
        return proc.stdout.strip() or None

    def ref_get(self, branch: str = "feature") -> list:
        return ["api", f"repos/{REPO}/git/ref/heads/{branch}"]

    def commit_get(self, oid: str) -> list:
        return ["api", f"repos/{REPO}/git/commits/{oid}"]

    def test_new_branch_publishes_each_commit_and_adopts_the_signed_history(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        first = self.commit("one.txt", "one\n", "Add one\n\nExplains one.")
        second = self.commit("two.txt", "two\n", "Add two")
        (self.main / "README.md").write_text("uncommitted\n")
        (self.main / "scratch.txt").write_text("untracked\n")

        status, report = self.push()

        self.assertEqual(status, 0, report)
        remote_second = self.origin_branch()
        remote_first = self.origin_git("rev-parse", f"{remote_second}^")
        self.assertEqual(report, {
            "repository": REPO,
            "branch": "feature",
            "created_branch": True,
            "rewritten": False,
            "published": [{"local": first, "remote": remote_first}, {"local": second, "remote": remote_second}],
            "head": remote_second,
        })
        self.assertEqual(self.origin_git("rev-parse", f"{remote_first}^"), self.seed)
        self.assertEqual(
            self.origin_git("log", "--format=%an <%ae>|%s|%b", f"{self.seed}..{remote_second}").splitlines(),
            [f"{BOT_NAME} <{BOT_EMAIL}>|Add two|", f"{BOT_NAME} <{BOT_EMAIL}>|Add one|Explains one."],
        )
        self.assertEqual(self.origin_git("show", f"{remote_second}:one.txt"), "one")
        self.assertEqual(self.origin_git("show", f"{remote_second}:two.txt"), "two")
        self.assertEqual(self.gh_argv(), [
            self.ref_get(),
            ["api", "--method", "POST", f"repos/{REPO}/git/refs", "-f", "ref=refs/heads/feature", "-f", f"sha={self.seed}"],
            ["api", "graphql", "--input", self.gh_argv()[2][3]],
            ["api", "graphql", "--input", self.gh_argv()[3][3]],
            self.commit_get(remote_second),
        ])
        self.assertEqual(self.graphql_inputs(), [
            {
                "branch": {"repositoryNameWithOwner": REPO, "branchName": "feature"},
                "message": {"headline": "Add one", "body": "Explains one."},
                "expectedHeadOid": self.seed,
                "fileChanges": {"additions": [{"path": "one.txt", "contents": base64.b64encode(b"one\n").decode()}], "deletions": []},
            },
            {
                "branch": {"repositoryNameWithOwner": REPO, "branchName": "feature"},
                "message": {"headline": "Add two"},
                "expectedHeadOid": remote_first,
                "fileChanges": {"additions": [{"path": "two.txt", "contents": base64.b64encode(b"two\n").decode()}], "deletions": []},
            },
        ])
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), remote_second)
        self.assertEqual(git(self.main, "symbolic-ref", "--short", "HEAD"), "feature")
        self.assertEqual(git(self.main, "rev-parse", "--abbrev-ref", "feature@{upstream}"), "origin/feature")
        self.assertEqual((self.main / "README.md").read_text(), "uncommitted\n")
        self.assertEqual(git(self.main, "status", "--porcelain"), "M README.md\n?? scratch.txt")

    def test_existing_branch_publishes_only_the_new_commits(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        self.commit("one.txt", "one\n", "Add one")
        self.push()
        published = self.origin_branch()
        self.log.unlink()
        third = self.commit("three.txt", "three\n", "Add three")

        status, report = self.push()

        self.assertEqual(status, 0, report)
        head = self.origin_branch()
        self.assertEqual(report["created_branch"], False)
        self.assertEqual(report["published"], [{"local": third, "remote": head}])
        self.assertEqual(self.origin_git("rev-parse", f"{head}^"), published)
        self.assertEqual([change["expectedHeadOid"] for change in self.graphql_inputs()], [published])
        self.assertEqual(self.gh_argv()[0], self.ref_get())
        self.assertEqual(self.gh_argv()[-1], self.commit_get(head))
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), head)

    def test_remote_with_the_same_tree_is_adopted_without_publishing(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        self.commit("one.txt", "one\n", "Add one")
        git(self.main, "push", "-q", "origin", "HEAD:refs/heads/objects-only")
        self.origin_git("update-ref", "-d", "refs/heads/objects-only")
        signed = self.origin_commit(git(self.main, "rev-parse", "HEAD^{tree}"), self.seed, "Add one", BOT_NAME, BOT_EMAIL)
        self.origin_git("update-ref", "refs/heads/feature", signed)

        status, report = self.push()

        self.assertEqual(status, 0, report)
        self.assertEqual(report, {
            "repository": REPO, "branch": "feature", "created_branch": False, "rewritten": False,
            "published": [], "head": signed,
        })
        self.assertEqual(self.gh_argv(), [self.ref_get(), self.commit_get(signed)])
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), signed)

    def diverge(self, author_email: str) -> tuple:
        git(self.main, "checkout", "-q", "-b", "feature")
        local = self.commit("one.txt", "one\n", "Add one")
        seed_tree = self.origin_git("rev-parse", f"{self.seed}^{{tree}}")
        remote = self.origin_commit(seed_tree, self.seed, "Remote change", "Someone", author_email)
        self.origin_git("update-ref", "refs/heads/feature", remote)
        return local, remote

    def test_diverged_branch_is_refused(self) -> None:
        local, remote = self.diverge("person@example.com")

        status, report = self.push()

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": (
            f"feature on {REPO} has commits this branch lacks; "
            "update from the remote (for example git pull --ff-only) or pass --rewrite"
        )})
        self.assertEqual(self.gh_argv(), [self.ref_get()])
        self.assertEqual(self.origin_branch(), remote)
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), local)

    def test_rewrite_replaces_a_bot_authored_remote(self) -> None:
        local, _ = self.diverge(BOT_EMAIL)

        status, report = self.push("--rewrite")

        self.assertEqual(status, 0, report)
        head = self.origin_branch()
        self.assertEqual(report["rewritten"], True)
        self.assertEqual(report["published"], [{"local": local, "remote": head}])
        self.assertEqual(self.origin_git("rev-list", f"{self.seed}..{head}"), head)
        self.assertEqual(self.gh_argv(), [
            self.ref_get(),
            ["api", "--method", "DELETE", f"repos/{REPO}/git/refs/heads/feature--push-signed"],
            ["api", "--method", "POST", f"repos/{REPO}/git/refs", "-f", "ref=refs/heads/feature--push-signed", "-f", f"sha={self.seed}"],
            ["api", "graphql", "--input", self.gh_argv()[3][3]],
            self.commit_get(head),
            ["api", "--method", "PATCH", f"repos/{REPO}/git/refs/heads/feature", "-f", f"sha={head}", "-F", "force=true"],
            ["api", "--method", "DELETE", f"repos/{REPO}/git/refs/heads/feature--push-signed"],
        ])
        self.assertEqual([change["expectedHeadOid"] for change in self.graphql_inputs()], [self.seed])
        self.assertEqual([change["branch"]["branchName"] for change in self.graphql_inputs()], ["feature--push-signed"])
        self.assertIsNone(self.origin_branch("feature--push-signed"))
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), head)

    def test_rewrite_resigns_a_branch_already_pushed_unsigned(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        published = []
        for name in ("one", "two"):
            self.commit(f"{name}.txt", f"{name}\n", f"Add {name}")
            git(self.main, "commit", "-q", "--amend", "--no-edit", f"--author=Bot <{BOT_EMAIL}>")
            published.append(git(self.main, "rev-parse", "HEAD"))
        first, second = published
        git(self.main, "push", "-q", "origin", "feature")

        status, report = self.push("--rewrite")

        self.assertEqual(status, 0, report)
        head = self.origin_branch()
        self.assertEqual(report["rewritten"], True)
        self.assertEqual([p["local"] for p in report["published"]], [first, second])
        self.assertNotEqual(head, second)
        self.assertNotIn(
            ["api", "--method", "PATCH", f"repos/{REPO}/git/refs/heads/feature", "-f", f"sha={self.seed}", "-F", "force=true"],
            self.gh_argv(),
        )
        self.assertIn(
            ["api", "--method", "PATCH", f"repos/{REPO}/git/refs/heads/feature", "-f", f"sha={head}", "-F", "force=true"],
            self.gh_argv(),
        )
        self.assertEqual(self.origin_git("rev-parse", f"{head}^{{tree}}"), git(self.main, "rev-parse", f"{second}^{{tree}}"))
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), head)

    def test_rewrite_replaces_a_scratch_branch_left_by_an_interrupted_run(self) -> None:
        local, remote = self.diverge(BOT_EMAIL)
        self.origin_git("update-ref", "refs/heads/feature--push-signed", remote)

        status, report = self.push("--rewrite")

        self.assertEqual(status, 0, report)
        self.assertEqual([p["local"] for p in report["published"]], [local])
        self.assertIsNone(self.origin_branch("feature--push-signed"))

    def test_failed_rewrite_leaves_the_pull_request_branch_alone(self) -> None:
        _, remote = self.diverge(BOT_EMAIL)

        status, report = self.push("--rewrite", env={"FAKE_GH_STRAY_FILE": "1"})

        self.assertEqual(status, 2)
        self.assertIn("feature was left unchanged", report["error"])
        self.assertEqual(self.origin_branch(), remote)
        self.assertIsNone(self.origin_branch("feature--push-signed"))
        self.assertEqual(self.gh_argv()[-1], ["api", "--method", "DELETE", f"repos/{REPO}/git/refs/heads/feature--push-signed"])

    def test_rewrite_refuses_to_discard_a_person_commit(self) -> None:
        _, remote = self.diverge("person@example.com")

        status, report = self.push("--rewrite")

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": f"--rewrite would discard commit {remote} by person@example.com, which no bot authored"})
        self.assertEqual(self.gh_argv(), [self.ref_get()])
        self.assertEqual(self.origin_branch(), remote)

    def assert_refused_before_any_write(self, error: str) -> None:
        status, report = self.push()

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": error})
        self.assertEqual(self.gh_argv(), [self.ref_get()])
        self.assertIsNone(self.origin_branch())

    def test_merge_commit_is_refused(self) -> None:
        git(self.main, "checkout", "-q", "-b", "side")
        self.commit("side.txt", "side\n", "Add side")
        git(self.main, "checkout", "-q", "-b", "feature", self.seed)
        self.commit("one.txt", "one\n", "Add one")
        git(self.main, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
        merge = git(self.main, "rev-parse", "HEAD")

        self.assert_refused_before_any_write(
            f"commit {merge} has 2 parents; push-signed replays only single-parent commits. "
            "Bring the base branch in with gh pr update-branch, whose merges GitHub signs"
        )

    def test_symlink_is_refused(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        os.symlink("README.md", self.main / "link")
        git(self.main, "add", "link")
        git(self.main, "commit", "-q", "-m", "Add link")
        sha = git(self.main, "rev-parse", "HEAD")

        self.assert_refused_before_any_write(f"commit {sha} writes symlink link; the API cannot create symlinks")

    def test_new_executable_is_refused(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        sha = self.commit("tool.sh", "#!/bin/sh\n", "Add tool", mode=0o755)

        self.assert_refused_before_any_write(f"commit {sha} adds executable tool.sh; the API cannot set file modes")

    def test_mode_change_is_refused(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        self.commit("tool.sh", "#!/bin/sh\n", "Add tool")
        os.chmod(self.main / "tool.sh", 0o755)
        git(self.main, "commit", "-q", "-am", "Make tool executable")
        sha = git(self.main, "rev-parse", "HEAD")

        self.assert_refused_before_any_write(f"commit {sha} changes the mode of tool.sh; the API cannot set file modes")

    def test_deleted_files_are_sent_as_deletions(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        git(self.main, "rm", "-q", "README.md")
        self.commit("other.txt", "other\n", "Replace the readme")

        status, report = self.push()

        self.assertEqual(status, 0, report)
        self.assertEqual(self.graphql_inputs()[0]["fileChanges"], {
            "additions": [{"path": "other.txt", "contents": base64.b64encode(b"other\n").decode()}],
            "deletions": [{"path": "README.md"}],
        })
        self.assertEqual(self.origin_git("ls-tree", "--name-only", self.origin_branch()), "other.txt")

    def test_default_branch_is_refused(self) -> None:
        self.commit("one.txt", "one\n", "Add one")

        status, report = self.push()

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": "main is the default branch; push-signed publishes only feature branches"})
        self.assertEqual(self.gh_calls(), [])

    def test_moved_remote_head_surfaces_the_api_error(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        local = self.commit("one.txt", "one\n", "Add one")
        seed_tree = self.origin_git("rev-parse", f"{self.seed}^{{tree}}")
        racer = self.origin_commit(seed_tree, self.seed, "Concurrent push", BOT_NAME, BOT_EMAIL)

        status, report = self.push(env={"FAKE_GH_MOVE_REF": racer})

        self.assertEqual(status, 1)
        self.assertTrue(report["error"].endswith(
            f'gh: Expected branch to point to "{self.seed}" but it did not. Pull and try again.'), report)
        self.assertEqual(self.origin_branch(), racer)
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), local)

    def test_remote_tree_mismatch_leaves_the_local_branch_alone(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        local = self.commit("one.txt", "one\n", "Add one")

        status, report = self.push(env={"FAKE_GH_STRAY_FILE": "1"})

        self.assertEqual(status, 2)
        remote = self.origin_branch()
        self.assertEqual(report, {"error": f"remote commit {remote} does not match the tree of {local}; the local branch was left unchanged"})
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), local)
        self.assertEqual((self.main / "one.txt").read_text(), "one\n")
