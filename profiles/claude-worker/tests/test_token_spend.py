from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from tests.support import SCRIPTS

OPUS, SONNET, MYSTERY = "claude-opus-5-5", "claude-sonnet-5-5", "claude-mystery-1"
DOLLARS_PER_TOKEN = {OPUS: (1, 10, 0.1, 2, 4), SONNET: (1, 5, 0.5, 1, 2)}
PRICES = {
    model: dict(zip(("input", "output", "cache_read", "cache_write_5m", "cache_write_1h"), (r * 1e6 for r in rates)))
    for model, rates in DOLLARS_PER_TOKEN.items()
}
SESSIONS = (
    "CREATE TABLE sessions (id TEXT, source TEXT, display_name TEXT, title TEXT, model TEXT, started_at REAL, "
    "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
    "estimated_cost_usd REAL)"
)
BOARD = (
    "CREATE TABLE tasks (id TEXT, title TEXT);"
    "CREATE TABLE task_runs (id INTEGER, task_id TEXT, profile TEXT, status TEXT, started_at INTEGER, "
    "ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT)"
)


def stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123Z")


def entry(epoch: float, message_id: str | None, model: str, side: bool = False, cwd: str | None = None, **usage) -> dict:
    message = {"model": model, "usage": usage}
    if message_id:
        message["id"] = message_id
    found = {"type": "assistant", "timestamp": stamp(epoch), "isSidechain": side, "message": message}
    return {**found, "cwd": cwd} if cwd else found


