from __future__ import annotations

import hashlib
import hmac
import importlib.machinery
import importlib.util
import io
import json
import plistlib
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "watch-gateway"
REPO = SCRIPT.parents[3]
sys.path.insert(0, str(REPO / "plugins" / "linear-agent-session" / "tests"))
from fake_broker import FakeBroker  # noqa: E402
loader = importlib.machinery.SourceFileLoader("watch_gateway", str(SCRIPT))
watch_gateway = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
sys.modules[loader.name] = watch_gateway
loader.exec_module(watch_gateway)

REAL = {"linear": "real-linear-secret", "github": "real-github-secret", "factory-linear": "real-linear-secret"}
LITERAL = {
    "linear": "${LINEAR_WEBHOOK_SECRET}",
    "factory-linear": "${LINEAR_WEBHOOK_SECRET}",
    "github": "${GITHUB_WEBHOOK_SECRET}",
}
LISTED = {
    "linear": ["AgentSessionEvent", "Issue"],
    "factory-linear": ["AgentSessionEvent", "Issue"],
    "github": ["pull_request", "issue_comment", "check_run"],
}
HEARTBEAT_PATH = "/ping/9b1c-private-monitor-id"
CONFIG = """\
plugins:
  entries:
    linear-agent-session:
      settings:
        app_user_id: not-a-secret
        webhook_route: {route}
"""


