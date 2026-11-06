from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import ledger
from tests.support import worker_body


class ParseDeliveryTest(unittest.TestCase):
    def test_a_worker_body_becomes_its_raw_message_and_attachments(self):
        delivery = ledger.parse_delivery(worker_body())

        self.assertEqual(delivery.message_id, "<invoice-1@vendor.example>")
        self.assertEqual(
            [(item.key, item.file) for item in delivery.objects],
            [("raw/invoice-1.eml", "message.eml"), ("attachments/invoice-1/invoice.pdf", "01-invoice.pdf")],
        )
        self.assertEqual(delivery.headers["subject"], "Your invoice")

    def test_unsafe_message_ids_are_rejected(self):
        for message_id, reason in (
            ("a" * 300, "300 characters"),
            ("<a/b@example.com>", "safe set"),
            ("<a b@example.com>", "safe set"),
            ("", "missing"),
        ):
            with self.subTest(message_id=message_id[:20]):
                with self.assertRaisesRegex(ledger.DeliveryError, reason):
                    ledger.parse_delivery(worker_body(message_id=message_id))

    def test_a_255_character_message_id_is_kept(self):
        self.assertEqual(len(ledger.parse_delivery(worker_body(message_id="a" * 255)).message_id), 255)

    def test_an_object_key_that_climbs_out_is_rejected(self):
        body = worker_body()
        body["attachments"][0]["key"] = "attachments/../secrets"
        with self.assertRaisesRegex(ledger.DeliveryError, r"attachments\[0\]\.key"):
            ledger.parse_delivery(body)

    def test_an_attachment_name_never_becomes_a_path(self):
        body = worker_body()
        body["attachments"][0]["filename"] = "../../.ssh/authorized_keys"
        self.assertEqual(ledger.parse_delivery(body).objects[1].file, "01-ssh_authorized_keys")

    def test_the_gateway_watch_probe_body_is_not_a_delivery(self):
        with self.assertRaisesRegex(ledger.DeliveryError, "message_id is missing"):
            ledger.parse_delivery({"type": "GatewayWatch"})


class OutcomeTest(unittest.TestCase):
    def test_each_kind_formats_to_one_line(self):
        self.assertEqual(ledger.format_outcome("new-issue", "PAT-12"), "new-issue PAT-12")
        self.assertEqual(ledger.format_outcome("duplicate", "PAT-7"), "duplicate PAT-7")
        self.assertEqual(ledger.format_outcome("no-action", "a receipt for a paid invoice"), "no-action a receipt for a paid invoice")

    def test_bad_outcomes_are_refused(self):
        for kind, value in (("new-issue", "not an id"), ("duplicate", ""), ("no-action", ""), ("no-action", "a\nb"), ("ignore", "x")):
            with self.subTest(kind=kind, value=value):
                with self.assertRaises(ValueError):
                    ledger.format_outcome(kind, value)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "email-intake" / "ledger.db"

    def tearDown(self):
        self.directory.cleanup()

    def test_only_the_first_claim_of_a_message_id_wins_even_concurrently(self):
        delivery = ledger.parse_delivery(worker_body())
        wins = []
        threads = [threading.Thread(target=lambda: wins.append(ledger.Ledger(self.path).claim(delivery))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(wins), [False] * 7 + [True])
        self.assertEqual([row.message_id for row in ledger.Ledger(self.path).rows()], ["<invoice-1@vendor.example>"])

    def test_a_state_moves_once_and_only_along_a_transition(self):
        store = ledger.Ledger(self.path)
        store.claim(ledger.parse_delivery(worker_body()))
        message_id = "<invoice-1@vendor.example>"

        self.assertTrue(store.move(message_id, ledger.CLAIMED, ledger.CARDED, card_id="t_1"))
        self.assertFalse(store.move(message_id, ledger.CLAIMED, ledger.CARDED, card_id="t_2"))
        with self.assertRaises(ValueError):
            store.move(message_id, ledger.CARDED, ledger.READY)
        self.assertEqual(store.get(message_id).card_id, "t_1")

    def test_an_outcome_is_recorded_once_and_only_when_the_files_are_ready(self):
        store = ledger.Ledger(self.path)
        store.claim(ledger.parse_delivery(worker_body()))
        message_id = "<invoice-1@vendor.example>"
        store.move(message_id, ledger.CLAIMED, ledger.CARDED, card_id="t_1")
        store.move(message_id, ledger.CARDED, ledger.FETCHING)

        self.assertFalse(store.set_outcome("t_1", None, "no-action early"))
        store.move(message_id, ledger.FETCHING, ledger.READY)
        self.assertTrue(store.set_outcome("t_1", None, "duplicate PAT-7"))
        self.assertFalse(store.set_outcome("t_1", None, "new-issue PAT-8"))
        self.assertEqual(store.by_card("t_1").outcome, "duplicate PAT-7")


if __name__ == "__main__":
    unittest.main()