def write_lines(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def split_cache(five_minute: int, one_hour: int = 0) -> dict:
    return {
        "cache_creation_input_tokens": five_minute + one_hour,
        "cache_creation": {"ephemeral_5m_input_tokens": five_minute, "ephemeral_1h_input_tokens": one_hour},
    }


def database(path: Path, script: str, table: str | None = None, rows: list[tuple] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript(script)
        if rows:
            db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows)
        db.commit()


def session(id, source, name, title, model, at, input_, output, read, write):
    return (id, source, name, title, model, at, input_, output, read, write, 999.0)


class TokenSpendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.gateway = root / "home"
        self.worker = self.gateway / "profiles" / "claude-worker"
        self.claude = root / "claude"
        self.prices = root / "prices.json"
        self.prices.write_text(json.dumps(PRICES))
        self.build()

    def build(self):
        database(self.gateway / "state.db", SESSIONS, "sessions", [
            session("w1", "webhook", "webhook/linear", None, SONNET, 1000, 10, 20, 300, 40),
            session("w2", "webhook-retry", None, None, SONNET, 1500, 1, 2, 3, 4),
            session("c1", "cli", None, "PAT-1: chat", OPUS, 1500, 5000, 5000, 5000, 5000),
        ])
        database(self.worker / "state.db", SESSIONS, "sessions", [
            session("s1", "kanban", None, "PAT-1: Do thing", SONNET, 1100, 5, 6, 70, 8),
            session("s2", "kanban", None, "PAT-1: Do thing #2", SONNET, 3000, 1, 1, 1, 1),
            session("s3", "kanban", None, "PAT-2: Other", MYSTERY, 1200, 2, 2, 2, 2),
            session("c2", "cli", None, "PAT-1: chat", OPUS, 1200, 5000, 5000, 5000, 5000),
        ])
        meta = lambda sid, wt, ws=None: json.dumps({"session_id": sid, "worktree": wt, "worker_session_id": ws})
        database(self.gateway / "kanban.db", BOARD, None)
        with closing(sqlite3.connect(self.gateway / "kanban.db")) as db:
            db.executemany("INSERT INTO tasks VALUES (?, ?)", [("t_1", "PAT-1: Do thing"), ("t_2", "PAT-2: Other")])
            db.executemany("INSERT INTO task_runs VALUES (?, ?, 'claude-worker', 'done', ?, ?, ?, NULL, ?)", [
                (1, "t_1", 2000, 3000, "completed", meta("sess-a", "/wt/one", "s1")),
                (2, "t_1", 3000, None, "completed", meta("sess-a", "/wt/one")),
                (3, "t_2", 2000, 4000, "blocked", meta("sess-b", "/wt/two")),
                (4, "t_2", 2000, 4000, "crashed", None),
            ])
            db.commit()
        (self.gateway / "kanban" / "boards" / "empty").mkdir(parents=True)
        (self.gateway / "kanban" / "boards" / "empty" / "kanban.db").write_bytes(b"")
        database(self.gateway / "kanban" / "boards" / "bare" / "kanban.db", "CREATE TABLE tasks (id TEXT, title TEXT)")

        projects = self.claude / "projects"
        write_lines(projects / "-wt-one" / "sess-a.jsonl", [
            entry(2010, "m1", OPUS, cwd="/wt/one", input_tokens=2, cache_read_input_tokens=100, output_tokens=5, **split_cache(30, 20)),
            entry(2011, "m1", OPUS, input_tokens=2, cache_read_input_tokens=100, output_tokens=9, **split_cache(30, 20)),
            entry(2500, "m2", OPUS, input_tokens=1, cache_read_input_tokens=200, output_tokens=7, **split_cache(10)),
            entry(2600, "m3", SONNET, side=True, input_tokens=3, cache_read_input_tokens=30, output_tokens=4),
            entry(2700, "synthetic", "<synthetic>", input_tokens=900, output_tokens=900),
            entry(3500, "m4", OPUS, input_tokens=4, cache_read_input_tokens=400, output_tokens=8),
            entry(3600, None, OPUS, input_tokens=1, output_tokens=1),
        ])
        write_lines(projects / "-wt-one" / "sess-a" / "subagents" / "agent-1.jsonl", [
            entry(2200, "a1", SONNET, True, input_tokens=6, cache_read_input_tokens=60, output_tokens=1, **split_cache(2)),
            entry(2201, "a1", SONNET, True, input_tokens=6, cache_read_input_tokens=60, output_tokens=11, **split_cache(2)),
            entry(3700, "a2", SONNET, True, input_tokens=2, cache_read_input_tokens=20, output_tokens=3),
        ])
        write_lines(projects / "-wt-else" / "sess-a.jsonl", [
            entry(2100, "x", OPUS, cwd="/wt/else", input_tokens=777777, output_tokens=777777),
        ])
        write_lines(projects / "-wt-two" / "sess-b.jsonl", [
            entry(2100, "b1", SONNET, cwd="/wt/two", input_tokens=1, cache_read_input_tokens=2, output_tokens=4, **split_cache(3)),
            entry(2200, "b2", MYSTERY, input_tokens=10, output_tokens=10),
        ])

    def run_script(self, *args: str, text: bool = False):
        command = [
            sys.executable, str(SCRIPTS / "token-spend"), "--worker-home", str(self.worker),
            "--claude-config-dir", str(self.claude), "--prices", str(self.prices), *args,
        ]
        done = subprocess.run(command + ([] if text else ["--json"]), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout if text else json.loads(done.stdout)

    @staticmethod
    def buckets(report: dict) -> dict:
        return {b["bucket"]: b for b in report["buckets"]}

    @staticmethod
    def classes(bucket: dict) -> tuple:
        t = bucket["tokens"]
        return t["input"], t["cache_read"], t["cache_write"], t["output"], t["total"]

    def test_each_bucket_holds_exactly_its_fixture_tokens(self):
        report = self.run_script()
        buckets = self.buckets(report)
        self.assertEqual(list(buckets), ["webhook", "dispatch", "startup", "conversation", "subagent"])
        self.assertEqual(self.classes(buckets["webhook"]), (11, 303, 44, 22, 380))
        self.assertEqual(self.classes(buckets["dispatch"]), (8, 73, 11, 9, 101))
        self.assertEqual(self.classes(buckets["startup"]), (7, 502, 53, 0, 562))
        self.assertEqual(self.classes(buckets["conversation"]), (12, 200, 10, 39, 261))
        self.assertEqual(self.classes(buckets["subagent"]), (11, 110, 2, 18, 141))
        self.assertEqual(report["total"]["tokens"]["total"], 380 + 101 + 562 + 261 + 141)
        self.assertEqual([r["route"] for r in report["webhook_routes"]], ["webhook-retry", "webhook/linear"])
        self.assertEqual([r["tokens"]["total"] for r in report["webhook_routes"]], [10, 370])

    def test_main_and_subagent_are_separate_and_message_ids_count_once(self):
        runs = {r["run_id"]: r for r in self.run_script("--card", "PAT-1")["card"]["runs"]}
        first = runs[1]
        self.assertEqual(first["startup"]["tokens"]["total"], 152)
        self.assertEqual(first["conversation"]["tokens"]["total"], 227)
        self.assertEqual(first["subagent"]["tokens"]["total"], 116)
        self.assertEqual(first["total"]["tokens"]["total"], 161 + 218 + 37 + 79)
        second = runs[2]
        self.assertEqual(
            (second["startup"]["tokens"]["total"], second["conversation"]["tokens"]["total"], second["subagent"]["tokens"]["total"]),
            (404, 10, 25),
        )

    def test_cost_is_tokens_times_rate_and_ranks_apart_from_tokens(self):
        buckets = self.buckets(self.run_script())
        self.assertEqual(
            {k: b["cost_usd"] for k, b in buckets.items()},
            {"webhook": 316.5, "dispatch": 85.5, "startup": 201.0, "conversation": 312.0, "subagent": 158.0},
        )
        by_cost = sorted(buckets, key=lambda k: -buckets[k]["cost_usd"])
        by_tokens = sorted(buckets, key=lambda k: -buckets[k]["tokens"]["total"])
        self.assertEqual(by_cost, ["webhook", "conversation", "startup", "subagent", "dispatch"])
        self.assertEqual(by_tokens, ["startup", "webhook", "conversation", "subagent", "dispatch"])

    def test_one_hour_cache_writes_are_priced_apart_from_five_minute_writes(self):
        startup = self.buckets(self.run_script())["startup"]
        opus = next(m for m in startup["models"] if m["model"] == OPUS)
        self.assertEqual(opus["cost_usd"], 152 + 44)

    def test_unpriced_model_keeps_its_tokens_and_is_flagged(self):
        report = self.run_script()
        conversation = self.buckets(report)["conversation"]
        mystery = next(m for m in conversation["models"] if m["model"] == MYSTERY)
        self.assertEqual((mystery["tokens"]["total"], mystery["cost_usd"]), (20, None))
        self.assertEqual(conversation["unpriced_models"], [MYSTERY])
        self.assertEqual(self.buckets(report)["dispatch"]["unpriced_models"], [MYSTERY])
        self.assertEqual(self.buckets(report)["startup"]["unpriced_models"], [])
        self.assertIn(f"unpriced: {MYSTERY}", self.run_script(text=True))

    def test_card_by_task_id_and_by_issue_list_runs_and_sessions(self):
        for wanted in ("t_1", "PAT-1"):
            report = self.run_script("--card", wanted)
            card = report["card"]
            self.assertEqual([(r["run_id"], r["task_id"], r["outcome"]) for r in card["runs"]], [(1, "t_1", "completed"), (2, "t_1", "completed")])
            self.assertEqual([(s["session_id"], s["tokens"]["total"]) for s in card["sessions"]], [("s1", 89), ("s2", 4)])
            self.assertEqual(report["total"]["tokens"]["total"], 556 + 237 + 141 + 93, wanted)
            self.assertEqual(report["total"]["cost_usd"], 731.5, wanted)
            self.assertEqual(self.buckets(report)["webhook"]["tokens"]["total"], 0)

    def test_window_counts_only_what_starts_inside_it(self):
        after = self.buckets(self.run_script("--since", "3000"))
        self.assertEqual({k: b["tokens"]["total"] for k, b in after.items()},
                         {"webhook": 0, "dispatch": 4, "startup": 404, "conversation": 10, "subagent": 25})
        before = self.buckets(self.run_script("--until", "3000"))
        self.assertEqual({k: b["tokens"]["total"] for k, b in before.items()},
                         {"webhook": 380, "dispatch": 97, "startup": 158, "conversation": 251, "subagent": 116})

    def test_dates_are_utc_and_epochs_pass_through(self):
        report = self.run_script("--since", "1970-01-01T00:20:00", "--until", "1970-01-01")
        self.assertEqual(report["window"], {"since": 1200.0, "until": 0.0})

    def test_script_writes_nothing(self):
        def snapshot():
            root = Path(self.tmp.name)
            return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}

        before = snapshot()
        self.run_script()
        self.run_script("--card", "PAT-1", text=True)
        self.assertEqual(snapshot(), before)

    def test_empty_board_file_is_tolerated(self):
        empty = self.gateway / "kanban" / "boards" / "empty" / "kanban.db"
        self.assertEqual(empty.stat().st_size, 0)
        self.assertEqual(self.buckets(self.run_script())["startup"]["tokens"]["total"], 562)

    def test_missing_homes_name_the_flag(self):
        env = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
        done = subprocess.run([sys.executable, str(SCRIPTS / "token-spend")], capture_output=True, text=True, env=env)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("--worker-home", done.stderr)
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "token-spend"), "--worker-home", str(self.claude)], capture_output=True, text=True, env=env
        )
        self.assertIn("--gateway-home", done.stderr)


if __name__ == "__main__":
    unittest.main()
