from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from tests import load_plugin

plugin = load_plugin()
idempotency = plugin.idempotency
LinearError = plugin.linear.LinearError

KEY = "5b0e3a1c-8d41-4f6e-9a57-2c1d0e7f9b34:2"


class OnceTest(unittest.TestCase):
    def setUp(self):
        self.store = os.path.join(tempfile.mkdtemp(), "idempotency")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.store))
        self.sent = []

    def send(self, value="first"):
        def run():
            self.sent.append(value)
            return {"url": f"https://example.test/{value}"}

        return run

    def test_a_repeat_returns_the_stored_result_and_sends_nothing(self):
        args = {"issue": "ABC-1", "body": "Hi", "idempotency_key": KEY}
        first = idempotency.once(self.store, "t", KEY, args, self.send("first"))
        again = idempotency.once(self.store, "t", KEY, args, self.send("second"))
        self.assertEqual(first, {"url": "https://example.test/first"})
        self.assertEqual(again, {"url": "https://example.test/first"})
        self.assertEqual(self.sent, ["first"])

    def test_the_record_is_one_file_holding_the_digest_state_and_result(self):
        idempotency.once(self.store, "t", KEY, {"a": 1}, self.send())
        (name,) = os.listdir(self.store)
        record = json.load(open(os.path.join(self.store, name)))
        self.assertEqual(sorted(record), ["args_digest", "result", "state"])
        self.assertEqual((record["state"], record["result"]), ("done", {"url": "https://example.test/first"}))

    def test_the_key_itself_is_not_part_of_the_compared_arguments(self):
        idempotency.once(self.store, "t", KEY, {"a": 1, "idempotency_key": KEY}, self.send())
        again = idempotency.once(self.store, "t", KEY, {"a": 1}, self.send("second"))
        self.assertEqual(again, {"url": "https://example.test/first"})

    def test_the_same_key_with_different_arguments_is_an_error_and_sends_nothing(self):
        idempotency.once(self.store, "t", KEY, {"a": 1}, self.send())
        with self.assertRaises(LinearError) as caught:
            idempotency.once(self.store, "t", KEY, {"a": 2}, self.send("second"))
        self.assertEqual(
            str(caught.exception), f"idempotency_key {KEY!r} was used with different arguments; pass a new key"
        )
        self.assertEqual(self.sent, ["first"])

    def test_the_same_key_on_another_tool_is_an_error(self):
        idempotency.once(self.store, "t", KEY, {"a": 1}, self.send())
        with self.assertRaises(LinearError):
            idempotency.once(self.store, "u", KEY, {"a": 1}, self.send("second"))
        self.assertEqual(self.sent, ["first"])

    def test_a_key_that_is_still_reserved_is_an_error_and_sends_nothing(self):
        def reentrant():
            with self.assertRaises(LinearError) as caught:
                idempotency.once(self.store, "t", KEY, {"a": 1}, self.send("second"))
            self.assertEqual(
                str(caught.exception),
                f"a call with idempotency_key {KEY!r} is still running or did not finish; nothing was sent again",
            )
            return {"url": "https://example.test/outer"}

        self.assertEqual(
            idempotency.once(self.store, "t", KEY, {"a": 1}, reentrant), {"url": "https://example.test/outer"}
        )
        self.assertEqual(self.sent, [])

    def test_a_failed_send_is_not_cached(self):
        def boom():
            raise LinearError("Linear refused")

        with self.assertRaises(LinearError):
            idempotency.once(self.store, "t", KEY, {"a": 1}, boom)
        self.assertEqual(os.listdir(self.store), [])
        self.assertEqual(idempotency.once(self.store, "t", KEY, {"a": 1}, self.send()), {"url": "https://example.test/first"})

    def test_a_key_that_does_not_start_with_a_uuid_sends_nothing(self):
        for key in (None, "", "abc", "x" + KEY, 5):
            with self.subTest(key=key), self.assertRaises(LinearError):
                idempotency.once(self.store, "t", key, {"a": 1}, self.send())
        self.assertEqual(self.sent, [])

    def test_two_threads_with_one_key_send_one_request(self):
        outcomes = []
        barrier = threading.Barrier(2)

        def slow():
            self.sent.append("x")
            time.sleep(0.2)
            return {"url": "https://example.test/x"}

        def call():
            barrier.wait()
            try:
                outcomes.append(idempotency.once(self.store, "t", KEY, {"a": 1}, slow))
            except LinearError as error:
                outcomes.append(error)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.sent, ["x"])
        results = [o for o in outcomes if isinstance(o, dict)]
        self.assertEqual(results, [{"url": "https://example.test/x"}])

    def test_the_store_lives_under_hermes_home(self):
        self.assertEqual(
            idempotency.store_dir({"HERMES_HOME": "/h"}), "/h/plugin-data/linear-agent-session/idempotency"
        )
        self.assertIsNone(idempotency.store_dir({}))


if __name__ == "__main__":
    unittest.main()
