from __future__ import annotations

import fcntl
import json
import os
import subprocess
import uuid
from pathlib import Path

from tests.support import SCRIPTS, GitFixture, git

ISSUE_ID = "0b6c2a8e-1111-4222-8333-944455556666"
SESSION_ID = "a62cb49b-8e77-5e0f-ba30-182967469dc0"
PUBLISH_RULE = (
    f"Publish commits only by running {(SCRIPTS / 'push-signed').resolve()} from the worktree, never `git push`. "
    "It creates commits that the GitHub App signs, which repositories requiring verified signatures need. "
    "To bring in the base branch, run `gh pr update-branch`, never a local merge."
)
REPO = "example-org/example-repo"
DEFAULT_ENTRY = (
    "Work Linear issue ABC-123 (https://linear.app/example/issue/ABC-123) in example-org/example-repo "
    "on branch abc-123-fix. First start the issue through this repository's own issue-start workflow, "
    "which moves it to In Progress. Open a ready (non-draft) pull request and drive it until it is ready to merge."
)


class ClaudeSessionTest(GitFixture):
    def setUp(self) -> None:
        super().setUp()
        self.worktree = self.add_worktree("abc-123-fix")
        self.claude = self.make_fake_claude()
        self.log = self.tmp / "claude-call.json"
        self.claude_config = self.tmp / "claude-config"
        self.nvm_dir = self.tmp / "nvm"
        self.repository = {"full_name": REPO, "path": str(self.main), "routing": "default", "worker_entry": ""}
        self.settings = self.write_settings([self.repository], {"max_turns": 40, "permission_mode": "bypassPermissions"})

    def run_session(self, *extra: str, workspace: Path | None = None, env: dict | None = None):
        base = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        base.update(
            HERMES_KANBAN_WORKSPACE=str(workspace or self.worktree),
            CLAUDE_BIN=str(self.claude),
            CLAUDE_CONFIG_DIR=str(self.claude_config),
            NVM_DIR=str(self.nvm_dir),
            FAKE_CLAUDE_LOG=str(self.log),
        )
        base.update(env or {})
        proc = subprocess.run(
            [
                str(SCRIPTS / "claude-session"),
                "--issue-id", ISSUE_ID,
                "--issue-identifier", "ABC-123",
                "--issue-url", "https://linear.app/example/issue/ABC-123",
                "--repo", REPO,
                "--settings-json", str(self.settings),
                *extra,
            ],
            env=base, capture_output=True, text=True,
        )
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        return proc.returncode, json.loads(lines[0])

    def claude_call(self) -> dict:
        return json.loads(self.log.read_text())

    def write_transcript(self, worktree: Path | None = None) -> None:
        project = self.claude_config / "projects" / f"project-{uuid.uuid4()}"
        project.mkdir(parents=True)
        cwd = str((worktree or self.worktree).resolve())
        lines = [{"type": "summary"}, {"type": "user", "cwd": cwd}, {"type": "assistant", "cwd": "/elsewhere"}]
        (project / f"{SESSION_ID}.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))

    def test_first_run_starts_a_pinned_session_with_the_default_entry(self) -> None:
        status, report = self.run_session()

        self.assertEqual(status, 0)
        self.assertEqual(report, {
            "session_id": SESSION_ID,
            "resumed": False,
            "worktree": str(self.worktree),
            "branch": "abc-123-fix",
            "subtype": "success",
            "num_turns": 7,
            "result": "Opened the pull request.",
        })
        call = self.claude_call()
        self.assertEqual(call["argv"], [
            "-p", DEFAULT_ENTRY,
            "--output-format", "json",
            "--max-turns", "40",
            "--permission-mode", "bypassPermissions",
            "--session-id", SESSION_ID,
            "--append-system-prompt", PUBLISH_RULE,
        ])
        self.assertEqual(Path(call["cwd"]).resolve(), self.worktree)

    def test_resume_uses_the_message_file_when_a_transcript_exists(self) -> None:
        self.write_transcript()
        message = self.tmp / "message.txt"
        message.write_text("Reviewer asked for a test.")

        status, report = self.run_session("--message-file", str(message))

        self.assertEqual(status, 0)
        self.assertTrue(report["resumed"])
        self.assertEqual(self.claude_call()["argv"], [
            "-p", "Reviewer asked for a test.",
            "--output-format", "json",
            "--max-turns", "40",
            "--permission-mode", "bypassPermissions",
            "--resume", SESSION_ID,
            "--append-system-prompt", PUBLISH_RULE,
        ])

    def test_transcript_of_another_worktree_starts_a_fresh_session(self) -> None:
        self.write_transcript(self.add_worktree("abc-123-old-card"))

        self.run_session()

        argv = self.claude_call()["argv"]
        self.assertEqual(argv[argv.index("--session-id") + 1], SESSION_ID)
        self.assertNotIn("--resume", argv)

    def test_resume_without_a_message_says_continue(self) -> None:
        self.write_transcript()

        self.run_session()

        self.assertEqual(self.claude_call()["argv"][:2], ["-p", "Continue."])

    def test_worker_entry_template_is_rendered(self) -> None:
        self.repository["worker_entry"] = "Run /work-issue {issue_identifier} {issue_id} {issue_url} {branch} {repo}"
        self.settings = self.write_settings([self.repository])

        self.run_session()

        self.assertEqual(
            self.claude_call()["argv"][1],
            f"Run /work-issue ABC-123 {ISSUE_ID} https://linear.app/example/issue/ABC-123 abc-123-fix {REPO}",
        )

    def test_unexpected_error_is_reported_as_one_json_line(self) -> None:
        self.repository["worker_entry"] = "Run {unknown_field}"
        self.settings = self.write_settings([self.repository])

        status, report = self.run_session()

        self.assertEqual(status, 1)
        self.assertEqual(report, {"error": "KeyError: 'unknown_field'"})

    def test_refuses_while_the_worktree_lock_is_held(self) -> None:
        lock_path = Path(git(self.worktree, "rev-parse", "--absolute-git-dir")) / "claude-session.lock"
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status, report = self.run_session()

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": f"another claude-session holds {self.worktree}"})
        self.assertFalse(self.log.exists())

    def test_refuses_while_a_process_runs_the_session(self) -> None:
        sleeper = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)", "--resume", SESSION_ID])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)

        status, report = self.run_session()

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": f"a process for Claude session {SESSION_ID} is already running"})

    def test_refuses_without_a_kanban_workspace(self) -> None:
        status, report = self.run_session(env={"HERMES_KANBAN_WORKSPACE": ""})

        self.assertEqual(status, 2)
        self.assertEqual(
            report,
            {"error": "HERMES_KANBAN_WORKSPACE is not set; claude-session runs only inside a Kanban worker"},
        )

    def test_refuses_a_directory_that_is_not_a_worktree_root(self) -> None:
        subdir = self.worktree / "src"
        subdir.mkdir()

        status, report = self.run_session(workspace=subdir)

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": f"workspace {subdir} is not the root of a git worktree"})

    def test_refuses_a_worktree_of_another_repository(self) -> None:
        stranger = self.tmp / "stranger"
        stranger.mkdir()
        git(stranger, "init", "-q")

        status, report = self.run_session(workspace=stranger)

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": f"workspace {stranger} does not belong to the repository at {self.main}"})

    def test_refuses_the_main_checkout(self) -> None:
        status, report = self.run_session(workspace=self.main)

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": f"workspace {self.main} is the main checkout, not a Kanban worktree"})

    def test_nvmrc_prefix_selects_the_highest_installed_match(self) -> None:
        for version in ("v22.9.0", "v24.2.0", "v24.10.1", "v25.0.0"):
            (self.nvm_dir / "versions" / "node" / version / "bin").mkdir(parents=True)
        (self.worktree / ".nvmrc").write_text("24\n")

        self.run_session()

        path = self.claude_call()["env"]["PATH"].split(os.pathsep)
        self.assertEqual(path[0], str(self.nvm_dir / "versions" / "node" / "v24.10.1" / "bin"))

    def test_missing_nvmrc_leaves_path_unchanged(self) -> None:
        self.run_session(env={"PATH": "/usr/bin:/bin"})

        self.assertEqual(self.claude_call()["env"]["PATH"], "/usr/bin:/bin")

    def test_uninstalled_nvmrc_version_refuses(self) -> None:
        (self.worktree / ".nvmrc").write_text("v20\n")

        status, report = self.run_session()

        self.assertEqual(status, 2)
        self.assertEqual(
            report,
            {"error": f"Node 20 from .nvmrc is not installed under {self.nvm_dir}; run nvm install 20"},
        )

    def test_transport_settings_reach_only_the_claude_process(self) -> None:
        self.settings = self.write_settings([self.repository], {
            "base_url": "http://127.0.0.1:9999",
            "model": "example-model",
            "auth_token_env": "EXAMPLE_PROXY_TOKEN",
            "max_turns": 12,
            "permission_mode": "acceptEdits",
        })

        self.run_session(env={"EXAMPLE_PROXY_TOKEN": "token-value"})

        call = self.claude_call()
        settings_path = call["argv"][-1]
        self.assertEqual(call["argv"][2:], [
            "--output-format", "json",
            "--max-turns", "12",
            "--permission-mode", "acceptEdits",
            "--session-id", SESSION_ID,
            "--append-system-prompt", PUBLISH_RULE,
            "--model", "example-model",
            "--settings", settings_path,
        ])
        self.assertEqual(call["settings"], {
            "mode": "0o600",
            "content": {"env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999",
                "ANTHROPIC_AUTH_TOKEN": "token-value",
                "ANTHROPIC_MODEL": "example-model",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "example-model",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "example-model",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "example-model",
                "ANTHROPIC_DEFAULT_FABLE_MODEL": "example-model",
                "CLAUDE_CODE_SUBAGENT_MODEL": "example-model",
            }},
        })
        self.assertEqual([k for k in call["env"] if k.startswith("ANTHROPIC_")], [])
        self.assertFalse(Path(settings_path).exists())

    def test_inherited_anthropic_variables_never_reach_claude(self) -> None:
        self.run_session(env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": "inherited"})

        self.assertEqual([k for k in self.claude_call()["env"] if k.startswith("ANTHROPIC_")], [])

    def test_unset_transport_adds_no_anthropic_variables(self) -> None:
        self.run_session()

        call = self.claude_call()
        self.assertEqual([k for k in call["env"] if k.startswith("ANTHROPIC_")], [])
        self.assertNotIn("--settings", call["argv"])
        self.assertIsNone(call["settings"])

    def test_max_turns_run_is_reported_as_completed(self) -> None:
        status, report = self.run_session(env={"FAKE_CLAUDE_SUBTYPE": "error_max_turns"})

        self.assertEqual(status, 0)
        self.assertEqual(report["subtype"], "error_max_turns")

    def test_failed_claude_run_is_reported_as_an_error(self) -> None:
        status, report = self.run_session(env={"FAKE_CLAUDE_SUBTYPE": "error_during_execution"})

        self.assertEqual(status, 1)
        self.assertEqual(report["error"], "claude run ended with error_during_execution (exit 0)")
        self.assertEqual(report["session_id"], SESSION_ID)
