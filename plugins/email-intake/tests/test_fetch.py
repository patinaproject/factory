from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import fetch
import ledger
from tests.support import HOST, PDF, RAW, SECRET, FakeBoard, ObjectHost, worker_body

MESSAGE_ID = "<invoice-1@vendor.example>"


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.host = ObjectHost({"raw/invoice-1.eml": RAW, "attachments/invoice-1/invoice.pdf": PDF})
        self.addCleanup(self.host.close)
        self.store = ledger.Ledger(ledger.ledger_path(self.root))
        self.board = FakeBoard(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def intake(self, secret=SECRET):
        return fetch.Intake(self.store, self.board.board(), HOST, lambda: secret, opener=self.host.opener)

    def claim(self, body=None):
        self.store.claim(ledger.parse_delivery(body or worker_body()))

    def test_a_listed_pdf_lands_in_the_cards_directory_and_the_card_unblocks(self):
        self.claim()
        intake = self.intake()

        self.assertEqual(intake.deliver(MESSAGE_ID), {"ok": True, "state": "fetching", "card_id": "t_0001"})
        self.assertEqual(intake.fetch(MESSAGE_ID), "ready")

        directory = self.root / "attachments" / "t_0001"
        self.assertEqual(sorted(os.listdir(directory)), ["01-invoice.pdf", "email.json", "message.eml"])
        self.assertEqual(hashlib.sha256((directory / "01-invoice.pdf").read_bytes()).hexdigest(), hashlib.sha256(PDF).hexdigest())
        self.assertEqual(stat.S_IMODE((directory / "01-invoice.pdf").stat().st_mode), 0o600)
        self.assertEqual(json.loads((directory / "email.json").read_text())["text"], "Your invoice is attached.")
        self.assertEqual(self.board.cards["t_0001"]["status"], "ready")
        self.assertEqual(self.board.cards["t_0001"]["assignee"], "mail-worker")
        self.assertEqual(
            self.host.requests,
            [("/raw/invoice-1.eml", True), ("/attachments/invoice-1/invoice.pdf", True)],
        )
        self.assertEqual(self.store.get(MESSAGE_ID).state, "ready")

    def test_a_sha256_mismatch_leaves_the_card_blocked_with_a_reason_naming_the_attachment(self):
        body = worker_body()
        body["attachments"][0]["sha256"] = "0" * 64
        self.claim(body)
        intake = self.intake()
        intake.deliver(MESSAGE_ID)

        self.assertEqual(intake.fetch(MESSAGE_ID), "failed")

        self.assertEqual(self.board.cards["t_0001"]["status"], "blocked")
        self.assertEqual(self.board.unblocked, [])
        (card_id, reason), = self.board.comments
        self.assertEqual(card_id, "t_0001")
        self.assertIn("01-invoice.pdf (attachments/invoice-1/invoice.pdf) has SHA-256", reason)
        row = self.store.get(MESSAGE_ID)
        self.assertEqual((row.state, row.outcome), ("failed", None))
        self.assertFalse((self.root / "attachments" / "t_0001" / "01-invoice.pdf").exists())

    def test_an_object_larger_than_listed_is_refused(self):
        body = worker_body()
        body["raw"]["size"] = 10
        self.claim(body)
        intake = self.intake()
        intake.deliver(MESSAGE_ID)

        self.assertEqual(intake.fetch(MESSAGE_ID), "failed")
        self.assertIn("message.eml (raw/invoice-1.eml) is larger than the listed 10 bytes", self.board.comments[0][1])

    def test_an_unknown_message_id_makes_no_request_and_no_card(self):
        intake = self.intake()

        self.assertEqual(intake.deliver(MESSAGE_ID), {"ok": False, "error": "unknown message_id"})
        with self.assertRaises(fetch.FetchError):
            intake.fetch(MESSAGE_ID)
        self.assertEqual((self.host.requests, self.board.cards), ([], {}))

    def test_the_socket_takes_only_a_message_id_and_fetches_only_that_rows_keys(self):
        self.claim()
        server = fetch.Server(ledger.socket_path(self.root), self.intake())
        server.start()
        self.addCleanup(server.stop)
        import socket

        def ask(request):
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(ledger.socket_path(self.root)))
                client.sendall(json.dumps(request).encode() + b"\n")
                return json.loads(client.makefile().readline())

        self.assertEqual(
            ask({"op": "fetch", "url": f"https://{HOST}/secrets", "message_id": MESSAGE_ID}),
            {"ok": False, "error": "ValueError: the only operation is deliver"},
        )
        self.assertEqual(ask({"op": "deliver", "message_id": "<other@example.com>"}), {"ok": False, "error": "unknown message_id"})
        self.assertEqual(self.host.requests, [])
        self.assertEqual(stat.S_IMODE(ledger.socket_path(self.root).stat().st_mode), 0o600)

    def test_a_redelivery_and_a_restart_create_no_second_card(self):
        self.claim()
        intake = self.intake()
        intake.deliver(MESSAGE_ID)
        intake.fetch(MESSAGE_ID)

        self.assertFalse(self.store.claim(ledger.parse_delivery(worker_body())))
        restarted = self.intake()
        self.assertEqual(restarted.resume(), [])
        self.assertEqual(restarted.deliver(MESSAGE_ID), {"ok": True, "state": "ready", "card_id": "t_0001"})
        self.assertEqual(list(self.board.cards), ["t_0001"])

    def test_a_restart_mid_fetch_resumes_and_keeps_the_files_already_written(self):
        self.claim()
        intake = self.intake()
        intake.deliver(MESSAGE_ID)
        directory = self.root / "attachments" / "t_0001"
        directory.mkdir(parents=True)
        (directory / "message.eml").write_bytes(RAW)

        self.assertEqual(self.intake().resume(), [MESSAGE_ID])

        self.assertEqual(self.store.get(MESSAGE_ID).state, "ready")
        self.assertEqual(self.host.requests, [("/attachments/invoice-1/invoice.pdf", True)])

    def test_a_missing_secret_fails_the_card_without_a_request(self):
        self.claim()
        intake = self.intake(secret=None)
        intake.deliver(MESSAGE_ID)

        self.assertEqual(intake.fetch(MESSAGE_ID), "failed")
        self.assertEqual(self.host.requests, [])
        self.assertIn("signing secret is not set", self.board.comments[0][1])


class SignatureTest(unittest.TestCase):
    def test_the_get_signature_is_hmac_of_get_timestamp_and_path(self):
        self.assertEqual(fetch.signature("s", "1700000000", "/raw/a.eml"), "2a4460ae31a06d48e71fccfbe92b1d6ac021532291c1e996aae3be3ce34c867a")


if __name__ == "__main__":
    unittest.main()
