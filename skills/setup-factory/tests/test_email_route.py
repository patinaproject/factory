from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "skills" / "setup-factory" / "scripts" / "email-route"
PLUGIN = REPO / "plugins" / "email-intake"
sys.path.insert(0, str(PLUGIN))

import fetch  # noqa: E402
import ledger  # noqa: E402

_support_spec = importlib.util.spec_from_file_location("email_intake_support", PLUGIN / "tests" / "support.py")
support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(support)
sys.path.remove(str(PLUGIN))
FakeBoard, worker_body = support.FakeBoard, support.worker_body


class EmailRouteTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "scripts").mkdir()
        shutil.copy(SCRIPT, self.root / "scripts" / "email-route")
        (self.root / "plugins").mkdir()
        shutil.copytree(PLUGIN, self.root / "plugins" / "email-intake")
        self.board = FakeBoard(self.root)
        self.store = ledger.Ledger(ledger.ledger_path(self.root))
        # No object host is reachable, so each fetch fails; these tests cover the route, not the fetch.
        intake = fetch.Intake(self.store, self.board.board(), "objects.invalid", lambda: None)
        intake.deliver_and_fetch = intake.deliver
        self.server = fetch.Server(ledger.socket_path(self.root), intake)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.directory.cleanup()

    def run_route(self, body) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.root / "scripts" / "email-route")],
            input=json.dumps(body), capture_output=True, text=True, timeout=30,
        )

    def test_a_signed_delivery_claims_one_row_creates_one_blocked_mail_card_and_starts_no_run(self):
        result = self.run_route(worker_body())

        self.assertEqual((result.returncode, result.stdout), (0, "[SILENT]"))
        self.assertEqual([(row.message_id, row.card_id) for row in self.store.rows()], [("<invoice-1@vendor.example>", "t_0001")])
        self.assertEqual(self.board.cards, {"t_0001": {"key": "email:<invoice-1@vendor.example>", "status": "blocked", "assignee": "mail-worker"}})

    def test_the_same_message_id_twice_concurrently_and_again_later_makes_one_row_and_one_card(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.run_route(worker_body()), range(4)))
        cards = list(self.board.cards)
        # As if the first card were archived and deleted: the ledger alone must still refuse a second card.
        self.board.cards.clear()
        later = self.run_route(worker_body())

        self.assertEqual({(result.returncode, result.stdout) for result in [*results, later]}, {(0, "[SILENT]")})
        self.assertEqual(len(self.store.rows()), 1)
        self.assertEqual((cards, self.board.cards), (["t_0001"], {}))

    def test_unsafe_message_ids_and_the_probe_body_make_no_row_and_no_card_and_log_why(self):
        for body, reason in (
            (worker_body(message_id="a" * 300), "300 characters"),
            (worker_body(message_id="<a/b@example.com>"), "safe set"),
            ({"type": "GatewayWatch"}, "message_id is missing"),
        ):
            with self.subTest(reason=reason):
                result = self.run_route(body)
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertIn("email-route: dropped the delivery: DeliveryError: ", result.stderr)
                self.assertIn(reason, result.stderr)
        self.assertEqual((self.store.rows(), self.board.cards), ([], {}))

    def test_a_claimed_row_whose_service_was_down_is_carded_on_redelivery(self):
        self.server.stop()
        down = self.run_route(worker_body())
        self.server = fetch.Server(ledger.socket_path(self.root), self.server.intake)
        self.server.start()
        again = self.run_route(worker_body())

        self.assertEqual(down.returncode, 1)
        self.assertEqual((again.returncode, again.stdout), (0, "[SILENT]"))
        self.assertEqual(list(self.board.cards), ["t_0001"])


if __name__ == "__main__":
    unittest.main()
