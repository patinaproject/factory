from __future__ import annotations

import hashlib
import hmac
import importlib.machinery
import importlib.util
import io
import json
import plistlib
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "watch-gateway"
loader = importlib.machinery.SourceFileLoader("watch_gateway", str(SCRIPT))
watch_gateway = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
sys.modules[loader.name] = watch_gateway
loader.exec_module(watch_gateway)

REAL = {"linear": "real-linear-secret", "github": "real-github-secret"}
LITERAL = {"linear": "${LINEAR_WEBHOOK_SECRET}", "github": "${GITHUB_WEBHOOK_SECRET}"}
HEARTBEAT_PATH = "/ping/9b1c-private-monitor-id"


class FakeGateway(ThreadingHTTPServer):
    """Answers a webhook delivery as Hermes does. A route signs with its real secret, or with the literal reference
    when its variable did not resolve, and the plugin then drops the delivery after a 202."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), GatewayHandler)
        self.unresolved: set[str] = set()
        self.override: dict[str, int] = {}
        self.deliveries: list[str] = []


class GatewayHandler(BaseHTTPRequestHandler):
    server: FakeGateway

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        route = self.path.rsplit("/", 1)[1]
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.deliveries.append(route)
        header = "Linear-Signature" if route == "linear" else "X-Hub-Signature-256"
        secret = LITERAL[route] if route in self.server.unresolved else REAL[route]
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        want = digest if route == "linear" else f"sha256={digest}"
        status = 202 if self.headers.get(header) == want else 401
        self.send_response(self.server.override.get(route, status))
        self.send_header("Content-Length", "0")
        self.end_headers()


class Heartbeat(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), HeartbeatHandler)
        self.paths: list[str] = []
        self.status = 200


class HeartbeatHandler(BaseHTTPRequestHandler):
    server: Heartbeat

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.server.paths.append(self.path)
        self.send_response(self.server.status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class FakeBroker:
    def __init__(self, path: Path) -> None:
        self.token_present = True
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client:
                client.recv(4096)
                reply = {"ok": True, "token_present": self.token_present}
                client.sendall(json.dumps(reply).encode() + b"\n")

    def close(self) -> None:
        self.server.close()


def serve(server: ThreadingHTTPServer) -> int:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


class WatchGatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.gateway = FakeGateway()
        self.port = serve(self.gateway)
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(self.gateway.shutdown)
        self.beat = Heartbeat()
        beat_port = serve(self.beat)
        self.addCleanup(self.beat.server_close)
        self.addCleanup(self.beat.shutdown)
        (self.root / watch_gateway.HEARTBEAT_FILE).write_text(f"http://127.0.0.1:{beat_port}{HEARTBEAT_PATH}\n")
        self.broker = FakeBroker(self.root / watch_gateway.UPLOAD_SOCKET)
        self.addCleanup(self.broker.close)
        self.clock = 1_000_000.0
        self.restarts = 0
        self.restart_effect = lambda: None

    def restart(self) -> int:
        self.restarts += 1
        self.restart_effect()
        return 0

    def tick(self, advance: float = 300) -> str:
        self.clock += advance
        out = io.StringIO()
        watch_gateway.watch(self.root, self.port, self.restart, now=lambda: self.clock, out=out)
        return out.getvalue()

    def state(self) -> dict:
        return json.loads((self.root / watch_gateway.STATE_FILE).read_text())

    def test_a_healthy_gateway_pings_the_heartbeat_and_restarts_nothing(self) -> None:
        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths, output), (0, [HEARTBEAT_PATH], ""))
        self.assertEqual(self.state()["state"], "healthy")

    def test_an_unresolved_route_secret_restarts_the_gateway_and_skips_the_ping(self) -> None:
        self.gateway.unresolved = {"github"}

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (1, []))
        self.assertIn("route github secret is its unresolved reference; restarting the gateway (1 of 3 this hour)", output)
        self.assertIn("restart-gateway exited 0", output)

    def test_a_missing_broker_token_restarts_the_gateway(self) -> None:
        self.broker.token_present = False

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (1, []))
        self.assertIn("the upload broker holds no token; restarting the gateway", output)

    def test_a_restart_that_applies_the_secrets_makes_the_next_check_healthy_without_a_person(self) -> None:
        self.gateway.unresolved = {"linear", "github"}
        self.broker.token_present = False

        def apply_secrets() -> None:
            self.gateway.unresolved = set()
            self.broker.token_present = True

        self.restart_effect = apply_secrets

        self.tick()
        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (1, [HEARTBEAT_PATH]))
        self.assertIn("healthy again", output)

    def test_three_restarts_in_an_hour_is_the_budget_and_the_heartbeat_stays_quiet(self) -> None:
        self.gateway.unresolved = {"linear"}

        outputs = [self.tick() for _ in range(4)]

        self.assertEqual((self.restarts, self.beat.paths), (3, []))
        self.assertIn("3 restarts in the last hour did not fix it, so the heartbeat stays quiet", outputs[3])
        self.assertEqual(len(self.state()["restarts"]), 3)

    def test_the_budget_refills_as_restarts_age_out_of_the_hour(self) -> None:
        self.gateway.unresolved = {"linear"}
        for _ in range(4):
            self.tick()

        self.tick(advance=3500)

        self.assertEqual(self.restarts, 4)

    def test_a_gateway_that_does_not_answer_is_not_restarted_and_earns_no_ping(self) -> None:
        self.gateway.shutdown()
        self.gateway.server_close()
        self.broker.close()

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (0, []))
        self.assertIn("down: route linear does not answer", output)

    def test_an_unexpected_status_is_neither_restarted_nor_pinged(self) -> None:
        self.gateway.override = {"github": 429}

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (0, []))
        self.assertIn("unknown: route github answered 429, not 401", output)

    def test_each_route_is_probed_once_per_check(self) -> None:
        self.tick()

        self.assertEqual(sorted(self.gateway.deliveries), ["github", "linear"])

    def test_no_heartbeat_file_is_named_and_does_not_stop_the_check(self) -> None:
        (self.root / watch_gateway.HEARTBEAT_FILE).unlink()

        output = self.tick()

        self.assertIn("holds no URL, so nothing alerts a person when it stops", output)
        self.assertEqual(self.state()["state"], "healthy")

    def test_a_failed_ping_names_the_reason_and_never_the_url(self) -> None:
        self.beat.status = 500
        failed = self.tick()
        self.beat.shutdown()
        self.beat.server_close()
        refused = self.tick()

        self.assertIn("the heartbeat ping failed: HTTP 500", failed)
        self.assertIn("the heartbeat ping failed: URLError", refused)
        for output in (failed, refused):
            self.assertNotIn("private-monitor-id", output)
            self.assertNotIn("127.0.0.1", output)

    def test_the_state_file_is_private_and_holds_no_url(self) -> None:
        self.tick()

        path = self.root / watch_gateway.STATE_FILE
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("private-monitor-id", path.read_text())

    def test_the_plist_runs_the_installed_script_every_five_minutes(self) -> None:
        stdout = io.TextIOWrapper(io.BytesIO(), write_through=True)
        with mock.patch.object(sys, "stdout", stdout):
            watch_gateway.main(["--print-plist", "--root", str(self.root), "--port", "8644"])
        job = plistlib.loads(stdout.buffer.getvalue())

        self.assertEqual(job["Label"], "ai.patinaproject.factory.gateway-watch")
        self.assertEqual(job["StartInterval"], 300)
        self.assertTrue(job["RunAtLoad"])
        self.assertEqual(
            job["ProgramArguments"],
            ["/usr/bin/python3", f"{self.root}/scripts/watch-gateway", "--root", str(self.root), "--port", "8644"],
        )


if __name__ == "__main__":
    unittest.main()
