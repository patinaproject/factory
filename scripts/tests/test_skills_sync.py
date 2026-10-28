from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skills_sync

SCRIPT = Path(skills_sync.__file__).resolve().parent / "skills-sync"
REPO = "example-org/example-repo"
RELEASE_URL = "https://github.com/patinaproject/skills/releases/tag/{}"
MOVED_COMMIT = "f" * 40

MACHINERY = """#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
while [ $# -gt 0 ]; do
  case "$1" in --repo) repo="$2"; shift 2 ;; *) exit 2 ;; esac
done
mkdir -p "$repo/.claude/agents"
cp "$here"/assets/agents/*.md "$repo/.claude/agents/"
"""

CODE = "plugins/code/skills"
SETUP = f"{CODE}/setup-code-skills"
V1 = {
    f"{CODE}/a/SKILL.md": "# a v1\n",
    f"{CODE}/b/SKILL.md": "# b\n",
    f"{CODE}/b/Zeta.md": "zeta\n",
    f"{CODE}/b/alpha.md": "alpha\n",
    f"{SETUP}/SKILL.md": "# setup\n",
    f"{SETUP}/assets/agents/alpha.md": "alpha v1\n",
    f"{SETUP}/assets/agents/beta.md": "beta v1\n",
    f"{SETUP}/scripts/install-machinery.sh": MACHINERY,
    "plugins/work/skills/retro/SKILL.md": "# retro\n",
    "plugins/work/skills/other/SKILL.md": "# other\n",
}
HASH_A = "e4b41bbb852104326d9a2ea4659a61be3d754cb4052d2ae1f709b429287054a3"
HASH_B = "636db791f0ec2774be1f7f497ca8a5b9854cf4193c60f189ecb16171013bf0d9"
HASH_RETRO = "74d76eadf72aefbd9235778fb6c0886aaa66d0d0f20fd07b7ac67fff35ea9ff6"


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    proc = subprocess.run(["git", "-c", "commit.gpgsign=false", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True, env=env)
    return proc.stdout.strip()


def write(root: Path, path: str, content: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(content)


class Upstream:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "uploadpack.allowAnySHA1InWant", "true")

    def release(self, tag: str, files: dict[str, str], delete: tuple[str, ...] = ()) -> str:
        for path, content in files.items():
            write(self.root, path, content)
        for path in delete:
            target = self.root / path
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", tag)
        git(self.root, "tag", tag)
        return git(self.root, "rev-parse", "HEAD")


class FakeGitHub:
    token = "token"

    def __init__(self) -> None:
        self.release_list: list[dict] = []
        self.tags: dict[str, str] = {}
        self.branches: dict[str, str] = {}
        self.commits: dict[str, dict] = {}
        self.trees: dict[str, list[dict]] = {}
        self.pulls: list[dict] = []
        self.writes: list[str] = []
        self.verified = True
        self.branch_reads = 0
        self.move_branch = False

    def releases(self, repo): return self.release_list
    def tag_commit(self, repo, tag): return self.tags[tag]
    def default_branch(self, repo): return "main"

    def branch_head(self, repo, branch):
        self.branch_reads += 1
        if self.move_branch and self.branch_reads > 1:
            return "moved-by-someone-else"
        return self.branches.get(branch)

    def commit_info(self, repo, sha):
        return {"tree": self.commits[sha]["tree"], "parents": self.commits[sha]["parents"]}

    def create_tree(self, repo, base_commit, entries):
        wire = [(e["path"], e["mode"], None if e["content"] is None else base64.b64encode(e["content"]).decode())
                for e in entries]
        tree = "tree-" + hashlib.sha1(json.dumps(wire).encode()).hexdigest()[:10]
        self.trees[tree] = entries
        return tree

    def create_commit(self, repo, tree, parent, message):
        sha = f"commit-{len(self.commits) + 1}"
        self.commits[sha] = {"tree": tree, "parents": [parent], "message": message}
        self.writes.append("create_commit")
        return {"sha": sha, "verified": self.verified, "reason": "valid" if self.verified else "unsigned"}

    def set_branch(self, repo, branch, sha, exists):
        self.branches[branch] = sha
        self.writes.append("set_branch")

    def open_pull(self, repo, branch):
        for pull in self.pulls:
            if pull["state"] == "open" and pull["head"] == branch:
                return {"number": pull["number"], "url": pull["url"]}
        return None

    def create_pull(self, repo, title, body, head, base):
        number = len(self.pulls) + 1
        pull = {"number": number, "url": f"https://example.test/pull/{number}", "title": title, "body": body,
                "head": head, "base": base, "state": "open"}
        self.pulls.append(pull)
        self.writes.append("create_pull")
        return {"number": number, "url": pull["url"]}

    def update_pull(self, repo, number, title, body):
        self.pulls[number - 1].update(title=title, body=body)
        self.writes.append("update_pull")

    def close_pull(self, repo, number, comment):
        self.pulls[number - 1].update(state="closed", comment=comment)
        self.writes.append("close_pull")


class SkillsSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.upstream = Upstream(self.tmp / "upstream")
        self.gh = FakeGitHub()
        self.targets = 0

    def publish_release(self, tag: str, files: dict[str, str], delete: tuple[str, ...] = (), **flags) -> str:
        commit = self.upstream.release(tag, files, delete)
        self.gh.release_list.append({"tag_name": tag, "draft": False, "prerelease": False,
                                     "html_url": RELEASE_URL.format(tag), **flags})
        self.gh.tags[tag] = commit
        return commit

    def make_target(self, skills: tuple[str, ...] = ("retro",)) -> Path:
        self.targets += 1
        target = self.tmp / f"target{self.targets}"
        target.mkdir()
        git(target, "init", "-q", "-b", "main")
        config = {"source": "patinaproject/skills", "plugins": ["code"], "skills": list(skills)}
        write(target, ".agents/skills-sync.json", json.dumps(config, indent=2) + "\n")
        write(target, "skills-lock.json", json.dumps({"version": 1, "skills": {}}, indent=2) + "\n")
        write(target, ".claude/agents/local-agent.md", "local\n")
        git(target, "add", "-A")
        git(target, "commit", "-q", "-m", "base")
        return target

    def sync(self, target: Path, *args: str, checks: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--target", str(target), "--repo", REPO, "--source-url", f"file://{self.upstream.root}", *args]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = skills_sync.main(argv if checks else [*argv, "--no-checks"], github=self.gh)
        return code, out.getvalue(), err.getvalue()

    def merge(self, target: Path) -> None:
        git(target, "add", "-A")
        git(target, "commit", "-q", "-m", "merge the sync")

    def lock(self, target: Path) -> dict:
        return json.loads((target / "skills-lock.json").read_text())["skills"]

    def test_a_newer_stable_release_is_published_with_pinned_entries_and_agents(self) -> None:
        self.publish_release("v0.9.0", {}, ())
        commit = self.publish_release("v1.0.0", V1)
        self.gh.release_list.append({"tag_name": "v1.1.0-rc.1", "draft": False, "prerelease": True, "html_url": "x"})
        target = self.make_target()

        code, out, err = self.sync(target)

        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("published v1.0.0: https://example.test/pull/1\n"))
        lock = self.lock(target)
        self.assertEqual(sorted(lock), ["a", "b", "retro", "setup-code-skills"])
        self.assertEqual(lock["a"], {"source": "patinaproject/skills", "sourceType": "github", "ref": commit,
                                     "skillPath": f"{CODE}/a/SKILL.md", "computedHash": HASH_A})
        self.assertEqual(lock["b"]["computedHash"], HASH_B)
        self.assertEqual(lock["retro"], {"source": "patinaproject/skills", "sourceType": "github", "ref": commit,
                                         "skillPath": "plugins/work/skills/retro/SKILL.md",
                                         "computedHash": HASH_RETRO})
        self.assertEqual(json.loads((target / ".agents/skills-sync.json").read_text())["release"],
                         {"tag": "v1.0.0", "commit": commit, "url": RELEASE_URL.format("v1.0.0")})
        self.assertEqual(os.readlink(target / ".claude/skills/a"), "../../.agents/skills/a")
        self.assertFalse((target / ".agents/skills/other").exists())
        self.assertEqual(sorted(p.name for p in (target / ".claude/agents").iterdir()),
                         ["alpha.md", "beta.md", "local-agent.md"])

        pull = self.gh.pulls[0]
        self.assertEqual((pull["title"], pull["head"], pull["base"]), ("chore: sync skills v1.0.0", "bot/bump-skills", "main"))
        self.assertIn(f"Release [v1.0.0]({RELEASE_URL.format('v1.0.0')})", pull["body"])
        self.assertIn(f"- Tag: `v1.0.0`\n- Commit: `{commit}`", pull["body"])
        self.assertIn("### Skills added\n\n- `a`\n- `b`\n- `retro`\n- `setup-code-skills`\n", pull["body"])
        self.assertIn("### Skills removed\n\nnone\n", pull["body"])
        self.assertIn("### Agents added\n\n- `alpha.md`\n- `beta.md`\n", pull["body"])
        self.assertIn("### Checks\n\nnot run\n", pull["body"])
        self.assertTrue(out.endswith(pull["body"]))
        branch = self.gh.commits[self.gh.branches["bot/bump-skills"]]
        self.assertEqual(branch["message"], f"chore: sync skills v1.0.0\n\nRelease: {RELEASE_URL.format('v1.0.0')}")
        self.assertEqual(branch["parents"], [git(target, "rev-parse", "HEAD")])
        link = next(e for e in self.gh.trees[branch["tree"]] if e["path"] == ".claude/skills/a")
        self.assertEqual((link["mode"], link["content"]), ("120000", b"../../.agents/skills/a"))

    def test_the_checks_run_before_publishing_and_their_results_are_reported(self) -> None:
        self.publish_release("v1.0.0", V1)
        bin_dir = self.tmp / "bin"
        write(bin_dir, "pnpm", '#!/bin/sh\n[ "$1" = test ]\n')
        (bin_dir / "pnpm").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            code, out, _ = self.sync(self.make_target(), checks=True)

        self.assertEqual(code, 0)
        self.assertIn("### Checks\n\n- `pnpm test`: pass\n- `pnpm lint:md`: fail\n", self.gh.pulls[0]["body"])

    def test_the_same_release_changes_nothing_and_closes_an_obsolete_pull_request(self) -> None:
        self.publish_release("v1.0.0", V1)
        target = self.make_target()
        self.sync(target)
        self.merge(target)

        code, out, err = self.sync(target)

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "no change: the vendored skills and agents already equal v1.0.0\n"
                              "closed the obsolete pull request https://example.test/pull/1\n")
        self.assertEqual(git(target, "status", "--porcelain"), "")
        self.assertEqual(self.gh.pulls[0]["state"], "closed")
        self.assertEqual(len(self.gh.commits), 1)

    def test_a_rerun_on_the_same_base_refreshes_the_pull_request_without_a_new_commit(self) -> None:
        self.publish_release("v1.0.0", V1)
        self.sync(self.make_target())
        self.gh.pulls[0]["body"] = "stale"

        code, out, _ = self.sync(self.make_target())

        self.assertEqual(code, 0)
        self.assertEqual(len(self.gh.commits), 1)
        self.assertEqual(len(self.gh.pulls), 1)
        self.assertIn("### Skills added", self.gh.pulls[0]["body"])

    def test_only_draft_and_prerelease_releases_select_nothing(self) -> None:
        self.publish_release("v2.0.0", V1, draft=True)
        self.publish_release("v1.5.0", {}, prerelease=True)
        target = self.make_target()

        code, out, _ = self.sync(target)

        self.assertEqual(code, 0)
        self.assertEqual(out, "no change: patinaproject/skills has no published stable release "
                              "(ignored: v2.0.0 draft, v1.5.0 prerelease)\n")
        self.assertEqual(git(target, "status", "--porcelain"), "")
        self.assertEqual(self.gh.writes, [])

    def test_a_dispatched_draft_or_prerelease_is_refused(self) -> None:
        self.publish_release("v2.0.0", V1, draft=True)
        self.publish_release("v1.5.0", {}, prerelease=True)
        for tag, reason in (("v2.0.0", "is a draft"), ("v1.5.0", "is a prerelease"), ("v9.9.9", "is not a release")):
            with self.subTest(tag):
                code, _, err = self.sync(self.make_target(), "--tag", tag)
                self.assertEqual(code, 1)
                self.assertIn(f"skills-sync: {tag} {reason}", err)

    def test_the_greatest_version_wins_numerically_and_a_named_tag_overrides_it(self) -> None:
        self.publish_release("v1.9.0", V1)
        self.publish_release("v1.10.0", {f"{CODE}/a/SKILL.md": "# a v110\n"})
        target = self.make_target()

        self.sync(target)
        self.assertEqual(json.loads((target / ".agents/skills-sync.json").read_text())["release"]["tag"], "v1.10.0")

        named = self.make_target()
        self.sync(named, "--tag", "v1.9.0")
        self.assertEqual(json.loads((named / ".agents/skills-sync.json").read_text())["release"]["tag"], "v1.9.0")
        self.assertEqual(self.lock(named)["a"]["computedHash"], HASH_A)

    def test_an_older_dispatched_release_is_refused(self) -> None:
        self.publish_release("v1.0.0", V1)
        self.publish_release("v2.0.0", {f"{CODE}/a/SKILL.md": "# a v2\n"})
        target = self.make_target()
        self.sync(target)
        self.merge(target)

        code, out, err = self.sync(target, "--tag", "v1.0.0")

        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "skills-sync: v1.0.0 is older than the vendored v2.0.0\n")
        self.assertEqual(git(target, "status", "--porcelain"), "")

    def test_a_tag_that_moved_is_refused(self) -> None:
        commit = self.publish_release("v1.0.0", V1)
        target = self.make_target()
        self.sync(target)
        self.merge(target)
        self.gh.tags["v1.0.0"] = MOVED_COMMIT

        code, _, err = self.sync(target)

        self.assertEqual(code, 1)
        self.assertEqual(err, f"skills-sync: tag v1.0.0 now points at {MOVED_COMMIT}, not the vendored {commit}\n")
        self.assertEqual(git(target, "status", "--porcelain"), "")

    def test_an_agent_dropped_upstream_is_removed_and_a_local_agent_stays(self) -> None:
        self.publish_release("v1.0.0", V1)
        target = self.make_target()
        self.sync(target)
        self.merge(target)
        self.publish_release("v2.0.0", {f"{SETUP}/assets/agents/alpha.md": "alpha v2\n"},
                             delete=(f"{SETUP}/assets/agents/beta.md",))

        code, out, _ = self.sync(target)

        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in (target / ".claude/agents").iterdir()), ["alpha.md", "local-agent.md"])
        self.assertEqual((target / ".claude/agents/alpha.md").read_text(), "alpha v2\n")
        body = self.gh.pulls[-1]["body"]
        self.assertIn("### Agents changed\n\n- `alpha.md`\n", body)
        self.assertIn("### Agents removed\n\n- `beta.md`\n", body)
        self.assertIn("### Agents added\n\nnone\n", body)

    def test_a_skill_renamed_upstream_leaves_no_trace_of_the_old_name(self) -> None:
        self.publish_release("v1.0.0", V1)
        target = self.make_target()
        self.sync(target)
        self.merge(target)
        self.publish_release("v2.0.0", {f"{CODE}/a2/SKILL.md": "# a v1\n"}, delete=(f"{CODE}/a",))

        code, _, _ = self.sync(target)

        self.assertEqual(code, 0)
        self.assertFalse((target / ".agents/skills/a").exists())
        self.assertFalse((target / ".claude/skills/a").is_symlink())
        self.assertNotIn("a", self.lock(target))
        self.assertEqual(os.readlink(target / ".claude/skills/a2"), "../../.agents/skills/a2")
        self.assertEqual(self.lock(target)["a2"]["skillPath"], f"{CODE}/a2/SKILL.md")
        self.assertEqual(self.lock(target)["a2"]["computedHash"], HASH_A)
        body = self.gh.pulls[-1]["body"]
        self.assertIn("### Skills added\n\n- `a2`\n", body)
        self.assertIn("### Skills removed\n\n- `a`\n", body)
        self.assertIn("### Skills changed\n\nnone\n", body)
        tree = self.gh.trees[self.gh.commits[self.gh.branches["bot/bump-skills"]]["tree"]]
        removals = {entry["path"]: entry["mode"] for entry in tree if entry["content"] is None}
        self.assertEqual(removals, {".agents/skills/a/SKILL.md": "100644", ".claude/skills/a": "120000"})

    def test_a_skill_the_release_lacks_is_refused_by_name(self) -> None:
        self.publish_release("v1.0.0", V1)
        target = self.make_target(skills=("retro", "triage"))

        code, out, err = self.sync(target)

        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "skills-sync: skill triage is not in the release\n")
        self.assertEqual(self.gh.writes, [])

    def test_a_dry_run_reports_the_delta_and_publishes_nothing(self) -> None:
        self.publish_release("v1.0.0", V1)
        target = self.make_target()

        code, out, _ = self.sync(target, "--dry-run")

        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("would publish v1.0.0\n"))
        self.assertIn("### Skills added\n\n- `a`", out)
        self.assertEqual(self.gh.writes, [])
        self.assertTrue((target / ".agents/skills/a/SKILL.md").is_file())

    def test_a_branch_that_moved_during_the_run_is_not_overwritten(self) -> None:
        self.publish_release("v1.0.0", V1)
        self.gh.move_branch = True

        code, _, err = self.sync(self.make_target())

        self.assertEqual(code, 1)
        self.assertEqual(err, "skills-sync: bot/bump-skills moved from None to moved-by-someone-else during the run\n")
        self.assertEqual(self.gh.writes, [])

    def test_an_unverified_commit_never_becomes_the_branch(self) -> None:
        self.publish_release("v1.0.0", V1)
        self.gh.verified = False

        code, _, err = self.sync(self.make_target())

        self.assertEqual(code, 1)
        self.assertIn("is not verified (unsigned)", err)
        self.assertEqual(self.gh.branches, {})
        self.assertEqual(self.gh.pulls, [])

    def test_the_command_line_launcher_runs_the_sync(self) -> None:
        proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--dry-run", proc.stdout)


if __name__ == "__main__":
    unittest.main()
