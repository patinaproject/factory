from __future__ import annotations

import base64
import json
import os
import subprocess

from tests.support import SCRIPTS, GitFixture, git, with_interpreter

REPO = "example-org/example-repo"
GITHUB_URL = f"https://github.com/{REPO}.git"
BOT_NAME = "factory-app[bot]"
BOT_EMAIL = "123+factory-app[bot]@users.noreply.github.com"

FAKE_GH = r"""#!/usr/bin/env python3
import base64, json, os, subprocess, sys

args = sys.argv[1:]
origin = os.environ["FAKE_GH_ORIGIN"]
entry = {"argv": args}
if "--input" in args:
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


method = args[args.index("--method") + 1] if "--method" in args else "GET"
path = next(a for a in args[1:] if a.startswith("repos/"))
fields = dict(v.split("=", 1) for f, v in zip(args, args[1:]) if f in ("-f", "-F"))
body = entry.get("request", {})
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
if method == "POST" and resource == "blobs":
    reply({"sha": git("hash-object", "-w", "--stdin", data=base64.b64decode(body["content"]))})
if method == "POST" and resource == "trees":
    fail_at = os.environ.get("FAKE_GH_FAIL_TREE_NUMBER")
    with open(os.environ["FAKE_GH_LOG"]) as log:
        tree_calls = sum(1 for line in log if prefix + "trees" in json.loads(line)["argv"])
    if fail_at and tree_calls == int(fail_at):
        fail("Service Unavailable (HTTP 503)")
    index = {"GIT_INDEX_FILE": os.environ["FAKE_GH_LOG"] + ".index"}
    git("read-tree", body["base_tree"], env=index)
    for item in body["tree"]:
        if item["sha"] is None:
            git("update-index", "--index-info", env=index, data=f"0 {'0' * 40}\t{item['path']}\n".encode())
        else:
            git("update-index", "--add", "--cacheinfo", f"{item['mode']},{item['sha']},{item['path']}", env=index)
    if os.environ.get("FAKE_GH_STRAY_FILE"):
        blob = git("hash-object", "-w", "--stdin", data=b"stray\n")
        git("update-index", "--add", "--cacheinfo", f"100644,{blob},stray.txt", env=index)
    reply({"sha": git("write-tree", env=index)})
if method == "POST" and resource == "commits":
    parents = [arg for parent in body["parents"] for arg in ("-p", parent)]
    oid = git("commit-tree", body["tree"], *parents, data=body["message"].encode())
    reply({"sha": oid, "tree": {"sha": body["tree"]}})
if method == "PATCH" and resource.startswith("refs/heads/"):
    ref = resource
    move_to = os.environ.get("FAKE_GH_MOVE_REF")
    if move_to:
        git("update-ref", ref, move_to)
    ancestor = subprocess.run(["git", "--git-dir", origin, "merge-base", "--is-ancestor", ref_sha(ref), body["sha"]])
    if body["force"] or ancestor.returncode != 0:
        fail("Update is not a fast forward (HTTP 422)")
    git("update-ref", ref, body["sha"])
    reply({"ref": ref, "object": {"sha": body["sha"]}})
if method == "GET" and resource.startswith("commits/"):
    oid = resource[len("commits/"):]
    reply({"sha": oid, "tree": {"sha": git("rev-parse", oid + "^{tree}")}})
fail(f"unexpected call {args}")
"""


def merge_refusal(sha: str, parents: int, why: str) -> str:
    return (
        f"commit {sha} has {parents} parents ({why}); push-signed replays single-parent commits "
        "and merges whose parents are the branch head and the current head of main, in that order. "
        "Merge origin/main into the branch again, or use gh pr update-branch"
    )


