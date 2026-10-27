from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
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


class FakeHermes:
    def __init__(self, running: dict[str, list[list[dict]]], archived: tuple[str, ...] = (), restart_code: int = 0):
        self.running = running
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
        elif args == ["gateway", "restart"]:
            code = self.restart_code
        return subprocess.CompletedProcess(args, code, stdout, "")

    def actions(self) -> list[str]:
        return [args[0] if args[0] != "gateway" else "restart" for args in self.calls if args[0] != "kanban"]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def restart(hermes: FakeHermes, paused: bool = False, timeout: float = 100, wait_only: bool = False) -> tuple[int, str]:
    clock, out = Clock(), io.StringIO()
    code = restart_gateway.restart(
        hermes, lambda: paused, timeout=timeout, poll=30, settle=10, clock=clock, sleep=clock.sleep, out=out,
        wait_only=wait_only,
    )
    return code, out.getvalue()


class RestartGatewayTest(unittest.TestCase):
    def test_waits_for_the_running_card_then_restarts_while_paused(self) -> None:
        hermes = FakeHermes({"default": [[CARD], []]})

        code, out = restart(hermes)

        self.assertEqual(code, 0)
        self.assertEqual(hermes.actions(), ["pause", "restart", "resume"])
        self.assertEqual(
            out,
            "restart-gateway: waiting for 1 running Kanban card(s)\n  default/t_1 PAT-1: Fix the thing\n"
            "restart-gateway: no Kanban card is running; restarting the gateway\n",
        )

    def test_does_not_restart_when_the_wait_expires(self) -> None:
        hermes = FakeHermes({"default": [[CARD]], "triage": [[OTHER]]})

        code, out = restart(hermes, timeout=60)

        self.assertEqual(code, 1)
        self.assertEqual(hermes.actions(), [])
        self.assertTrue(
            out.endswith(
                "restart-gateway: 2 Kanban card(s) still running after 60s; not restarting\n"
                "  default/t_1 PAT-1: Fix the thing\n  triage/t_2 PAT-2: Fix another thing\n"
            ),
            out,
        )

    def test_a_card_spawned_while_pausing_lifts_the_pause_and_keeps_waiting(self) -> None:
        hermes = FakeHermes({"default": [[], [CARD], [CARD], [], []]})

        code, _ = restart(hermes)

        self.assertEqual(code, 0)
        self.assertEqual(hermes.actions(), ["pause", "resume", "pause", "restart", "resume"])

    def test_keeps_an_operators_pause_in_place(self) -> None:
        hermes = FakeHermes({"default": [[]]})

        self.assertEqual(restart(hermes, paused=True)[0], 0)
        self.assertEqual(hermes.actions(), ["restart"])

    def test_wait_only_exits_zero_once_the_board_is_empty_and_changes_nothing(self) -> None:
        hermes = FakeHermes({"default": [[CARD], [], []]})

        code, out = restart(hermes, wait_only=True)

        self.assertEqual(code, 0)
        self.assertEqual(hermes.actions(), [])
        self.assertTrue(out.endswith("restart-gateway: no Kanban card is running\n"), out)

    def test_wait_only_keeps_waiting_when_a_card_appears_during_the_settle(self) -> None:
        hermes = FakeHermes({"default": [[], [CARD], [CARD], [], []]})

        code, _ = restart(hermes, wait_only=True)

        self.assertEqual(code, 0)
        self.assertEqual(hermes.actions(), [])

    def test_wait_only_fails_with_the_card_list_when_the_wait_expires(self) -> None:
        hermes = FakeHermes({"default": [[CARD]]})

        code, out = restart(hermes, timeout=60, wait_only=True)

        self.assertEqual(code, 1)
        self.assertEqual(hermes.actions(), [])
        self.assertIn("1 Kanban card(s) still running after 60s", out)

    def test_skips_archived_boards(self) -> None:
        hermes = FakeHermes({"default": [[]]}, archived=("old",))

        restart(hermes)

        listed = [args[2] for args in hermes.calls if args[0] == "kanban" and args[1] == "--board"]
        self.assertEqual(set(listed), {"default"})

    def test_returns_the_restart_exit_code_and_still_resumes(self) -> None:
        hermes = FakeHermes({"default": [[]]}, restart_code=3)

        self.assertEqual(restart(hermes)[0], 3)
        self.assertEqual(hermes.actions(), ["pause", "restart", "resume"])

    def test_a_failed_listing_after_pausing_lifts_the_pause_and_does_not_restart(self) -> None:
        hermes = FakeHermes({"default": [[]]})
        listings = 0

        def failing(args: list[str]) -> subprocess.CompletedProcess:
            nonlocal listings
            if args[:2] == ["kanban", "boards"]:
                listings += 1
                if listings == 2:
                    return subprocess.CompletedProcess(args, 1, "", "database is locked")
            return hermes(args)

        with self.assertRaisesRegex(RuntimeError, "database is locked"):
            restart_gateway.restart(failing, lambda: False, timeout=0, poll=30, settle=0, sleep=lambda _: None)
        self.assertEqual(hermes.actions(), ["pause", "resume"])


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
