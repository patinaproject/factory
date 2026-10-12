from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

from tests.support import BOT_AUTHOR, BOT_LOGIN, SCRIPTS, GitFixture, git

# A fresh issue per test process keeps concurrent suites from seeing each other's fake claude in `ps`.
ISSUE_ID = str(uuid.uuid4())
SESSION_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, f"linear-issue:{ISSUE_ID}"))
SYSTEM_PROMPT = (
    f"Publish commits only by running {(SCRIPTS / 'push-signed').resolve()} from the worktree, never `git push`. "
    "It creates commits that the GitHub App signs, which repositories requiring verified signatures need. "
    "To bring in the base branch, run `gh pr update-branch`, never a local merge.\n\n"
    "Reach Linear only with the token in LINEAR_API_KEY, which the factory's Linear agent app issued for this "
    "run. Never mint a Linear token yourself and never run a secrets helper: a token request with another scope "
    "set revokes the factory's tokens.\n\n"
    "A finished check never runs again by itself, and the GitHub App cannot re-request a check. When a "
    "check failed on a head that is behind the base branch, or a person says the cause of a failure is "
    "fixed, run `gh pr update-branch <number>` so every check runs again on a new head, then wait for "
    "that run before you judge the ready gate.\n\n"
    "Stop every background command, monitor, and watcher you started before you write your final result. "
    "The run ends when you finish, and anything still running is killed. To wait for CI, run a foreground "
    "command that ends on its own, such as `gh pr checks <number> --watch --fail-fast` with the tool's "
    "timeout set, and run it again when it times out."
)
REPO = "example-org/example-repo"
LINEAR_MCP_CONFIG = (
    '{"mcpServers": {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp", '
    '"headers": {"Authorization": "Bearer ${LINEAR_API_KEY}"}}}}'
)
DEFAULT_ENTRY = (
    "Work Linear issue ABC-123 (https://linear.app/example/issue/ABC-123) in example-org/example-repo "
    "on branch abc-123-fix. First start the issue through this repository's own issue-start workflow, "
    "which moves it to In Progress. Open a ready (non-draft) pull request and drive it until it is ready to merge."
)
TAKEOVER_PR = "https://github.com/Example-Org/example-repo/pull/7"
# Everything else a worker process holds. None of it may reach Claude.
WORKER_SECRETS = {
    "LINEAR_WEBHOOK_SECRET": "x", "GITHUB_WEBHOOK_SECRET": "x", "FACTORY_INFISICAL_CLIENT_ID": "x",
    "FACTORY_INFISICAL_CLIENT_SECRET": "x", "SLACK_BOT_TOKEN": "x", "OP_SERVICE_ACCOUNT_TOKEN": "x",
    "EXAMPLE_PROXY_TOKEN": "token-value", "HERMES_KANBAN_TASK": "t_1", "GH_TOKEN": "x",
}
ALLOWED_NAMES = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TMPDIR", "TZ", "NVM_DIR",
    "GH_CONFIG_DIR", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE",
    "JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT", "DEVELOPER_DIR",
}
TOOLCHAIN = {
    "JAVA_HOME": "/opt/jdk", "ANDROID_HOME": "/opt/android-sdk", "ANDROID_SDK_ROOT": "/opt/android-sdk",
    "DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer",
}
TAKEOVER_INTRO = f"The issue already has an open pull request, {TAKEOVER_PR}, opened by someone else."
TAKEOVER_RULES = (
    f"Open your own ready pull request, and in its body credit the original author and link "
    f"{TAKEOVER_PR}. Do not close {TAKEOVER_PR}. The worker closes it after a human approves. If push-signed "
    "refuses one of its commits, such as a merge commit, squash the pull request onto the default branch before "
    "you publish: check out the default branch under this branch's name, run `git merge --squash` with the pull "
    "request's head (fetch it again with `git fetch origin refs/pull/<number>/head` and use FETCH_HEAD), resolve "
    "conflicts once, and commit. Credit the original author with a Co-authored-by trailer "
    "on the commits you publish."
)
TAKEOVER_ENTRY = f"{TAKEOVER_INTRO} This branch starts from its head. {TAKEOVER_RULES}"
FACTORY_BRANCH_TAKEOVER_ENTRY = (
    f"{TAKEOVER_INTRO} This branch holds the factory's own earlier commits, not that pull request's. {TAKEOVER_RULES}"
)
PERSON_BRANCH_REFUSAL = (
    f"origin/abc-123-fix was not created by {BOT_LOGIN}, so the factory cannot publish to it; a person must rename "
    "that branch or open a pull request from it, which triage hands over as takeover_pr"
)


