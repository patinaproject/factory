from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from tests import BUSY, FakeBroker, load_plugin

broker_client = load_plugin().linear.broker_client

REPO = Path(__file__).resolve().parents[3]
REFUSED = {"ok": False, "state": "unavailable", "message": "The gateway holds no proxy token."}


class BrokerClientTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.worker = os.path.join(self.root, "profiles", "claude-worker")
        os.makedirs(self.worker)
        self.socket = os.path.join(self.root, "factory-uploads.sock")

    def broker(self, **answers):
        broker = FakeBroker(self.socket, **answers)
        self.addCleanup(broker.close)
        return broker

    def test_the_broker_lives_in_the_root_home(self):
        self.assertEqual(broker_client.socket_path({"HERMES_HOME": self.worker}), self.socket)
        self.assertEqual(broker_client.socket_path({"HERMES_HOME": self.root}), self.socket)

    def test_a_request_sends_its_op_and_fields_and_returns_the_reply(self):
        broker = self.broker(mcp_token=[{"ok": True, "token": "t1", "expires_at": None}])

        reply = broker_client.request(self.socket, "mcp_token", server="sentry")

        self.assertEqual(reply, {"ok": True, "token": "t1", "expires_at": None})
        self.assertEqual(broker.bodies, [{"op": "mcp_token", "server": "sentry"}])

    def test_a_busy_broker_is_asked_again_until_it_answers(self):
        broker = self.broker(proxy_token=[BUSY, BUSY, {"ok": True, "token": "t1"}])

        with mock.patch("time.sleep") as sleep:
            self.assertEqual(broker_client.request(self.socket, "proxy_token")["token"], "t1")

        self.assertEqual(broker.requests, ["proxy_token"] * 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.5])

    def test_a_broker_that_stays_busy_or_down_is_unavailable_after_a_bounded_wait(self):
        broker = self.broker(proxy_token=[BUSY])
        for case in ("busy", "down"):
            with self.subTest(case=case), mock.patch("time.sleep") as sleep:
                if case == "down":
                    broker.close()
                with self.assertRaises(broker_client.BrokerUnavailable):
                    broker_client.request(self.socket, "proxy_token")
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.5, 1.0, 2.0])
        self.assertEqual(len(broker.requests), 5)

    def test_a_broker_that_closes_without_an_answer_is_asked_again(self):
        broker = self.broker(proxy_token=[None, {"ok": True, "token": "t1"}])

        with mock.patch("time.sleep"):
            self.assertEqual(broker_client.request(self.socket, "proxy_token")["token"], "t1")

        self.assertEqual(len(broker.requests), 2)

    def test_a_broker_that_stalls_is_unavailable_without_a_retry(self):
        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(stalled.close)
        stalled.bind(self.socket)
        stalled.listen()

        with mock.patch("time.sleep") as sleep, self.assertRaisesRegex(broker_client.BrokerUnavailable, "did not answer in 0.2s"):
            broker_client.request(self.socket, "proxy_token", timeout=0.2)

        self.assertEqual(sleep.call_args_list, [])

    def test_a_refusal_is_not_retried_and_is_not_unavailability(self):
        broker = self.broker(proxy_token=[REFUSED])

        with mock.patch("time.sleep") as sleep, self.assertRaisesRegex(
            broker_client.BrokerError, "refused proxy_token: The gateway holds no proxy token."
        ) as raised:
            broker_client.request(self.socket, "proxy_token")

        self.assertNotIsInstance(raised.exception, broker_client.BrokerUnavailable)
        self.assertEqual((len(broker.requests), sleep.call_args_list), (1, []))

    def test_a_payload_follows_the_request_line(self):
        broker = self.broker(upload=[{"ok": True, "url": "https://example.test/a"}])

        reply = broker_client.request(self.socket, "upload", payload=b"bytes", repo="o/r", name="a.png", size=5)

        self.assertEqual(reply["url"], "https://example.test/a")
        self.assertEqual(
            (broker.bodies, broker.payloads), ([{"op": "upload", "repo": "o/r", "name": "a.png", "size": 5}], [b"bytes"])
        )

    def test_a_payload_whose_answer_is_lost_is_never_sent_again(self):
        broker = self.broker(upload=[None, {"ok": True, "url": "https://example.test/a"}])

        with mock.patch("time.sleep") as sleep, self.assertRaisesRegex(
            broker_client.BrokerError, "closed the connection before it answered, so it may have acted on upload"
        ) as raised:
            broker_client.request(self.socket, "upload", payload=b"bytes", repo="o/r", name="a.png", size=5)

        self.assertNotIsInstance(raised.exception, broker_client.BrokerUnavailable)
        self.assertEqual((len(broker.requests), sleep.call_args_list), (1, []))

    def test_status_reads_each_field_and_treats_anything_but_true_as_false(self):
        self.broker(status=[{
            "ok": True, "token_present": True, "linear_ready": "yes", "proxy_token_present": False,
            "mcp_tokens_present": {"sentry": True, "posthog": 1}, "github_app_ready": True,
            "last_state": "ok", "expires_at": "2026-01-01T00:00:00Z", "checked_at": 12.5,
        }])

        self.assertEqual(broker_client.status(self.socket), broker_client.Status(
            token_present=True, linear_ready=False, proxy_token_present=False,
            mcp_tokens_present={"sentry": True, "posthog": False}, github_app_ready=True,
            last_state="ok", expires_at="2026-01-01T00:00:00Z", checked_at=12.5,
        ))

    def test_status_of_a_broker_that_names_no_field_is_all_false(self):
        self.broker(status=[{"ok": True}])

        status = broker_client.status(self.socket)

        self.assertEqual(
            (status.token_present, status.linear_ready, status.mcp_tokens_present, status.last_state), (False, False, {}, None)
        )


class OneClientTest(unittest.TestCase):
    def test_only_the_broker_and_its_client_connect_to_a_unix_socket(self):
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout.decode()
        opening = sorted(
            name for name in filter(None, listing.split("\0"))
            if not name.startswith((".agents/", ".claude/", "node_modules/"))
            and "tests" not in PurePosixPath(name).parts
            and (REPO / name).is_file() and not (REPO / name).is_symlink()
            and any(call in (REPO / name).read_bytes() for call in (b"AF_UNIX", b"open_unix_connection"))
        )

        self.assertEqual(
            opening,
            [
                "plugins/factory-uploads/broker.py",
                "plugins/linear-agent-session/broker_client.py",
                "profiles/claude-worker/scripts/probe-sandbox",
            ],
        )


if __name__ == "__main__":
    unittest.main()
