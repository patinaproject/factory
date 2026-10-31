from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from tests.support import SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import install_plan  # noqa: E402
from install_plan import ADVISORY, Drift, Install, Manual, Plan, Step  # noqa: E402


def install(step: Step, *argv: str, restarts: bool = False) -> Install:
    return Install(step, argv, restarts)


ROUTE = install(Step.ROUTES, "hermes", "config", "set", "routes.github", "{}", restarts=True)
PROFILE = install(Step.PROFILE, "hermes", "profile", "update", "claude-worker", "-y")
BOARD = install(Step.BOARDS, "hermes", "kanban", "boards", "create", "triage")
WRAPPER = install(Step.WRAPPERS, "install", "-m", "0644", "a", "b")


class PlanTest(unittest.TestCase):
    def test_installs_come_in_step_order_and_manual_lines_apart(self) -> None:
        drifts = [
            Drift("webhook config", "routes.github", "differs", ROUTE),
            Drift("dashboard sign-in", "/root", "dashboard.public_url is not set", Manual("follow step 6")),
            Drift("script", "/root/scripts/x", "is missing", WRAPPER),
            Drift("profile", "claude-worker", "SOUL.md is missing", PROFILE),
            Drift("webhook route", "github", "3 rejected", ADVISORY),
            Drift("kanban board", "triage", "is missing", BOARD),
        ]

        self.assertEqual(install_plan.plan(drifts), Plan(
            (BOARD, PROFILE, ROUTE, WRAPPER),
            (install_plan.ManualLine("dashboard sign-in /root: dashboard.public_url is not set", "follow step 6"),),
        ))

    def test_a_manual_line_never_removes_an_install(self) -> None:
        drifts = [
            Drift("config", "model.default in /root", "is not set", Manual("set it")),
            Drift("profile", "claude-worker", "SOUL.md is missing", PROFILE),
        ]

        self.assertEqual(install_plan.plan(drifts).installs, (PROFILE,))

    def test_one_install_named_by_several_lines_runs_once(self) -> None:
        drifts = [Drift("profile", "claude-worker", f"{name} is missing", PROFILE) for name in ("SOUL.md", "scripts/x")]

        self.assertEqual(install_plan.plan(drifts).installs, (PROFILE,))

    def test_the_plan_restarts_the_gateway_only_when_an_install_needs_it(self) -> None:
        self.assertFalse(install_plan.plan([Drift("profile", "claude-worker", "x", PROFILE)]).restarts_gateway)
        self.assertTrue(install_plan.plan([Drift("webhook config", "k", "x", ROUTE)]).restarts_gateway)

    def test_planning_leaves_its_input_unchanged(self) -> None:
        drifts = [Drift("script", "w", "x", WRAPPER), Drift("profile", "claude-worker", "x", PROFILE)]
        before = list(drifts)

        install_plan.plan(drifts)

        self.assertEqual(drifts, before)


class RecordTest(unittest.TestCase):
    def test_a_record_prints_as_its_line_and_reads_back_as_written(self) -> None:
        for remedy in (ROUTE, Manual("set it"), ADVISORY):
            with self.subTest(remedy=remedy):
                drift = Drift("config", "kanban.max_spawn in /root", "is not a positive limit (part 10)", remedy)
                record = json.loads(json.dumps(drift.to_json()))

                self.assertEqual(record["line"], "config kanban.max_spawn in /root: is not a positive limit (part 10)")
                self.assertEqual(install_plan.drift_from_json(record), drift)

    def test_a_manual_record_without_gates_runs_reads_as_false_and_the_flag_reaches_the_plan(self) -> None:
        old = {"kind": "x", "where": "y", "detail": "z", "remedy": {"type": "manual", "action": "do it"}}
        self.assertEqual(install_plan.drift_from_json(old).remedy, Manual("do it", False))

        gating = Drift("service", "gh login refresh", "is not loaded", Manual("load it", True))
        self.assertEqual(json.loads(json.dumps(gating.to_json()))["remedy"]["gates_runs"], True)
        self.assertEqual(
            install_plan.plan([gating, Drift("x", "y", "z", Manual("do it"))]).manual,
            (install_plan.ManualLine("service gh login refresh: is not loaded", "load it", True),
             install_plan.ManualLine("x y: z", "do it", False)),
        )

    def test_a_record_of_another_shape_is_refused(self) -> None:
        for record in ({}, {"kind": "x", "where": "y", "detail": "z", "remedy": {"type": "later"}}, []):
            with self.subTest(record=record), self.assertRaises(ValueError):
                install_plan.drift_from_json(record)


class CommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.homes = install_plan.Homes("/bin/hermes", "/usr/bin/python3", Path("/r"), Path("/r/profiles/claude-worker"), Path("/src"))

    def test_a_worker_home_command_selects_the_profile_and_restarts_nothing(self) -> None:
        self.assertEqual(
            install_plan.plugin_install(self.homes, self.homes.worker, "p1", reinstall=True),
            Install(Step.PLUGINS, (
                "/bin/hermes", "-p", "claude-worker", "plugins", "install", "file:///src#plugins/p1", "--enable",
                "--yes-deps", "--force",
            ), False),
        )

    def test_a_root_home_command_restarts_the_gateway(self) -> None:
        self.assertEqual(
            install_plan.config_set(self.homes, self.homes.root, "kanban.review_dispatch", False),
            Install(Step.CONFIG, ("/bin/hermes", "config", "set", "kanban.review_dispatch", "false"), True),
        )

    def test_a_string_value_is_set_verbatim_and_any_other_as_json(self) -> None:
        self.assertEqual(
            [install_plan.config_set(self.homes, self.homes.root, "k", value).argv[-1] for value in ("127.0.0.1", 400, [], None)],
            ["127.0.0.1", "400", "[]", "null"],
        )

    def test_a_cron_job_gets_the_runbook_schedule(self) -> None:
        self.assertEqual(
            install_plan.cron_command(self.homes, self.homes.worker, "create", install_plan.schedule(60), "--name", "x"),
            Install(Step.CRON, ("/bin/hermes", "-p", "claude-worker", "cron", "create", "every 1h", "--name", "x"), False),
        )
        self.assertEqual(install_plan.schedule(5), "every 5m")

    def test_a_board_without_a_default_repository_gets_no_workdir(self) -> None:
        self.assertEqual(
            install_plan.board_create(self.homes, "triage", "Triage", None).argv,
            ("/bin/hermes", "kanban", "boards", "create", "triage", "--name", "Triage"),
        )


if __name__ == "__main__":
    unittest.main()
