from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restart-gateway"
loader = importlib.machinery.SourceFileLoader("restart_gateway", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
restart_gateway = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = restart_gateway
loader.exec_module(restart_gateway)

CARD = {"id": "t_1", "title": "PAT-1: Fix the thing"}
OTHER = {"id": "t_2", "title": "PAT-2: Fix another thing"}
GATEWAY_PID = 10
GATEWAY_START = 1_000_000.0
Process = restart_gateway.Process


def run_of(worker_pid: int | None, ended_at: int | None = None) -> dict:
    return {"id": 1, "worker_pid": worker_pid, "ended_at": ended_at}


def shown(worker_pid: int | None) -> dict:
    return {"task": {"status": "running"}, "runs": [run_of(999, ended_at=5), run_of(worker_pid)]}


def table(*children: tuple[int, int, float]) -> dict:
    """The gateway plus (pid, ppid, start) rows."""
    rows = {GATEWAY_PID: Process(1, GATEWAY_START)}
    rows.update({pid: Process(ppid, start) for pid, ppid, start in children})
    return rows


# t_1's worker is 100 and t_2's is 200; each has one child.
FRESH = (101, 100, GATEWAY_START + 60)
LATE = (101, 100, GATEWAY_START + 3600)
OTHER_CHILD = (201, 200, GATEWAY_START + 3600)
SHOWS = {"t_1": shown(100), "t_2": shown(200)}


class FakeHermes:
    def __init__(
        self,
        running: dict[str, list[list[dict]]],
        shows: dict | None = None,
        archived: tuple[str, ...] = (),
        restart_code: int = 0,
    ):
        self.running = running
        self.shows = SHOWS if shows is None else shows
        self.archived = archived
        self.restart_code = restart_code
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        stdout, code = "", 0
        if args[:3] == ["kanban", "boards", "list"]:
            boards = [{"slug": slug, "archived": False} for slug in self.running]
            boards += [{"slug": slug, "archived": True} for slug in self.archived]
            stdout = json.dumps(boards)
        elif args[0] == "kanban" and args[3] == "list":
            sets = self.running[args[2]]
            stdout = json.dumps(sets.pop(0) if len(sets) > 1 else sets[0])
        elif args[0] == "kanban" and args[3] == "show":
            payload = self.shows[args[4]]
            if payload is None:
                return subprocess.CompletedProcess(args, 1, "", "no such task")
            stdout = payload if isinstance(payload, str) else json.dumps(payload)
        elif args == ["gateway", "restart"]:
            code = self.restart_code
        return subprocess.CompletedProcess(args, code, stdout, "")

    def actions(self) -> list[str]:
        return [args[0] if args[0] != "gateway" else "restart" for args in self.calls if args[0] != "kanban"]

    def shown_on(self) -> list[tuple[str, str]]:
        return [(args[2], args[4]) for args in self.calls if args[0] == "kanban" and args[3] == "show"]


class Tables:
    """Process tables handed out one per read; the last one repeats."""

    def __init__(self, *tables: dict | None):
        self.tables = list(tables)

    def __call__(self) -> dict | None:
        return self.tables.pop(0) if len(self.tables) > 1 else self.tables[0]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def restart(
    hermes: FakeHermes,
    processes,
    paused: bool = False,
    timeout: float = 100,
    gateway_pid=lambda: GATEWAY_PID,
) -> tuple[int, str, Clock]:
    clock, out = Clock(), io.StringIO()
    code = restart_gateway.restart(
        hermes, lambda: paused, gateway_pid, processes, timeout=timeout, poll=30, clock=clock, sleep=clock.sleep, out=out
    )
    return code, out.getvalue(), clock


def raising(exc: Exception):
    def fail():
        raise exc

    return fail


class RestartGatewayTest(unittest.TestCase):
    def test_running_cards_that_are_not_at_risk_never_delay_the_restart(self) -> None:
        hermes = FakeHermes({"default": [[CARD, OTHER]]})

        code, out, clock = restart(hermes, Tables(table(LATE, OTHER_CHILD)))

        self.assertEqual(code, 0)
        self.assertEqual(hermes.actions(), ["pause", "restart", "resume"])
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(out, "restart-gateway: no Kanban card is at risk; restarting the gateway\n")

    def test_waits_only_for_the_at_risk_card_then_restarts_while_paused(self) -> None:
        hermes = FakeHermes({"default": [[CARD, OTHER]]})

        code, out, clock = restart(hermes, Tables(table(FRESH, OTHER_CHILD), table(OTHER_CHILD)))

        self.assertEqual(code, 0)
        self.assertEqual(hermes.actions(), ["pause", "restart", "resume"])
        self.assertEqual(clock.sleeps, [30])
        self.assertEqual(
            out,
            "restart-gateway: waiting for 1 at-risk Kanban card(s)\n  default/t_1 PAT-1: Fix the thing\n"
            "restart-gateway: no Kanban card is at risk; restarting the gateway\n",
        )

    def test_the_grace_after_the_gateway_start_counts_as_at_risk(self) -> None:
        for offset, expected in [(-5, 1), (0, 1), (300, 1), (301, 0)]:
            with self.subTest(offset=offset):
                hermes = FakeHermes({"default": [[CARD]]})
                child = (101, 100, GATEWAY_START + offset)

                code, _, _ = restart(hermes, Tables(table(child)), timeout=0)

                self.assertEqual(code, expected)

    def test_does_not_restart_when_the_at_risk_card_outlasts_the_wait(self) -> None:
        hermes = FakeHermes({"default": [[CARD]], "triage": [[OTHER]]})

        code, out, _ = restart(hermes, Tables(table(FRESH, OTHER_CHILD)), timeout=60)

        self.assertEqual(code, 1)
        self.assertEqual(hermes.actions(), [])
        self.assertTrue(
            out.endswith(
                "restart-gateway: 1 at-risk Kanban card(s) still running after 60s; not restarting\n"
                "  default/t_1 PAT-1: Fix the thing\n"
            ),
            out,
        )

    def test_a_card_on_another_board_is_read_with_that_board(self) -> None:
        hermes = FakeHermes({"default": [[]], "triage": [[OTHER]]})

        code, out, _ = restart(hermes, Tables(table(OTHER_CHILD)), timeout=0)

        self.assertEqual(code, 0)
        self.assertEqual(hermes.shown_on(), [("triage", "t_2"), ("triage", "t_2")])

    def test_a_card_claimed_while_pausing_lifts_the_pause_and_keeps_waiting(self) -> None:
        hermes = FakeHermes({"default": [[CARD]]})

        code, _, _ = restart(hermes, Tables(table(LATE), table(FRESH), table(LATE), table(LATE)))

        self.assertEqual(code, 0)
        self.assertEqual(hermes.actions(), ["pause", "resume", "pause", "restart", "resume"])

    def test_keeps_an_operators_pause_in_place(self) -> None:
        hermes = FakeHermes({"default": [[]]})

        self.assertEqual(restart(hermes, Tables(table()), paused=True)[0], 0)
        self.assertEqual(hermes.actions(), ["restart"])

    def test_skips_archived_boards(self) -> None:
        hermes = FakeHermes({"default": [[]]}, archived=("old",))

        restart(hermes, Tables(table()))

        listed = [args[2] for args in hermes.calls if args[0] == "kanban" and args[1] == "--board"]
        self.assertEqual(set(listed), {"default"})

    def test_returns_the_restart_exit_code_and_still_resumes(self) -> None:
        hermes = FakeHermes({"default": [[]]}, restart_code=3)

        self.assertEqual(restart(hermes, Tables(table()))[0], 3)
        self.assertEqual(hermes.actions(), ["pause", "restart", "resume"])

    def test_a_gateway_restart_that_cannot_run_still_resumes(self) -> None:
        hermes = FakeHermes({"default": [[]]})

        def failing(args: list[str]) -> subprocess.CompletedProcess:
            if args == ["gateway", "restart"]:
                raise OSError("hermes vanished")
            return hermes(args)

        with self.assertRaisesRegex(OSError, "hermes vanished"):
            restart_gateway.restart(
                failing, lambda: False, lambda: GATEWAY_PID, Tables(table()), timeout=0, poll=30, sleep=lambda _: None
            )
        self.assertEqual(hermes.actions(), ["pause", "resume"])

    def test_a_failed_check_after_pausing_lifts_the_pause_and_does_not_restart(self) -> None:
        reads = 0

        def second_read_raises() -> dict:
            nonlocal reads
            reads += 1
            if reads == 2:
                raise RuntimeError("ps exploded")
            return table()

        hermes = FakeHermes({"default": [[]]})

        with self.assertRaisesRegex(RuntimeError, "ps exploded"):
            restart_gateway.restart(
                hermes, lambda: False, lambda: GATEWAY_PID, second_read_raises, timeout=0, poll=30, sleep=lambda _: None
            )
        self.assertEqual(hermes.actions(), ["pause", "resume"])

    def test_a_failed_listing_after_pausing_lifts_the_pause_and_does_not_restart(self) -> None:
        hermes = FakeHermes({"default": [[CARD]]})
        listings = 0

        def failing(args: list[str]) -> subprocess.CompletedProcess:
            nonlocal listings
            if args[:2] == ["kanban", "boards"]:
                listings += 1
                if listings == 2:
                    return subprocess.CompletedProcess(args, 1, "", "database is locked")
            return hermes(args)

        with self.assertRaisesRegex(RuntimeError, "database is locked"):
            restart_gateway.restart(
                failing, lambda: False, lambda: GATEWAY_PID, Tables(table(LATE)), timeout=0, poll=30, sleep=lambda _: None
            )
        self.assertEqual(hermes.actions(), ["pause", "resume"])


class UnknownStateTest(unittest.TestCase):
    def refused(self, hermes: FakeHermes, processes, gateway_pid=lambda: GATEWAY_PID) -> tuple[int, str]:
        code, out, _ = restart(hermes, processes, timeout=0, gateway_pid=gateway_pid)
        self.assertEqual(hermes.actions(), [] if code == 1 else ["pause", "restart", "resume"])
        return code, out

    def test_no_gateway_record_restarts_at_once(self) -> None:
        hermes = FakeHermes({"default": [[CARD]]})

        self.assertEqual(self.refused(hermes, Tables(table(FRESH)), gateway_pid=lambda: None)[0], 0)
        self.assertEqual(hermes.shown_on(), [])

    def test_a_gateway_pid_missing_from_the_process_table_restarts_at_once(self) -> None:
        hermes = FakeHermes({"default": [[CARD]]})

        self.assertEqual(self.refused(hermes, Tables({7: Process(1, GATEWAY_START)}))[0], 0)

    def test_an_unreadable_gateway_record_puts_every_running_card_at_risk(self) -> None:
        hermes = FakeHermes({"default": [[CARD, OTHER]]})

        code, out = self.refused(hermes, Tables(table()), gateway_pid=raising(ValueError("bad")))

        self.assertEqual(code, 1)
        self.assertIn("2 at-risk", out)

    def test_an_unreadable_process_table_puts_every_running_card_at_risk(self) -> None:
        hermes = FakeHermes({"default": [[CARD, OTHER]]})

        code, out = self.refused(hermes, Tables(None))

        self.assertEqual(code, 1)
        self.assertIn("2 at-risk", out)

    def test_a_card_that_cannot_be_shown_is_at_risk(self) -> None:
        bad = {
            "failing show": None,
            "malformed json": "{not json",
            "no runs key": {"task": {}},
            "no current run": {"task": {"status": "running"}, "runs": [run_of(100, ended_at=5)]},
            "null worker pid": shown(None),
        }
        for name, payload in bad.items():
            with self.subTest(name):
                hermes = FakeHermes({"default": [[CARD, OTHER]]}, shows={"t_1": payload, "t_2": shown(200)})

                code, out = self.refused(hermes, Tables(table(LATE, OTHER_CHILD)))

                self.assertEqual(code, 1)
                self.assertIn("1 at-risk", out)
                self.assertIn("default/t_1", out)
                self.assertNotIn("t_2", out)

    def test_a_card_that_ended_after_the_listing_is_not_at_risk(self) -> None:
        ended = {"task": {"status": "blocked"}, "runs": [run_of(100, ended_at=5)]}
        hermes = FakeHermes({"default": [[CARD]]}, shows={"t_1": ended})

        self.assertEqual(self.refused(hermes, Tables(table(FRESH)))[0], 0)

    def test_a_worker_with_no_live_child_is_not_at_risk(self) -> None:
        hermes = FakeHermes({"default": [[CARD]]})

        self.assertEqual(self.refused(hermes, Tables(table((101, 1, GATEWAY_START + 1))))[0], 0)


class ReaderTest(unittest.TestCase):
    def test_parses_a_ps_line(self) -> None:
        parsed = restart_gateway.parse_processes("  101   100 Tue Oct 27 22:32:20 2026\n 5 1 Mon Oct  5 01:02:03 2026\n")

        expected = time.mktime((2026, 10, 27, 22, 32, 20, 0, 0, -1))
        self.assertEqual(parsed[101], Process(100, expected))
        self.assertEqual(parsed[5].ppid, 1)

    def test_refuses_a_line_it_cannot_parse(self) -> None:
        with self.assertRaises(ValueError):
            restart_gateway.parse_processes("101 100 not a date\n")

    def test_reads_the_gateway_pid_from_its_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            read = restart_gateway.read_gateway_pid(root)
            self.assertIsNone(read())
            (root / "gateway.pid").write_text(json.dumps({"pid": 65843, "kind": "hermes-gateway"}))
            self.assertEqual(read(), 65843)
            for bad in ["", "{", "[]", '{"kind": "hermes-gateway"}', '{"pid": "x"}']:
                (root / "gateway.pid").write_text(bad)
                with self.subTest(bad), self.assertRaises(ValueError):
                    read()


class OperatorPausedTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "profiles" / "claude-worker"
        self.home.mkdir(parents=True)

    def test_reads_a_pause_in_the_fleet_root_from_a_profile_home(self) -> None:
        (self.root / "ESTOP").write_text("{}")
        self.assertTrue(restart_gateway.operator_paused(self.home, self.root))

    def test_reads_a_pause_in_the_profile_home(self) -> None:
        (self.home / "ESTOP").write_text("{}")
        self.assertTrue(restart_gateway.operator_paused(self.home, self.root))

    def test_is_false_without_a_sentinel(self) -> None:
        self.assertFalse(restart_gateway.operator_paused(self.home, self.root))


if __name__ == "__main__":
    unittest.main()
