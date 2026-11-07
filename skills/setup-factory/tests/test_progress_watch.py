from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import plistlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "progress-watch"
REPO = SCRIPT.parents[3]
loader = importlib.machinery.SourceFileLoader("progress_watch", str(SCRIPT))
progress_watch = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
sys.modules[loader.name] = progress_watch
loader.exec_module(progress_watch)

NOW = 1_000_000.0
PING = "https://monitor.invalid/ping/private-ping-id"
FAIL = "https://monitor.invalid/fail/private-fail-id"
GATEWAY_BEAT = "https://monitor.invalid/gateway/private-gateway-id"
IDLE = "claude made no progress for 2700 s (idle limit 2700 s); last command: none"
LOAD = progress_watch.Context(2.5, 1.75, 8)


def stamp(moment: float) -> str:
    return progress_watch.stamp(moment)


class ProgressWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "hermes"
        self.worker = self.root / "profiles" / "claude-worker"
        self.config_dir = self.tmp / "claude"
        self.worker.mkdir(parents=True)
        (self.worker / "scripts").symlink_to(REPO / "profiles" / "claude-worker" / "scripts")
        self.beats = f"{PING}\n{FAIL}\n"
        (self.root / "gateway-heartbeat-url").write_text(f"{GATEWAY_BEAT}\n")
        self.write_beats(self.beats)
        self.settings = {"claude_session": {}}
        self.sent: list = []
        self.send_failure = None
        self.out = io.StringIO()
        self.default = self.board(self.root / "kanban.db")
        self.addCleanup(lambda: self.assertNotIn(GATEWAY_BEAT, [url for url, _ in self.sent]))

    def board(self, path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, created_at INTEGER, started_at INTEGER, "
            "workspace_path TEXT, last_failure_error TEXT, assignee TEXT)"
        )
        db.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)")
        db.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, ended_at INTEGER, error TEXT, summary TEXT)")
        return db

    def write_beats(self, text: str) -> None:
        path = self.root / "progress-heartbeat-url"
        path.write_text(text)
        path.chmod(0o600)

    def card(self, id: str, status: str = "ready", age: float = 90, *, workspace: str | None = None, error: str | None = None,
             guard: str | None = None, db: sqlite3.Connection | None = None, assignee: str | None = "claude-worker") -> None:
        db = db or self.default
        db.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, NULL, ?, ?, ?)", (id, status, NOW - age * 60, workspace, error, assignee)
        )
        if guard:
            db.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'respawn_guarded', ?, ?)",
                (id, json.dumps({"reason": guard}), NOW - 60),
            )
        db.commit()

    def run_ended(self, task: str, outcome: str, age: float, error: str | None = None, summary: str | None = None) -> None:
        self.default.execute(
            "INSERT INTO task_runs (task_id, outcome, ended_at, error, summary) VALUES (?, ?, ?, ?, ?)",
            (task, outcome, NOW - age * 60, error, summary),
        )
        self.default.commit()

    def hold(self, **entries: dict) -> None:
        (self.root / "dispatch-holds.json").write_text(json.dumps({"holds": entries, "applied": True}))

    def watch(self, now: float = NOW):
        self.out = io.StringIO()
        return progress_watch.watch(
            self.root, self.worker, self.config_dir, settings_reader=lambda: self.settings, now=lambda: now,
            load=lambda: LOAD, send=self.send, out=self.out,
        )

    def send(self, url: str, body: bytes | None) -> str | None:
        self.sent.append((url, body.decode() if body is not None else None))
        return self.send_failure

    def state(self) -> dict:
        return json.loads((self.root / "progress-watch.json").read_text())

    def kinds(self) -> list:
        return [reason["kind"] for reason in self.state()["reasons"]]

    def test_a_ready_card_held_as_blocker_auth_withholds_the_ping_and_the_state_lists_its_error(self) -> None:
        self.card("t_auth", guard="blocker_auth", error="quota exhausted: resets at 5pm")
        self.watch()
        self.assertEqual(self.sent, [(FAIL, "default/t_auth is held as blocker_auth: quota exhausted: resets at 5pm")])
        self.assertEqual(self.state()["reasons"], [
            {"kind": "blocker_auth", "text": "default/t_auth is held as blocker_auth: quota exhausted: resets at 5pm"},
        ])
        self.assertIs(self.state()["pinged"], False)

    def test_waiting_cards_that_nothing_holds_withhold_the_ping_and_the_state_records_the_load(self) -> None:
        self.card("t_wait")
        self.watch()
        self.assertEqual([url for url, _ in self.sent], [FAIL])
        self.assertEqual(self.kinds(), ["unexplained"])
        self.assertEqual(self.state()["context"], {"load1": 2.5, "load5": 1.75, "cores": 8})
        self.assertIs(self.state()["stalled"], True)

    def test_a_hold_entry_that_ends_later_covers_the_stall_even_when_dispatch_profiles_is_set(self) -> None:
        self.card("t_wait")
        (self.root / "config.yaml").write_text("kanban:\n  dispatch_profiles: []\n")
        until = stamp(NOW + 600)
        self.hold(host_load={"reason": "load 9.1 over 12", "until": until})
        self.watch()
        self.assertEqual(self.sent, [(PING, None)])
        self.assertEqual(self.state()["reasons"], [
            {"kind": "hold_entry", "text": f"host_load holds dispatch until {until}: load 9.1 over 12"},
        ])
        self.assertIs(self.state()["pinged"], True)

    def test_a_hold_entry_that_ended_or_has_no_end_time_withholds_the_ping_and_says_so(self) -> None:
        self.card("t_wait")
        until = stamp(NOW)
        self.hold(host_load={"reason": "load", "until": until})
        self.watch()
        self.assertEqual([url for url, _ in self.sent], [FAIL])
        self.assertEqual(self.state()["reasons"], [
            {"kind": "hold_expired", "text": f"host_load hold expired at {until} and is still listed: load"},
        ])
        self.hold(host_load={"reason": "load"})
        self.watch()
        self.assertEqual([url for url, _ in self.sent], [FAIL, FAIL])
        self.assertEqual(self.kinds(), ["hold_unreadable"])
        self.assertIn("without a valid until", self.state()["reasons"][0]["text"])

    def test_an_estop_in_the_worker_home_withholds_the_ping_and_names_the_pause(self) -> None:
        self.card("t_wait")
        (self.worker / "ESTOP").write_text(json.dumps({"reason": "operator maintenance"}))
        self.watch()
        self.assertEqual([url for url, _ in self.sent], [FAIL])
        self.assertEqual(self.state()["reasons"][0], {
            "kind": "paused", "text": f"hermes pause is engaged by {self.worker / 'ESTOP'}: operator maintenance",
        })

    def test_every_card_held_as_blocker_auth_or_active_pr_is_listed_when_the_hold_list_is_empty(self) -> None:
        self.card("t_a", guard="blocker_auth", error="401 unauthorized")
        self.card("t_b", guard="active_pr")
        self.card("t_c", guard="active_pr")
        self.watch()
        self.assertEqual(self.state()["reasons"], [
            {"kind": "blocker_auth", "text": "default/t_a is held as blocker_auth: 401 unauthorized"},
            {"kind": "active_pr", "text": "default/t_b is held as active_pr"},
            {"kind": "active_pr", "text": "default/t_c is held as active_pr"},
        ])
        self.assertEqual([url for url, _ in self.sent], [FAIL])

    def test_an_idle_limit_run_and_a_crashed_run_in_the_window_are_not_progress(self) -> None:
        self.card("t_wait")
        self.card("t_blocked")
        self.run_ended("t_blocked", "blocked", 15, summary=IDLE)
        self.run_ended("t_wait", "crashed", 10, IDLE)
        self.run_ended("t_other", "crashed", 20, "claude exited 1 without JSON output")
        self.watch()
        self.assertEqual(self.state()["stalled"], True)
        self.assertEqual(self.state()["reasons"], [
            {"kind": "idle_limit", "text": f"default/t_blocked ended a run on the idle limit: {IDLE}"},
            {"kind": "idle_limit", "text": f"default/t_wait ended a run on the idle limit: {IDLE}"},
        ])
        self.assertEqual([url for url, _ in self.sent], [FAIL])

    def test_cards_outside_ready_and_running_do_not_stall_and_record_no_reason(self) -> None:
        for status in ("review", "scheduled", "blocked", "done", "triage"):
            self.card(f"t_{status}", status, 500)
        self.watch()
        self.assertEqual(self.sent, [(PING, None)])
        self.assertEqual(self.state()["reasons"], [])
        self.assertIs(self.state()["stalled"], False)

    def test_a_card_that_became_ready_inside_the_window_does_not_stall(self) -> None:
        self.card("t_new", age=59)
        self.watch()
        self.assertEqual(self.sent, [(PING, None)])
        self.assertEqual(self.state()["reasons"], [])

    def test_a_run_that_completes_ends_the_stall_on_the_next_tick(self) -> None:
        self.card("t_wait")
        self.watch()
        self.assertEqual([url for url, _ in self.sent], [FAIL])
        self.run_ended("t_done", "completed", 1)
        self.watch(NOW + 300)
        self.assertEqual([url for url, _ in self.sent], [FAIL, PING])
        self.assertEqual(self.state()["reasons"], [])

    def test_a_card_that_moved_to_review_inside_the_window_is_progress(self) -> None:
        self.card("t_wait")
        self.default.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_other', 'review_requested', NULL, ?)",
            (NOW - 600,),
        )
        self.default.commit()
        self.watch()
        self.assertEqual([url for url, _ in self.sent], [PING])

    def transcript(self, workspace: str, age: float, *, folder: str | None = None, cwd: str | None = None) -> None:
        directory = self.config_dir / "projects" / (folder or progress_watch.encode_path(workspace))
        (directory / "session-1" / "subagents").mkdir(parents=True)
        path = directory / "session-1" / "subagents" / "agent.jsonl"
        main = directory / "session-1.jsonl"
        main.write_text(json.dumps({"cwd": cwd or workspace}) + "\n")
        path.write_text("{}\n")
        os.utime(main, (NOW - 3 * 3600, NOW - 3 * 3600))
        os.utime(path, (NOW - age * 60, NOW - age * 60))

    def test_a_running_cards_transcript_written_inside_the_window_is_progress(self) -> None:
        workspace = "/work/tree.1/t_run"
        self.card("t_run", "running", 300, workspace=workspace)
        self.transcript(workspace, 5)
        self.watch()
        self.assertEqual(self.sent, [(PING, None)])

    def test_a_transcript_older_than_the_window_is_not_progress(self) -> None:
        workspace = "/work/tree.1/t_run"
        self.card("t_run", "running", 300, workspace=workspace)
        self.transcript(workspace, 120)
        self.watch()
        self.assertEqual([url for url, _ in self.sent], [FAIL])

    def test_a_transcript_in_a_truncated_folder_is_found_by_its_recorded_directory(self) -> None:
        workspace = "/work/tree.1/t_run"
        self.card("t_run", "running", 300, workspace=workspace)
        self.transcript(workspace, 5, folder=progress_watch.encode_path(workspace) + "-abc123")
        self.watch()
        self.assertEqual(self.sent, [(PING, None)])

    def test_only_the_transcripts_that_record_the_cards_directory_count_in_a_shared_folder(self) -> None:
        workspace = "/work/tree.1/t_run"
        self.card("t_run", "running", 300, workspace=workspace)
        folder = progress_watch.encode_path(workspace) + "-abc123"
        self.transcript(workspace, 120, folder=folder)
        other = self.config_dir / "projects" / folder / "other.jsonl"
        other.write_text(json.dumps({"cwd": "/somewhere/else"}) + "\n")
        os.utime(other, (NOW - 60, NOW - 60))
        self.watch()
        self.assertEqual([url for url, _ in self.sent], [FAIL])

    def test_a_transcript_that_is_not_utf8_is_skipped_without_a_crash(self) -> None:
        workspace = "/work/tree.1/t_run"
        self.card("t_run", "running", 300, workspace=workspace)
        folder = self.config_dir / "projects" / (progress_watch.encode_path(workspace) + "-abc123")
        folder.mkdir(parents=True)
        (folder / "broken.jsonl").write_bytes(b"\xff\xfe{}\n")
        self.watch()
        self.assertEqual(self.kinds(), ["unexplained"])

    def test_an_unassigned_ready_card_is_not_waiting(self) -> None:
        self.card("t_parked", assignee=None)
        self.card("t_blank", assignee=" ")
        self.watch()
        self.assertEqual(self.sent, [(PING, None)])
        self.assertEqual(self.state()["reasons"], [])

    def test_a_plain_http_heartbeat_url_is_never_requested(self) -> None:
        self.write_beats("http://monitor.invalid/ping/private-ping-id\n")
        self.watch()
        self.assertEqual(self.sent, [])
        self.assertIn("holds no URL", self.out.getvalue())

    def test_a_dispatch_profiles_list_that_names_a_profile_is_not_a_hold(self) -> None:
        self.card("t_wait")
        (self.root / "config.yaml").write_text("other:\n  dispatch_profiles: []\nkanban:\n  dispatch_profiles:\n  - claude-worker\n")
        self.watch()
        self.assertEqual(self.kinds(), ["unexplained"])
        (self.root / "config.yaml").write_text("kanban:\n  max_spawn: 2\n  dispatch_profiles: []  # held\n")
        self.watch()
        self.assertEqual(self.kinds(), ["dispatch_profiles"])

    def test_a_folder_that_only_shares_the_prefix_is_not_the_cards_transcript(self) -> None:
        workspace = "/work/tree.1/t_run"
        self.card("t_run", "running", 300, workspace=workspace)
        self.transcript(workspace + "0", 5)
        self.watch()
        self.assertEqual([url for url, _ in self.sent], [FAIL])

    def test_a_stalled_tick_posts_the_redacted_reasons_to_the_failure_url_and_never_pings(self) -> None:
        token = "ghs_" + "a1B2" * 8
        self.card("t_auth", guard="blocker_auth", error=f"401 for {token}")
        self.watch()
        self.assertEqual(self.sent, [(FAIL, "default/t_auth is held as blocker_auth: 401 for [redacted]")])
        for text in (self.out.getvalue(), (self.root / "progress-watch.json").read_text()):
            self.assertNotIn("private-ping-id", text)
            self.assertNotIn("private-fail-id", text)
            self.assertNotIn(token, text)

    def test_a_stalled_tick_with_one_line_in_the_heartbeat_file_sends_no_request(self) -> None:
        self.write_beats(f"{PING}\n")
        self.card("t_wait")
        self.watch()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.kinds(), ["unexplained"])

    def test_each_reason_sent_to_the_failure_url_is_clipped_to_the_run_error_limit(self) -> None:
        self.card("t_long", guard="blocker_auth", error="quota " * 100)
        self.watch()
        self.assertEqual(len(self.sent[0][1]), 200)
        self.assertTrue(self.sent[0][1].endswith("..."))

    def test_the_gateway_heartbeat_is_never_requested(self) -> None:
        self.card("t_wait")
        self.watch()
        self.run_ended("t_done", "completed", 1)
        self.watch(NOW + 300)
        self.assertNotIn(GATEWAY_BEAT, [url for url, _ in self.sent])
        self.assertEqual(len(self.sent), 2)

    def test_the_script_names_no_secret_helper(self) -> None:
        source = SCRIPT.read_text()
        for name in ("factory-secrets", "worker-secrets", "proxy-token", "gateway-heartbeat-url"):
            self.assertNotIn(name, source)

    def test_a_board_that_cannot_be_read_is_a_stall_reason_and_not_a_crash(self) -> None:
        self.card("t_ok", "review")
        broken = self.root / "kanban" / "boards" / "triage" / "kanban.db"
        broken.parent.mkdir(parents=True)
        broken.write_text("not a database")
        self.watch()
        self.assertEqual(self.kinds(), ["board_unreadable"])
        self.assertEqual(self.state()["reasons"][0]["text"], f"triage board {broken} cannot be read: file is not a database")
        self.assertEqual([url for url, _ in self.sent], [FAIL])

    def test_a_missing_board_is_skipped(self) -> None:
        (self.root / "kanban.db").unlink()
        self.watch()
        self.assertEqual(self.sent, [(PING, None)])

    def test_a_threshold_that_is_not_a_positive_integer_withholds_the_ping(self) -> None:
        self.settings = {"claude_session": {"progress_stall_minutes": 0}}
        self.watch()
        self.assertEqual([url for url, _ in self.sent], [FAIL])
        self.assertEqual(self.state()["reasons"], [{
            "kind": "settings",
            "text": "progress_stall_minutes cannot be read: claude_session.progress_stall_minutes 0 must be a positive integer",
        }])

    def test_a_configured_threshold_moves_the_window(self) -> None:
        self.settings = {"claude_session": {"progress_stall_minutes": 120}}
        self.card("t_wait", age=90)
        self.watch()
        self.assertEqual(self.sent, [(PING, None)])

    def test_the_plist_names_the_job_and_runs_it_every_five_minutes(self) -> None:
        job = plistlib.loads(progress_watch.plist(self.root, self.worker, self.config_dir))
        self.assertEqual(job["Label"], "ai.patinaproject.factory.progress-watch")
        self.assertEqual(job["StartInterval"], 300)
        self.assertEqual(job["ProgramArguments"], [
            "/usr/bin/python3", str(self.root / "scripts" / "progress-watch"), "--root", str(self.root),
            "--worker-home", str(self.worker), "--claude-config-dir", str(self.config_dir),
        ])

    def test_print_plist_prints_the_job(self) -> None:
        stdout = io.StringIO()
        stdout.buffer = io.BytesIO()
        original, sys.stdout = sys.stdout, stdout
        try:
            progress_watch.main(["--root", str(self.root), "--print-plist"])
        finally:
            sys.stdout = original
        job = plistlib.loads(stdout.buffer.getvalue())
        self.assertEqual((job["Label"], job["StartInterval"]), ("ai.patinaproject.factory.progress-watch", 300))


if __name__ == "__main__":
    unittest.main()