class PushSignedTest(GitFixture):
    def setUp(self) -> None:
        super().setUp()
        git(self.main, "remote", "set-url", "origin", GITHUB_URL)
        git(self.main, "config", f"url.{self.origin}.insteadOf", GITHUB_URL)
        self.seed = git(self.main, "rev-parse", "HEAD")
        self.gh = self.tmp / "fake-gh"
        self.gh.write_text(with_interpreter(FAKE_GH))
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

    def requests(self, resource: str) -> list:
        path = f"repos/{REPO}/git/{resource}"
        return [call["request"] for call in self.gh_calls() if "request" in call and path in call["argv"]]

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
        self.assertEqual(self.gh_argv()[:2], [
            self.ref_get(),
            ["api", "--method", "POST", f"repos/{REPO}/git/refs", "-f", "ref=refs/heads/feature", "-f", f"sha={self.seed}"],
        ])
        self.assertEqual(self.gh_argv()[-1], self.commit_get(remote_second))
        self.assertEqual(self.requests("blobs"), [
            {"content": base64.b64encode(b"one\n").decode(), "encoding": "base64"},
            {"content": base64.b64encode(b"two\n").decode(), "encoding": "base64"},
        ])
        self.assertEqual(self.requests("commits"), [
            {"message": "Add one\n\nExplains one.", "tree": git(self.main, "rev-parse", f"{first}^{{tree}}"), "parents": [self.seed]},
            {"message": "Add two", "tree": git(self.main, "rev-parse", f"{second}^{{tree}}"), "parents": [remote_first]},
        ])
        self.assertEqual(self.requests("refs/heads/feature"), [
            {"sha": remote_first, "force": False},
            {"sha": remote_second, "force": False},
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
        self.assertEqual([commit["parents"] for commit in self.requests("commits")], [[published]])
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
            "repository": REPO, "branch": "feature", "created_branch": False,
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
            "update from the remote (for example git pull --ff-only)"
        )})
        self.assertEqual(self.gh_argv(), [self.ref_get()])
        self.assertEqual(self.origin_branch(), remote)
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), local)

    def assert_refused_before_any_write(self, error: str, branch: str = "feature", reads_base: bool = False) -> None:
        status, report = self.push()

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": error})
        self.assertEqual(self.gh_argv(), [self.ref_get(branch)] + ([self.ref_get("main")] if reads_base else []))
        self.assertIsNone(self.origin_branch(branch))

    def test_merge_commit_is_refused(self) -> None:
        git(self.main, "checkout", "-q", "-b", "side")
        self.commit("side.txt", "side\n", "Add side")
        git(self.main, "checkout", "-q", "-b", "feature", self.seed)
        self.commit("one.txt", "one\n", "Add one")
        git(self.main, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
        merge = git(self.main, "rev-parse", "HEAD")

        self.assert_refused_before_any_write(
            merge_refusal(merge, 2, "its second parent is not the current head of main"), reads_base=True
        )

    def remote_entry(self, path: str) -> str:
        return self.origin_git("ls-tree", self.origin_branch(), "--", path)

    def test_symlinks_are_published_added_renamed_and_deleted(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        os.symlink("README.md", self.main / "link")
        git(self.main, "add", "link")
        git(self.main, "commit", "-q", "-m", "Add link")
        git(self.main, "mv", "link", "moved")
        git(self.main, "commit", "-q", "-m", "Rename link")

        status, report = self.push()

        self.assertEqual(status, 0, report)
        target = self.origin_git("rev-parse", f"{self.origin_branch()}:moved")
        self.assertEqual(self.remote_entry("moved"), f"120000 blob {target}\tmoved")
        self.assertEqual(self.origin_git("cat-file", "blob", target), "README.md")
        self.assertEqual(self.remote_entry("link"), "")
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), self.origin_branch())

        git(self.main, "rm", "-q", "moved")
        git(self.main, "commit", "-q", "-m", "Remove link")

        status, report = self.push()

        self.assertEqual(status, 0, report)
        self.assertEqual(self.remote_entry("moved"), "")
        self.assertEqual(self.requests("trees")[-1]["tree"], [{"path": "moved", "mode": "120000", "type": "blob", "sha": None}])

    def test_a_file_replaced_by_a_symlink_is_published(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        self.commit("guide.md", "guide\n", "Add guide")
        (self.main / "guide.md").unlink()
        os.symlink("README.md", self.main / "guide.md")
        git(self.main, "commit", "-q", "-am", "Point guide at the readme")

        status, report = self.push()

        self.assertEqual(status, 0, report)
        target = self.origin_git("rev-parse", f"{self.origin_branch()}:guide.md")
        self.assertEqual(self.remote_entry("guide.md"), f"120000 blob {target}\tguide.md")
        self.assertEqual(self.origin_git("cat-file", "blob", target), "README.md")

    def test_new_executable_is_refused(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        sha = self.commit("tool.sh", "#!/bin/sh\n", "Add tool", mode=0o755)

        self.assert_refused_before_any_write(
            f"commit {sha} adds executable tool.sh; push-signed publishes no executable bits. "
            "Commit the script without it and run it with its interpreter"
        )

    def test_mode_change_is_refused(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        self.commit("tool.sh", "#!/bin/sh\n", "Add tool")
        os.chmod(self.main / "tool.sh", 0o755)
        git(self.main, "commit", "-q", "-am", "Make tool executable")
        sha = git(self.main, "rev-parse", "HEAD")

        self.assert_refused_before_any_write(f"commit {sha} changes the mode of tool.sh; push-signed publishes no mode changes")

    def test_deleted_files_are_sent_as_deletions(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        git(self.main, "rm", "-q", "README.md")
        self.commit("other.txt", "other\n", "Replace the readme")

        status, report = self.push()

        self.assertEqual(status, 0, report)
        blob = git(self.main, "rev-parse", "HEAD:other.txt")
        self.assertEqual(self.requests("trees")[0]["tree"], [
            {"path": "README.md", "mode": "100644", "type": "blob", "sha": None},
            {"path": "other.txt", "mode": "100644", "type": "blob", "sha": blob},
        ])
        self.assertEqual(self.origin_git("ls-tree", "--name-only", self.origin_branch()), "other.txt")

    def test_executable_bit_removal_is_refused(self) -> None:
        self.commit("tool.sh", "#!/bin/sh\n", "Add tool", mode=0o755)
        git(self.main, "push", "-q", "origin", "main")
        git(self.main, "checkout", "-q", "-b", "feature")
        os.chmod(self.main / "tool.sh", 0o644)
        git(self.main, "commit", "-q", "-am", "Drop the executable bit")
        sha = git(self.main, "rev-parse", "HEAD")

        self.assert_refused_before_any_write(f"commit {sha} changes the mode of tool.sh; push-signed publishes no mode changes")

    def test_symlink_to_executable_is_refused(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        os.symlink("README.md", self.main / "tool.sh")
        git(self.main, "add", "tool.sh")
        git(self.main, "commit", "-q", "-m", "Add link")
        (self.main / "tool.sh").unlink()
        (self.main / "tool.sh").write_text("#!/bin/sh\n")
        os.chmod(self.main / "tool.sh", 0o755)
        git(self.main, "add", "tool.sh")
        git(self.main, "commit", "-q", "-m", "Replace the link with a script")
        sha = git(self.main, "rev-parse", "HEAD")

        self.assert_refused_before_any_write(f"commit {sha} changes the mode of tool.sh; push-signed publishes no mode changes")

    def test_a_directory_replaced_by_a_file_publishes(self) -> None:
        git(self.main, "checkout", "-q", "-b", "feature")
        self.commit("a.txt", "scratch\n", "Add a placeholder")
        (self.main / "a").mkdir()
        (self.main / "a" / "b").write_text("inside\n")
        git(self.main, "add", "a/b")
        git(self.main, "commit", "-q", "-m", "Add a directory")
        git(self.main, "rm", "-q", "a/b")
        (self.main / "a").write_text("now a file\n")
        git(self.main, "add", "a")
        git(self.main, "commit", "-q", "-m", "Replace the directory with a file")

        status, report = self.push()

        self.assertEqual(status, 0, report)
        blob = git(self.main, "rev-parse", "HEAD:a")
        self.assertEqual(self.requests("trees")[-1]["tree"], [
            {"path": "a/b", "mode": "100644", "type": "blob", "sha": None},
            {"path": "a", "mode": "100644", "type": "blob", "sha": blob},
        ])
        self.assertEqual(self.remote_entry("a"), f"100644 blob {blob}\ta")
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), self.origin_branch())

    def partial_publish(self) -> tuple:
        git(self.main, "checkout", "-q", "-b", "feature")
        shas = [self.commit(f"{name}.txt", f"{name}\n", f"Add {name}") for name in ("one", "two", "three")]
        status, report = self.push(env={"FAKE_GH_FAIL_TREE_NUMBER": "2"})
        self.assertEqual(status, 1, report)
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), shas[-1])
        signed = self.origin_branch()
        self.assertEqual(self.origin_git("log", "--format=%s", f"{self.seed}..{signed}"), "Add one")
        return shas, signed

    def test_a_rerun_resumes_after_a_partial_publish(self) -> None:
        shas, signed = self.partial_publish()
        self.log.unlink()

        status, report = self.push()

        self.assertEqual(status, 0, report)
        head = self.origin_branch()
        second = self.origin_git("rev-parse", f"{head}^")
        self.assertEqual(report["published"], [{"local": shas[1], "remote": second}, {"local": shas[2], "remote": head}])
        self.assertEqual(self.origin_git("rev-parse", f"{second}^"), signed)
        self.assertEqual(self.origin_git("log", "--format=%s", f"{self.seed}..{head}").splitlines(), ["Add three", "Add two", "Add one"])
        self.assertEqual([commit["parents"] for commit in self.requests("commits")], [[signed], [second]])
        self.assertEqual([patch["force"] for patch in self.requests("refs/heads/feature")], [False, False])
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), head)

    def test_a_rerun_refuses_when_a_person_added_to_the_partial_publish(self) -> None:
        shas, signed = self.partial_publish()
        tree = self.origin_git("rev-parse", f"{signed}^{{tree}}")
        person = self.origin_commit(tree, signed, "Someone's change", "Someone", "person@example.com")
        self.origin_git("update-ref", "refs/heads/feature", person)
        self.log.unlink()

        status, report = self.push()

        self.assertEqual(status, 2)
        self.assertIn("has commits this branch lacks", report["error"])
        self.assertEqual(self.origin_branch(), person)
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), shas[-1])
        self.assertEqual(self.gh_argv(), [self.ref_get()])

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
        self.assertTrue(report["error"].endswith("gh: Update is not a fast forward (HTTP 422)"), report)
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

    def advance_main(self, name: str, files: dict) -> str:
        other = self.tmp / f"other-{name}"
        git(self.tmp, "clone", "-q", str(self.origin), str(other))
        for path, content in files.items():
            (other / path).write_text(content)
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", name)
        git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
        return git(other, "rev-parse", "HEAD")

    def merge_main(self, resolved: dict) -> str:
        """Merge the fetched default branch, expect a conflict, resolve it by hand, and commit."""
        git(self.main, "fetch", "-q", "origin")
        proc = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "merge", "--no-edit", "origin/main"],
            cwd=self.main, capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        for path, content in resolved.items():
            (self.main / path).write_text(content)
        git(self.main, "add", "-A")
        git(self.main, "commit", "-q", "-m", "Merge main")
        return git(self.main, "rev-parse", "HEAD")

    def conflicted_branch(self) -> tuple:
        """A pushed feature branch whose README conflicts with a base that moved twice."""
        git(self.main, "checkout", "-q", "-b", "feature")
        self.commit("README.md", "feature\n", "Edit the readme")
        self.push()
        pushed = self.origin_branch()
        self.advance_main("mid", {"mid.txt": "mid\n"})
        base = self.advance_main("tail", {"README.md": "main\n", "tail.txt": "tail\n"})
        self.log.unlink()
        return pushed, base

    def parents_of(self, oid: str) -> list:
        return self.origin_git("rev-list", "--parents", "-n", "1", oid).split()[1:]

    def test_a_resolved_merge_of_the_current_base_publishes_one_two_parent_commit(self) -> None:
        pushed, base = self.conflicted_branch()
        merge = self.merge_main({"README.md": "resolved\n"})

        status, report = self.push()

        self.assertEqual(status, 0, report)
        head = self.origin_branch()
        self.assertEqual(report["published"], [{"local": merge, "remote": head}])
        self.assertEqual(self.parents_of(head), [pushed, base])
        self.assertEqual(self.origin_git("rev-parse", f"{head}^{{tree}}"), git(self.main, "rev-parse", f"{merge}^{{tree}}"))
        self.assertEqual(self.origin_git("show", f"{head}:README.md"), "resolved")
        self.assertEqual(self.requests("commits"), [
            {"message": "Merge main", "tree": git(self.main, "rev-parse", f"{merge}^{{tree}}"), "parents": [pushed, base]},
        ])
        self.assertEqual(self.requests("blobs"), [{"content": base64.b64encode(b"resolved\n").decode(), "encoding": "base64"}])
        self.assertEqual(
            sorted((entry["path"], entry["sha"]) for entry in self.requests("trees")[0]["tree"]),
            sorted([
                ("README.md", git(self.main, "rev-parse", f"{merge}:README.md")),
                ("mid.txt", git(self.main, "rev-parse", f"{merge}:mid.txt")),
                ("tail.txt", git(self.main, "rev-parse", f"{merge}:tail.txt")),
            ]),
        )
        self.assertEqual(self.gh_argv()[:2], [self.ref_get(), self.ref_get("main")])
        self.assertEqual([patch["force"] for patch in self.requests("refs/heads/feature")], [False])
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), head)

    def test_a_commit_after_the_merge_publishes_too(self) -> None:
        pushed, base = self.conflicted_branch()
        merge = self.merge_main({"README.md": "resolved\n"})
        after = self.commit("after.txt", "after\n", "Add after")

        status, report = self.push()

        self.assertEqual(status, 0, report)
        head = self.origin_branch()
        signed_merge = self.origin_git("rev-parse", f"{head}^")
        self.assertEqual(report["published"], [{"local": merge, "remote": signed_merge}, {"local": after, "remote": head}])
        self.assertEqual(self.parents_of(signed_merge), [pushed, base])
        self.assertEqual(self.parents_of(head), [signed_merge])
        self.assertEqual(self.origin_git("show", f"{head}:after.txt"), "after")

    def test_a_merge_of_a_base_that_moved_again_is_refused(self) -> None:
        pushed, base = self.conflicted_branch()
        merge = self.merge_main({"README.md": "resolved\n"})
        self.advance_main("newer", {"newer.txt": "newer\n"})

        status, report = self.push()

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": merge_refusal(merge, 2, "its second parent is not the current head of main")})
        self.assertEqual(self.origin_branch(), pushed)
        self.assertEqual(self.gh_argv(), [self.ref_get(), self.ref_get("main")])
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), merge)

    def test_an_octopus_merge_is_refused(self) -> None:
        for name in ("left", "right"):
            git(self.main, "checkout", "-q", "-b", name, self.seed)
            self.commit(f"{name}.txt", f"{name}\n", f"Add {name}")
        git(self.main, "checkout", "-q", "-b", "feature", self.seed)
        self.commit("one.txt", "one\n", "Add one")
        git(self.main, "merge", "-q", "--no-ff", "-m", "Merge both", "left", "right")
        merge = git(self.main, "rev-parse", "HEAD")

        self.assert_refused_before_any_write(merge_refusal(merge, 3, "an octopus merge"), reads_base=True)

    def test_a_merge_with_the_base_as_first_parent_is_refused(self) -> None:
        git(self.main, "checkout", "-q", "-b", "side", self.seed)
        self.commit("side.txt", "side\n", "Add side")
        git(self.main, "checkout", "-q", "-b", "reversed", self.seed)
        git(self.main, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
        merge = git(self.main, "rev-parse", "HEAD")

        self.assert_refused_before_any_write(
            merge_refusal(merge, 2, "its first parent is on main"), branch="reversed", reads_base=True
        )

    def test_a_rerun_resumes_after_a_partial_publish_that_holds_a_merge(self) -> None:
        pushed, base = self.conflicted_branch()
        merge = self.merge_main({"README.md": "resolved\n"})
        after = self.commit("after.txt", "after\n", "Add after")
        status, report = self.push(env={"FAKE_GH_FAIL_TREE_NUMBER": "2"})
        self.assertEqual(status, 1, report)
        signed_merge = self.origin_branch()
        self.assertEqual(self.parents_of(signed_merge), [pushed, base])
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), after)
        self.log.unlink()

        status, report = self.push()

        self.assertEqual(status, 0, report)
        head = self.origin_branch()
        self.assertEqual(report["published"], [{"local": after, "remote": head}])
        self.assertEqual(self.parents_of(head), [signed_merge])
        self.assertEqual([commit["parents"] for commit in self.requests("commits")], [[signed_merge]])
        self.assertEqual(self.gh_argv()[0], self.ref_get())
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), head)