class FakeGateway(ThreadingHTTPServer):
    """Answers a webhook delivery in the order Hermes does (gateway/platforms/webhook.py `_handle_webhook`): `401` for
    a bad signature, then `200 ignored` for an event outside the route's `events` list, then `202` for a listed
    event. A route signs with its real secret, or with the literal reference when its variable did not resolve."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), GatewayHandler)
        self.unresolved: set[str] = set()
        self.listed: dict[str, list[str]] = {name: list(events) for name, events in LISTED.items()}
        self.override: dict[str, int] = {}
        self.garbage = False
        self.deliveries: list[str] = []


class GatewayHandler(BaseHTTPRequestHandler):
    server: FakeGateway

    def log_message(self, *args) -> None:
        pass

    def reply(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        route = self.path.rsplit("/", 1)[1]
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.deliveries.append(route)
        if self.server.garbage:
            self.wfile.write(b"not http\r\n\r\n")
            return
        if route not in REAL:
            return self.reply(404)
        header = "X-Hub-Signature-256" if route == "github" else "Linear-Signature"
        secret = LITERAL[route] if route in self.server.unresolved else REAL[route]
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        want = f"sha256={digest}" if route == "github" else digest
        if self.headers.get(header) != want:
            return self.reply(401)
        if route in self.server.override:
            return self.reply(self.server.override[route])
        event = self.headers.get("X-GitHub-Event") or json.loads(body).get("type", "unknown")
        if event not in self.server.listed[route]:
            return self.reply(200)
        self.reply(202)


class Heartbeat(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), HeartbeatHandler)
        self.paths: list[str] = []
        self.status = 200
        self.garbage = False


class HeartbeatHandler(BaseHTTPRequestHandler):
    server: Heartbeat

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.server.paths.append(self.path)
        if self.server.garbage:
            self.wfile.write(b"not http\r\n\r\n")
            return
        self.send_response(self.server.status)
        self.send_header("Content-Length", "0")
        self.end_headers()


def holding(token_present: bool = True) -> list:
    return [{"ok": True, "token_present": token_present}]


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
        (self.root / "config.yaml").write_text(CONFIG.format(route="linear"))
        plugin = self.root / "plugins" / "linear-agent-session"
        plugin.mkdir(parents=True)
        (plugin / "broker_client.py").symlink_to(REPO / "plugins" / "linear-agent-session" / "broker_client.py")
        self.broker = FakeBroker(self.root / watch_gateway.UPLOAD_SOCKET, status=holding())
        self.addCleanup(self.broker.close)
        self.broker_config = self.root / "no-broker-user.json"
        self.lock = self.root / watch_gateway.LOCK_FILE
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
        target = watch_gateway.discover(self.root, self.port, self.broker_config, lock=None)
        watch_gateway.watch(self.root, target, self.restart, now=lambda: self.clock, out=out)
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

    def test_a_signed_probe_for_an_event_the_route_does_not_list_still_shows_an_unresolved_secret(self) -> None:
        self.gateway.unresolved = {"linear"}

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths, self.gateway.deliveries), (1, [], ["linear", "github"]))
        self.assertIn("route linear secret is its unresolved reference; restarting the gateway", output)

    def test_a_listed_event_that_the_gateway_accepts_shows_an_unresolved_secret_too(self) -> None:
        self.gateway.unresolved = {"github"}
        self.gateway.listed["github"].append("ping")

        self.tick()

        self.assertEqual(self.restarts, 1)

    def test_a_rate_limited_answer_means_the_signature_was_accepted(self) -> None:
        self.gateway.unresolved = {"github"}
        self.gateway.override = {"github": 429}

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (1, []))
        self.assertIn("route github secret is its unresolved reference", output)

    def test_a_resolved_secret_answers_401_whatever_the_route_lists(self) -> None:
        self.gateway.listed = {"linear": [], "github": ["ping"]}

        self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (0, [HEARTBEAT_PATH]))

    def test_a_missing_broker_token_alone_alerts_without_restarting_a_working_gateway(self) -> None:
        self.broker.answers["status"] = holding(False)

        outputs = [self.tick() for _ in range(5)]

        self.assertEqual((self.restarts, self.beat.paths), (0, []))
        self.assertIn("the upload broker holds no token; a restart cannot fix it, so the heartbeat stays quiet", outputs[0])
        self.assertEqual(self.state()["restarts"], [])

    def test_a_missing_broker_token_with_an_unresolved_route_secret_still_restarts_once(self) -> None:
        self.broker.answers["status"] = holding(False)
        self.gateway.unresolved = {"github"}

        self.tick()

        self.assertEqual(self.restarts, 1)

    def test_a_root_home_without_the_broker_client_is_neither_restarted_nor_pinged(self) -> None:
        (self.root / "plugins" / "linear-agent-session" / "broker_client.py").unlink()

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths, self.broker.requests), (0, [], []))
        self.assertIn("broker_client.py cannot be read, so the upload broker is not asked", output)

    def test_the_broker_user_socket_named_by_its_config_is_the_one_probed(self) -> None:
        self.broker.close()
        run = self.root / "run"
        run.mkdir()
        service = FakeBroker(run / "factory-uploads.sock", status=holding())
        self.addCleanup(service.close)
        self.broker_config.write_text(json.dumps({"socket": str(run / "factory-uploads.sock")}))

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths, output), (0, [HEARTBEAT_PATH], ""))

    def test_a_broker_user_without_a_token_is_never_restarted_for_it(self) -> None:
        self.broker.close()
        run = self.root / "run"
        run.mkdir()
        service = FakeBroker(run / "factory-uploads.sock", status=holding())
        service.answers["status"] = holding(False)
        self.addCleanup(service.close)
        self.broker_config.write_text(json.dumps({"socket": str(run / "factory-uploads.sock")}))

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (0, []))
        self.assertIn("the upload broker holds no token", output)

    def test_a_restart_that_applies_the_secrets_makes_the_next_check_healthy_without_a_person(self) -> None:
        self.gateway.unresolved = {"linear", "github"}
        self.broker.answers["status"] = holding(False)

        def apply_secrets() -> None:
            self.gateway.unresolved = set()
            self.broker.answers["status"] = holding(True)

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
        self.gateway.override = {"github": 500}
        self.gateway.unresolved = {"github"}

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (0, []))
        self.assertIn("unknown: route github answered 500, not 401", output)

    def test_a_listener_that_answers_something_other_than_http_is_down_and_the_state_is_still_written(self) -> None:
        self.gateway.garbage = True

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (0, []))
        self.assertIn("down: route linear does not answer", output)
        self.assertEqual(self.state()["state"], "down")

    def test_a_heartbeat_that_answers_something_other_than_http_is_named_and_the_state_is_still_written(self) -> None:
        self.beat.garbage = True

        output = self.tick()

        self.assertIn("the heartbeat ping failed: BadStatusLine", output)
        self.assertEqual(self.state()["state"], "healthy")

    def test_the_linear_route_is_the_one_settings_name(self) -> None:
        (self.root / "config.yaml").write_text(CONFIG.format(route="factory-linear"))

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths, output), (0, [HEARTBEAT_PATH], ""))
        self.assertEqual(sorted(self.gateway.deliveries), ["factory-linear", "github"])

    def test_a_missing_route_name_is_named_and_neither_restarts_nor_pings(self) -> None:
        (self.root / "config.yaml").write_text("plugins: {}\n")

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths), (0, []))
        self.assertIn("unknown: config.yaml names no webhook_route for the Linear route", output)

    def test_a_lock_holds_the_restart_back_without_spending_the_budget(self) -> None:
        self.gateway.unresolved = {"linear"}
        self.lock.write_text("deploy in progress\n")

        output = self.tick()

        self.assertEqual((self.restarts, self.beat.paths, self.state()["restarts"]), (0, [], []))
        self.assertIn("factory-gateway.lock exists, so the gateway is not restarted and the heartbeat stays quiet", output)

        self.lock.unlink()
        self.tick()

        self.assertEqual(self.restarts, 1)

    def test_a_held_hermes_gateway_lock_alone_does_not_hold_the_restart_back(self) -> None:
        self.gateway.unresolved = {"linear"}
        (self.root / "gateway.lock").write_text('{"kind": "hermes-gateway", "pid": 1}\n')

        self.tick()

        self.assertEqual(self.restarts, 1)

    def test_a_dangling_lock_link_also_holds_the_restart_back(self) -> None:
        self.gateway.unresolved = {"linear"}
        self.lock.symlink_to(self.root / "nowhere")

        self.tick()

        self.assertEqual(self.restarts, 0)

    def test_the_lock_defaults_to_one_file_in_the_root_home_and_a_named_lock_replaces_it(self) -> None:
        default = watch_gateway.discover(self.root, self.port, self.broker_config, lock=None)
        named = watch_gateway.discover(self.root, self.port, self.broker_config, lock=self.root / "elsewhere.lock")

        self.assertEqual((default.lock, named.lock), (self.root / "factory-gateway.lock", self.root / "elsewhere.lock"))

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

    def test_the_plist_names_a_lock_only_when_one_is_given(self) -> None:
        stdout = io.TextIOWrapper(io.BytesIO(), write_through=True)
        with mock.patch.object(sys, "stdout", stdout):
            watch_gateway.main(["--print-plist", "--root", str(self.root), "--lock", "/somewhere/deploy.lock"])
        job = plistlib.loads(stdout.buffer.getvalue())

        self.assertEqual(job["ProgramArguments"][-2:], ["--lock", "/somewhere/deploy.lock"])


if __name__ == "__main__":
    unittest.main()