class ClaudeSessionTest(GitFixture):
    def setUp(self) -> None:
        super().setUp()
        self.worktree = self.add_worktree("abc-123-fix")
        self.log = self.tmp / "claude-call.json"
        self.claude = self.make_fake_claude(self.log)
        self.claude_config = self.tmp / "claude-config"
        self.nvm_dir = self.tmp / "nvm"
        self.hermes_home = self.tmp / "hermes" / "profiles" / "claude-worker"
        self.install_linear_plugin(self.hermes_home)
        self.repository = {"full_name": REPO, "path": str(self.main), "routing": "default", "worker_entry": ""}
        self.settings = self.write_settings([self.repository], {"max_turns": 40})

    def session_command(self, *extra: str, workspace: Path | None = None, env: dict | None = None):
        base = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        base.update(
            HERMES_KANBAN_WORKSPACE=str(workspace or self.worktree),
            HERMES_HOME=str(self.hermes_home),
            CLAUDE_BIN=str(self.claude),
            CLAUDE_CONFIG_DIR=str(self.claude_config),
            NVM_DIR=str(self.nvm_dir),
            GH_CONFIG_DIR=str(self.tmp / "gh"),
            LINEAR_CLIENT_ID="id1",
            LINEAR_CLIENT_SECRET="secret1",
            **WORKER_SECRETS,
        )
        base.update(env or {})
        return [
            str(SCRIPTS / "claude-session"),
            "--issue-id", ISSUE_ID,
            "--issue-identifier", "ABC-123",
            "--issue-url", "https://linear.app/example/issue/ABC-123",
            "--repo", REPO,
            "--settings-json", str(self.settings),
            *extra,
        ], base

    def start_session(self) -> subprocess.Popen:
        command, env = self.session_command()
        return subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def run_session(self, *extra: str, workspace: Path | None = None, env: dict | None = None):
        command, env = self.session_command(*extra, workspace=workspace, env=env)
        proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        return proc.returncode, json.loads(lines[0])

    def claude_call(self) -> dict:
        return json.loads(self.log.read_text())

    def worktree_git_dir(self) -> Path:
        return Path(git(self.worktree, "rev-parse", "--absolute-git-dir"))

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
            "adopted": None,
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
            "--append-system-prompt", SYSTEM_PROMPT,
            "--mcp-config", LINEAR_MCP_CONFIG,
            "--settings", str(self.worktree_git_dir() / "claude-session.settings.json"),
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
            "--append-system-prompt", SYSTEM_PROMPT,
            "--mcp-config", LINEAR_MCP_CONFIG,
            "--settings", str(self.worktree_git_dir() / "claude-session.settings.json"),
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

    def test_worker_entry_prefixes_the_default_entry(self) -> None:
        self.repository["worker_entry"] = "/patina-mode"
        self.settings = self.write_settings([self.repository])

        self.run_session()

        self.assertEqual(self.claude_call()["argv"][1], f"/patina-mode {DEFAULT_ENTRY}")

    def test_worker_entry_with_fields_is_refused_before_claude_starts(self) -> None:
        entry = "/patina-mode Work Linear issue {issue_identifier} on branch {branch}."
        self.repository["worker_entry"] = entry
        self.settings = self.write_settings([self.repository])

        status, report = self.run_session()

        self.assertEqual(status, 2)
        self.assertEqual(report, {
            "error": f"worker_entry {entry!r} must be a plain prefix such as /patina-mode; "
            "claude-session adds the issue, URL, repository, and branch itself",
        })
        self.assertFalse(self.log.exists())

    def test_unexpected_error_is_reported_as_one_json_line(self) -> None:
        missing = self.main / "no-such-message.txt"

        status, report = self.run_session("--message-file", str(missing))

        self.assertEqual(status, 1)
        self.assertEqual(report, {"error": f"FileNotFoundError: [Errno 2] No such file or directory: '{missing}'"})

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
        })

        self.run_session()

        call = self.claude_call()
        settings_path = call["argv"][-1]
        self.assertEqual(call["argv"][2:], [
            "--output-format", "json",
            "--max-turns", "12",
            "--permission-mode", "bypassPermissions",
            "--session-id", SESSION_ID,
            "--append-system-prompt", SYSTEM_PROMPT,
            "--mcp-config", LINEAR_MCP_CONFIG,
            "--model", "example-model",
            "--settings", settings_path,
        ])
        self.assertEqual(call["settings"]["mode"], "0o600")
        self.assertEqual(call["settings"]["content"]["env"], {
                "LINEAR_API_KEY": "cached-token",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999",
                "ANTHROPIC_AUTH_TOKEN": "token-value",
                "ANTHROPIC_MODEL": "example-model",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "example-model",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "example-model",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "example-model",
                "ANTHROPIC_DEFAULT_FABLE_MODEL": "example-model",
                "CLAUDE_CODE_SUBAGENT_MODEL": "example-model",
        })
        self.assertEqual([k for k in call["env"] if k.startswith("ANTHROPIC_")], [])
        self.assertNotIn("EXAMPLE_PROXY_TOKEN", call["env"])
        self.assertFalse(Path(settings_path).exists())

    def test_claude_gets_only_the_allowlisted_environment(self) -> None:
        self.run_session(env={"LC_ALL": "C", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1", "SOME_API_KEY": "x"})

        env = self.claude_call()["env"]
        self.assertEqual(set(env) & set(WORKER_SECRETS), set())
        # macOS adds __CF_USER_TEXT_ENCODING to every process it starts.
        extra = {name for name in env if name not in ALLOWED_NAMES and not name.startswith(("LC_", "__CF_"))}
        self.assertEqual(extra, set())
        self.assertNotIn("LINEAR_CLIENT_ID", env)
        self.assertNotIn("LINEAR_CLIENT_SECRET", env)
        self.assertNotIn("HERMES_HOME", env)
        self.assertEqual(env["GH_CONFIG_DIR"], str(self.tmp / "gh"))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.claude_config))
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"], "1")
        self.assertEqual(env["HOME"], os.environ["HOME"])
        self.assertIn("PATH", env)

    def test_claude_gets_the_mobile_toolchain_locations(self) -> None:
        self.run_session(env=TOOLCHAIN)

        env = self.claude_call()["env"]
        self.assertEqual({name: env.get(name) for name in TOOLCHAIN}, TOOLCHAIN)
        self.assertEqual(set(env) & set(WORKER_SECRETS), set())

    def test_toolchain_checks_run_in_the_worktree_with_the_environment_claude_gets(self) -> None:
        marker = self.tmp / "checked"
        self.repository["toolchain_checks"] = [
            'test "$ANDROID_HOME" = /opt/android-sdk',
            'test -z "$LINEAR_CLIENT_SECRET$GITHUB_WEBHOOK_SECRET"',
            f'pwd > {marker}',
        ]
        self.settings = self.write_settings([self.repository])

        status, report = self.run_session(env=TOOLCHAIN)

        self.assertEqual((status, report.get("error")), (0, None))
        self.assertEqual(marker.read_text().strip(), str(self.worktree.resolve()))
        self.assertTrue(self.log.exists())

    def test_a_failing_toolchain_check_refuses_the_run_before_claude_starts(self) -> None:
        self.repository["toolchain_checks"] = ["true", 'echo "no JAVA_HOME" >&2; exit 3']
        self.settings = self.write_settings([self.repository])

        status, report = self.run_session()

        self.assertEqual(status, 2)
        self.assertEqual(report, {
            "error": "toolchain check 'echo \"no JAVA_HOME\" >&2; exit 3' failed with exit 3 "
            "in the environment Claude Code gets: no JAVA_HOME",
        })
        self.assertFalse(self.log.exists())

    def test_toolchain_checks_must_be_a_list_of_commands(self) -> None:
        self.repository["toolchain_checks"] = "xcrun simctl help"
        self.settings = self.write_settings([self.repository])

        status, report = self.run_session()

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": "toolchain_checks 'xcrun simctl help' must be a list of shell commands"})
        self.assertFalse(self.log.exists())

    def test_linear_token_comes_from_the_plugin_cache_in_the_settings_file(self) -> None:
        self.run_session()

        call = self.claude_call()
        self.assertEqual(call["settings"]["mode"], "0o600")
        self.assertEqual(call["settings"]["content"]["env"], {"LINEAR_API_KEY": "cached-token"})
        self.assertNotIn("LINEAR_API_KEY", call["env"])
        self.assertFalse(Path(call["argv"][-1]).exists())
        self.assertEqual([r for r in self.linear_requests(self.hermes_home) if "form" in r], [])

    def test_the_linear_mcp_server_sends_the_run_token_without_putting_it_in_argv(self) -> None:
        self.run_session()

        call = self.claude_call()
        argv = call["argv"]
        server = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["linear"]
        env = call["settings"]["content"]["env"]
        # Claude Code expands ${NAME} in MCP headers from its environment, which the settings env joins.
        header = re.sub(r"\$\{(\w+)\}", lambda m: env[m.group(1)], server["headers"]["Authorization"])
        self.assertEqual((server["type"], server["url"]), ("http", "https://mcp.linear.app/mcp"))
        self.assertEqual(header, "Bearer cached-token")
        self.assertFalse(any("cached-token" in arg for arg in argv))

    def test_a_revoked_cached_linear_token_is_replaced_with_the_plugin_scope(self) -> None:
        (self.hermes_home / "linear-fake.json").write_text(json.dumps({"accepted": []}))

        self.run_session()

        self.assertEqual(self.claude_call()["settings"]["content"]["env"], {"LINEAR_API_KEY": "minted-1"})
        cache = self.hermes_home / "plugin-data" / "linear-agent-session" / "token.json"
        self.assertEqual(json.loads(cache.read_text())["access_token"], "minted-1")
        forms = [r["form"] for r in self.linear_requests(self.hermes_home) if "form" in r]
        self.assertEqual(forms, [{"grant_type": "client_credentials", "scope": "read,write,app:assignable,app:mentionable"}])

    def test_a_linear_that_rejects_every_token_refuses_before_claude_starts(self) -> None:
        (self.hermes_home / "linear-fake.json").write_text(json.dumps({"accepted": [], "reject_all": True}))

        status, report = self.run_session()

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": "Linear token: Linear rejected a newly issued token"})
        self.assertFalse(self.log.exists())

    def settings_path(self) -> Path:
        return self.worktree_git_dir() / "claude-session.settings.json"

    def test_claude_code_removes_the_settings_file_when_its_session_starts(self) -> None:
        self.run_session()

        settings = self.claude_call()["settings"]
        self.assertEqual(settings["content"]["env"], {"LINEAR_API_KEY": "cached-token"})
        self.assertFalse(settings["exists_after_start"])

    def test_signal_mid_run_removes_the_settings_file_and_ends_claude(self) -> None:
        # Without the start hook, as when hooks are disabled, the file stays until claude-session removes it.
        self.configure_fake_claude(skip_start_hooks=True, hold_seconds=600)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=signum.name):
                self.log.unlink(missing_ok=True)
                session = self.start_session()
                while not self.log.exists():
                    if session.poll() is not None:
                        self.fail(session.communicate())
                    time.sleep(0.05)
                self.assertTrue(self.settings_path().exists())

                session.send_signal(signum)

                session.communicate(timeout=30)
                self.assertEqual(session.returncode, -signum)
                self.assertFalse(self.settings_path().exists())
                self.assert_process_gone(self.claude_call()["pid"])

    def test_next_run_replaces_a_settings_file_left_by_a_killed_run(self) -> None:
        self.settings_path().write_text('{"env": {"LINEAR_API_KEY": "stale-token"}}')
        os.chmod(self.settings_path(), 0o644)
        self.configure_fake_claude(skip_start_hooks=True)

        status, _ = self.run_session()

        settings = self.claude_call()["settings"]
        self.assertEqual(status, 0)
        self.assertEqual(settings["mode"], "0o600")
        self.assertEqual(settings["content"]["env"], {"LINEAR_API_KEY": "cached-token"})
        self.assertFalse(self.settings_path().exists())

    def test_refused_run_still_removes_a_stale_settings_file(self) -> None:
        self.settings_path().write_text('{"env": {"LINEAR_API_KEY": "stale-token"}}')

        status, _ = self.run_session(env={"LINEAR_CLIENT_SECRET": ""})

        self.assertEqual(status, 2)
        self.assertFalse(self.settings_path().exists())

    def test_missing_linear_credentials_refuse_before_claude_starts(self) -> None:
        status, report = self.run_session(env={"LINEAR_CLIENT_SECRET": ""})

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": "Linear token: set LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET"})
        self.assertFalse(self.log.exists())

    def test_missing_plugin_refuses_before_claude_starts(self) -> None:
        empty = self.tmp / "empty-home"
        empty.mkdir()
        status, report = self.run_session(env={"HERMES_HOME": str(empty)})

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": f"the linear-agent-session plugin is not installed in {empty}"})
        self.assertFalse(self.log.exists())

    def test_inherited_anthropic_variables_never_reach_claude(self) -> None:
        self.run_session(env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": "inherited"})

        self.assertEqual([k for k in self.claude_call()["env"] if k.startswith("ANTHROPIC_")], [])

    def test_unset_transport_adds_no_anthropic_variables(self) -> None:
        self.run_session()

        call = self.claude_call()
        self.assertEqual([k for k in call["env"] if k.startswith("ANTHROPIC_")], [])
        self.assertEqual(call["settings"]["content"]["env"], {"LINEAR_API_KEY": "cached-token"})

    def test_without_max_turns_the_run_has_no_turn_limit(self) -> None:
        for claude_session in ({}, {"max_turns": None}, {"max_turns": 0}):
            with self.subTest(claude_session=claude_session):
                self.settings = self.write_settings([self.repository], claude_session)

                status, _ = self.run_session()

                self.assertEqual(status, 0)
                self.assertNotIn("--max-turns", self.claude_call()["argv"])

    def test_invalid_max_turns_is_refused(self) -> None:
        for value in (-1, "40", True):
            with self.subTest(max_turns=value):
                self.settings = self.write_settings([self.repository], {"max_turns": value})

                status, report = self.run_session()

                self.assertEqual(status, 2)
                self.assertEqual(report, {
                    "error": f"claude_session.max_turns {value!r} must be a positive integer, or 0 for no limit",
                })

    def test_max_turns_run_is_reported_as_completed(self) -> None:
        self.configure_fake_claude(subtype="error_max_turns")
        status, report = self.run_session()

        self.assertEqual(status, 0)
        self.assertEqual(report["subtype"], "error_max_turns")

    def test_failed_claude_run_is_reported_as_an_error(self) -> None:
        self.configure_fake_claude(subtype="error_during_execution")
        status, report = self.run_session()

        self.assertEqual(status, 1)
        self.assertEqual(report["error"], "claude run ended with error_during_execution (exit 0)")
        self.assertEqual(report["session_id"], SESSION_ID)

    def leftover_pid(self) -> int:
        return int((self.tmp / "leftover.pid").read_text())

    def assert_process_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        self.fail(f"process {pid} from the run was still alive")

    def test_run_ends_when_claude_exits_but_a_child_holds_its_output(self) -> None:
        started = time.monotonic()
        self.configure_fake_claude(leftover_pid_file=str(self.tmp / "leftover.pid"))
        status, report = self.run_session()

        self.assertEqual(status, 0)
        self.assertEqual(report["result"], "Opened the pull request.")
        self.assertLess(time.monotonic() - started, 60)
        self.assert_process_gone(self.leftover_pid())

    def test_run_ends_when_claude_waits_for_a_detached_background_command(self) -> None:
        started = time.monotonic()
        self.configure_fake_claude(
            leftover_pid_file=str(self.tmp / "leftover.pid"), leftover_detached=True, waits_for_leftover=True
        )
        status, report = self.run_session()

        self.assertEqual(status, 0)
        self.assertEqual(report["result"], "Opened the pull request.")
        self.assertLess(time.monotonic() - started, 60)
        self.assert_process_gone(self.leftover_pid())
        listing = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True, check=True).stdout
        self.assertNotIn(SESSION_ID, listing)

    def head(self) -> str:
        return git(self.worktree, "rev-parse", "HEAD")

    def assert_refused_before_claude(self, status: int, report: dict, error: str, start: str) -> None:
        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": error})
        self.assertEqual(self.head(), start)
        self.assertFalse(self.log.exists())

    def test_unstarted_branch_adopts_the_factory_remote_branch(self) -> None:
        remote = self.push_upstream_commit("remote", "refs/heads/abc-123-fix", author=BOT_AUTHOR)

        status, report = self.run_session()

        self.assertEqual(status, 0)
        self.assertEqual(report["adopted"], {"source": "remote_branch", "ref": "abc-123-fix", "head": remote})
        self.assertEqual(self.head(), remote)
        self.assertEqual(self.claude_call()["argv"][1], DEFAULT_ENTRY)

    def test_person_remote_branch_is_refused(self) -> None:
        self.push_upstream_commit("remote", "refs/heads/abc-123-fix")
        start = self.head()

        status, report = self.run_session()

        self.assert_refused_before_claude(status, report, PERSON_BRANCH_REFUSAL, start)

    def test_person_remote_branch_is_refused_even_with_a_takeover_pull_request(self) -> None:
        self.push_upstream_commit("remote", "refs/heads/abc-123-fix")
        self.push_upstream_commit("pull", "refs/pull/7/head")
        start = self.head()

        status, report = self.run_session("--takeover-pr", TAKEOVER_PR)

        self.assert_refused_before_claude(status, report, PERSON_BRANCH_REFUSAL, start)

    def test_remote_branch_at_the_default_branch_is_adopted(self) -> None:
        start = self.head()
        git(self.main, "push", "-q", "origin", "HEAD:refs/heads/abc-123-fix")

        status, report = self.run_session()

        self.assertEqual(status, 0)
        self.assertEqual(report["adopted"], {"source": "remote_branch", "ref": "abc-123-fix", "head": start})
        self.assertEqual(self.head(), start)

    def test_branch_the_factory_created_is_adopted_after_a_person_adds_to_it(self) -> None:
        self.push_upstream_commit("factory", "refs/heads/abc-123-fix", author=BOT_AUTHOR)
        remote = self.push_upstream_commit("person", "refs/heads/abc-123-fix", base="refs/heads/abc-123-fix")

        status, report = self.run_session()

        self.assertEqual(status, 0)
        self.assertEqual(report["adopted"], {"source": "remote_branch", "ref": "abc-123-fix", "head": remote})
        self.assertEqual(self.head(), remote)

    def test_remote_branch_without_a_github_login_is_refused(self) -> None:
        self.push_upstream_commit("remote", "refs/heads/abc-123-fix", author=BOT_AUTHOR)
        self.settings = self.write_settings([self.repository], login=None)
        start = self.head()

        status, report = self.run_session()

        self.assert_refused_before_claude(status, report, (
            "github.login is not set in the settings, so claude-session cannot tell the factory's branches "
            "from a person's"
        ), start)

    def test_missing_origin_head_is_refused(self) -> None:
        git(self.main, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        start = self.head()

        status, report = self.run_session()

        self.assert_refused_before_claude(status, report, (
            f"origin/HEAD is not set in {self.worktree}; run git remote set-head origin -a in the checkout"
        ), start)

    def test_unstarted_branch_adopts_the_takeover_pull_request(self) -> None:
        pull = self.push_upstream_commit("pull", "refs/pull/7/head")

        status, report = self.run_session("--takeover-pr", TAKEOVER_PR)

        self.assertEqual(status, 0)
        self.assertEqual(report["adopted"], {"source": "takeover_pr", "ref": TAKEOVER_PR, "head": pull})
        self.assertEqual(self.head(), pull)
        self.assertEqual(self.claude_call()["argv"][1], f"{DEFAULT_ENTRY}\n\n{TAKEOVER_ENTRY}")

    def test_factory_remote_branch_wins_over_the_takeover_pull_request(self) -> None:
        remote = self.push_upstream_commit("remote", "refs/heads/abc-123-fix", author=BOT_AUTHOR)
        self.push_upstream_commit("pull", "refs/pull/7/head")

        _, report = self.run_session("--takeover-pr", TAKEOVER_PR)

        self.assertEqual(report["adopted"], {"source": "remote_branch", "ref": "abc-123-fix", "head": remote})
        self.assertEqual(self.head(), remote)
        self.assertEqual(self.claude_call()["argv"][1], f"{DEFAULT_ENTRY}\n\n{FACTORY_BRANCH_TAKEOVER_ENTRY}")

    def test_adoption_runs_once(self) -> None:
        pull = self.push_upstream_commit("pull", "refs/pull/7/head")
        self.run_session("--takeover-pr", TAKEOVER_PR)
        self.push_upstream_commit("pull-again", "refs/pull/7/head")

        _, report = self.run_session("--takeover-pr", TAKEOVER_PR)

        self.assertIsNone(report["adopted"])
        self.assertEqual(self.head(), pull)

    def test_started_branch_is_left_alone(self) -> None:
        self.push_upstream_commit("remote", "refs/heads/abc-123-fix")
        (self.worktree / "local.txt").write_text("local")
        git(self.worktree, "add", "local.txt")
        git(self.worktree, "commit", "-q", "-m", "local")
        local = self.head()

        _, report = self.run_session()

        self.assertIsNone(report["adopted"])
        self.assertEqual(self.head(), local)

    def test_dirty_worktree_is_left_alone(self) -> None:
        self.push_upstream_commit("remote", "refs/heads/abc-123-fix")
        start = self.head()
        (self.worktree / "draft.txt").write_text("draft")

        _, report = self.run_session()

        self.assertIsNone(report["adopted"])
        self.assertEqual(self.head(), start)
        self.assertTrue((self.worktree / "draft.txt").exists())

    def test_takeover_pull_request_of_another_repository_is_refused(self) -> None:
        url = "https://github.com/someone-else/example-repo/pull/7"

        status, report = self.run_session("--takeover-pr", url)

        self.assertEqual(status, 2)
        self.assertEqual(report, {"error": f"--takeover-pr {url} is not a pull request URL in {REPO}"})
        self.assertFalse(self.log.exists())

    def test_missing_takeover_pull_ref_is_refused(self) -> None:
        status, report = self.run_session("--takeover-pr", TAKEOVER_PR)

        self.assertEqual(status, 2)
        self.assertIn("git fetch --quiet origin refs/pull/7/head failed", report["error"])
        self.assertFalse(self.log.exists())

    def test_resumed_session_gets_no_takeover_paragraph(self) -> None:
        self.push_upstream_commit("pull", "refs/pull/7/head")
        self.write_transcript()

        self.run_session("--takeover-pr", TAKEOVER_PR)

        self.assertEqual(self.claude_call()["argv"][1], "Continue.")
