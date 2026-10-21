from __future__ import annotations

import asyncio
import builtins
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import PNG, TOKEN, UUID_URL, FakeUploads, upload_token

from factory_uploads import broker as broker_module

REPO = "example-org/example-repo"
PROBES = ("ghp_ABCDEF123456", "github_pat_ANOTHER_fake1")


async def ask(path: Path, header: dict, body: bytes = b"") -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(json.dumps(header).encode() + b"\n" + body)
    await writer.drain()
    reply = json.loads(await reader.readline())
    writer.close()
    return reply


def upload_request(data: bytes = PNG, repo: str = REPO, name: str = "shot.png") -> tuple[dict, bytes]:
    return {"op": "upload", "repo": repo, "name": name, "size": len(data)}, data


class BrokerTestCase(unittest.IsolatedAsyncioTestCase):
    token: str | None = TOKEN

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.uploads = FakeUploads()
        self.uploads.__enter__()
        self.addCleanup(self.uploads.__exit__, None, None, None)
        self.now = 1_800_000_000.0
        self.lookups = []
        self.broker = self.make_broker()
        self.assertTrue(await self.broker.start())
        self.addAsyncCleanup(self.broker.stop)

    def make_broker(self, repositories=(REPO,)):
        def lookup(repo):
            self.lookups.append(repo)
            return 7

        return broker_module.Broker(
            self.home,
            list(repositories),
            lambda: self.token,
            resolve_repository_id=lookup,
            base_url=self.uploads.base,
            clock=lambda: self.now,
        )

    async def upload(self, *args, **kwargs) -> dict:
        header, body = upload_request(*args, **kwargs)
        return await ask(self.broker.socket_path, header, body)

    def state_files(self) -> dict[str, str]:
        return {path.name: path.read_text() for path in (self.home / "factory-uploads").iterdir()}


class UploadTest(BrokerTestCase):
    async def test_a_png_for_an_allowed_repository_returns_only_the_asset_url(self):
        reply = await self.upload()

        self.assertEqual(reply, {"ok": True, "url": UUID_URL})
        [request] = self.uploads.requests
        self.assertEqual(request["body"], PNG)
        self.assertEqual(request["headers"]["Authorization"], f"token {TOKEN}")
        self.assertIn("repository_id=7", request["path"])
        self.assertIn("name=shot.png", request["path"])

    async def test_the_socket_is_private_to_the_owner(self):
        self.assertEqual(stat.S_IMODE(self.broker.socket_path.stat().st_mode), 0o600)

    async def test_a_repository_outside_the_allowlist_is_refused_before_any_request(self):
        reply = await self.upload(repo="example-org/other")

        self.assertEqual(reply, {"ok": False, "state": "refused", "message": broker_module.NOT_ALLOWED})
        self.assertEqual(self.uploads.requests, [])
        self.assertEqual(self.lookups, [])

    async def test_an_empty_allowlist_refuses_every_repository(self):
        await self.broker.stop()
        self.broker = self.make_broker(repositories=())
        await self.broker.start()

        reply = await self.upload()

        self.assertEqual(reply["state"], "refused")
        self.assertEqual(self.uploads.requests, [])

    async def test_a_text_file_named_png_is_refused_before_any_request(self):
        reply = await self.upload(data=b"just some text, not an image", name="evil.png")

        self.assertEqual(reply["state"], "refused")
        self.assertEqual(reply["message"], "Only png, jpeg, gif, and webp images can be uploaded.")
        self.assertEqual(self.uploads.requests, [])
        self.assertEqual(self.lookups, [])

    async def test_a_refused_non_image_does_not_use_up_the_hourly_quota(self):
        for _ in range(broker_module.HOURLY_QUOTA):
            await self.upload(data=b"not an image")

        self.assertTrue((await self.upload())["ok"])

    async def test_a_malformed_request_is_refused_with_a_fixed_message(self):
        for header in (
            {"op": "upload", "repo": REPO, "name": "a.png", "size": 0},
            {"op": "upload", "repo": REPO, "name": "a.png", "size": upload_token.MAX_BYTES + 1},
            {"op": "upload", "repo": REPO, "name": "a.png", "size": True},
            {"op": "upload", "repo": REPO, "name": "", "size": 10},
            {"op": "upload", "repo": 5, "name": "a.png", "size": 10},
            {"op": "delete"},
        ):
            with self.subTest(header=header):
                reply = await ask(self.broker.socket_path, header)
                self.assertEqual(reply, {"ok": False, "state": "refused", "message": broker_module.BAD_REQUEST})
        self.assertEqual(self.uploads.requests, [])

    async def test_the_sixty_first_file_in_an_hour_is_refused_and_the_hour_resets(self):
        for _ in range(60):
            self.assertTrue((await self.upload())["ok"])

        refused = await self.upload()

        self.assertEqual(refused, {"ok": False, "state": "refused", "message": broker_module.OVER_QUOTA})
        self.assertEqual(len(self.uploads.requests), 60)
        self.now += 3601
        self.assertTrue((await self.upload())["ok"])

    async def test_a_missing_token_answers_unavailable_without_a_request(self):
        self.token = None

        reply = await self.upload()

        self.assertEqual(reply["state"], "unavailable")
        self.assertEqual(reply["message"], broker_module.FIXED_MESSAGES["unavailable"])
        self.assertEqual(self.uploads.requests, [])

    async def test_the_token_is_read_for_each_request(self):
        self.token = "github_pat_FIRST_fake"
        await self.upload()
        self.token = "github_pat_SECOND_fake"
        await self.upload()

        seen = [request["headers"]["Authorization"] for request in self.uploads.requests]
        self.assertEqual(seen, ["token github_pat_FIRST_fake", "token github_pat_SECOND_fake"])

    async def test_the_repository_id_is_looked_up_once_per_repository(self):
        await self.upload()
        await self.upload()

        self.assertEqual(self.lookups, [REPO])


class FailureTest(BrokerTestCase):
    async def test_github_failures_become_fixed_messages(self):
        cases = {401: "rejected", 404: "no-access", 422: "error", 500: "error"}
        for status, state in cases.items():
            with self.subTest(status=status):
                self.uploads.reply = (status, json.dumps({"message": f"echo {TOKEN} {PROBES[0]}"}), {})

                reply = await self.upload()

                self.assertEqual(reply, {"ok": False, "state": state, "message": broker_module.FIXED_MESSAGES[state]})

    async def test_the_token_never_appears_in_a_reply_an_error_or_a_state_file(self):
        replies = [await self.upload()]
        for status in (401, 404, 422, 429):
            self.uploads.reply = (status, json.dumps({"message": f"echo {TOKEN} {' '.join(PROBES)}"}), {})
            replies.append(await self.upload())
        replies.append(await self.upload(data=b"not an image"))
        replies.append(await self.upload(repo="example-org/other"))
        self.token = None
        replies.append(await self.upload())
        replies.append(await ask(self.broker.socket_path, {"op": "status"}))

        everything = json.dumps(replies) + "".join(self.state_files().values())
        for secret in (TOKEN, *PROBES):
            self.assertNotIn(secret, everything)
        self.assertIsNone(upload_token.TOKEN_SHAPE.search(everything))

    async def test_a_gateway_log_line_never_carries_the_token(self):
        self.uploads.reply = (422, json.dumps({"message": f"echo {TOKEN}"}), {})

        with self.assertLogs("factory_uploads.broker", level="WARNING") as logs:
            await self.upload()

        self.assertNotIn(TOKEN, "\n".join(logs.output))


class AuditTest(BrokerTestCase):
    async def test_each_upload_appends_one_private_line_without_the_token_or_the_bytes(self):
        await self.upload(name="first.png")
        await self.upload(repo="example-org/other", name="second.png")

        log = self.home / "factory-uploads" / "log.jsonl"
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        first, second = (json.loads(line) for line in log.read_text().splitlines())
        self.assertEqual(
            (first["repo"], first["name"], first["size"], first["state"], first["url"]),
            (REPO, "first.png", len(PNG), "ok", UUID_URL),
        )
        self.assertEqual(len(first["sha256"]), 64)
        self.assertIsInstance(first["pid"], int)
        self.assertEqual((second["state"], second["url"]), ("refused", None))
        self.assertNotIn(TOKEN, log.read_text())
        self.assertNotIn("PNG", log.read_text())


class StatusTest(BrokerTestCase):
    async def test_status_reports_booleans_and_dates_only(self):
        self.uploads.reply = (201, json.dumps({"url": UUID_URL}), {upload_token.EXPIRY_HEADER: "2027-01-01 00:00:00 UTC"})
        before = await ask(self.broker.socket_path, {"op": "status"})
        await self.upload()

        after = await ask(self.broker.socket_path, {"op": "status"})

        self.assertIsNone(before["last_state"])
        self.assertEqual(
            {key: after[key] for key in ("ok", "token_present", "last_state", "expires_at")},
            {"ok": True, "token_present": True, "last_state": "ok", "expires_at": "2027-01-01 00:00:00 UTC"},
        )
        self.assertEqual(
            set(after),
            {
                "ok", "token_present", "linear_ready", "proxy_token_present", "sentry_token_present",
                "last_state", "expires_at", "checked_at",
            },
        )

    async def test_status_says_when_the_token_is_missing(self):
        self.token = None

        self.assertFalse((await ask(self.broker.socket_path, {"op": "status"}))["token_present"])


class FakeCredentials:
    def __init__(self):
        self.linear = {"access_token": "lin_oauth_FAKE_access", "expires_at": 1_900_000_000.0}
        self.proxy = "proxy-FAKE-token"
        self.sentry = "sntryu_FAKE"
        self.failure = None

    def linear_ready(self):
        return self.linear is not None

    def linear_token(self):
        if self.failure:
            raise self.failure
        return self.linear

    def proxy_token(self):
        return self.proxy

    def sentry_token(self):
        return self.sentry


class CredentialTest(BrokerTestCase):
    async def asyncSetUp(self):
        self.credentials = FakeCredentials()
        await super().asyncSetUp()

    def make_broker(self, repositories=(REPO,)):
        return broker_module.Broker(
            self.home, list(repositories), lambda: self.token, base_url=self.uploads.base,
            clock=lambda: self.now, credentials=self.credentials,
        )

    async def test_each_credential_comes_back_with_its_expiry(self):
        self.assertEqual(
            await ask(self.broker.socket_path, {"op": "linear_token"}),
            {"ok": True, "token": "lin_oauth_FAKE_access", "expires_at": 1_900_000_000.0},
        )
        self.assertEqual(
            await ask(self.broker.socket_path, {"op": "proxy_token"}),
            {"ok": True, "token": "proxy-FAKE-token", "expires_at": None},
        )
        self.assertEqual(
            await ask(self.broker.socket_path, {"op": "sentry_token"}),
            {"ok": True, "token": "sntryu_FAKE", "expires_at": None},
        )

    async def test_a_missing_credential_answers_unavailable(self):
        self.credentials.linear = self.credentials.proxy = self.credentials.sentry = None

        for op in ("linear_token", "proxy_token", "sentry_token"):
            with self.subTest(op=op):
                reply = await ask(self.broker.socket_path, {"op": op})
                self.assertEqual(reply, {"ok": False, "state": "unavailable", "message": broker_module.CREDENTIAL_MESSAGES[op]})

    async def test_a_failed_linear_token_gets_a_fixed_reply(self):
        self.credentials.failure = RuntimeError("Linear is unreachable")

        with self.assertLogs("factory_uploads.broker", level="WARNING"):
            reply = await ask(self.broker.socket_path, {"op": "linear_token"})

        self.assertEqual(reply, {"ok": False, "state": "error", "message": broker_module.LINEAR_FAILED})

    async def test_the_upload_token_is_never_handed_out(self):
        replies = [await ask(self.broker.socket_path, {"op": op}) for op in ("linear_token", "proxy_token", "sentry_token", "status")]

        self.assertNotIn(TOKEN, json.dumps(replies))

    async def test_each_credential_request_is_audited_without_the_token(self):
        await ask(self.broker.socket_path, {"op": "linear_token"})
        self.credentials.proxy = None
        await ask(self.broker.socket_path, {"op": "proxy_token"})
        await ask(self.broker.socket_path, {"op": "sentry_token"})

        log = (self.home / "factory-uploads" / "log.jsonl").read_text()
        entries = [json.loads(line) for line in log.splitlines()]
        self.assertEqual([(e["op"], e["state"]) for e in entries], [
            ("linear_token", "ok"), ("proxy_token", "unavailable"), ("sentry_token", "ok"),
        ])
        self.assertIsInstance(entries[0]["pid"], int)
        self.assertNotIn("lin_oauth_FAKE_access", log)
        self.assertNotIn("proxy-FAKE-token", log)
        self.assertNotIn("sntryu_FAKE", log)

    async def test_status_reports_the_credentials_as_booleans(self):
        status = await ask(self.broker.socket_path, {"op": "status"})

        self.assertEqual(
            (status["linear_ready"], status["proxy_token_present"], status["sentry_token_present"]), (True, True, True)
        )
        self.assertNotIn("proxy-FAKE-token", json.dumps(status))
        self.assertNotIn("sntryu_FAKE", json.dumps(status))
        self.credentials.sentry = None
        self.assertFalse((await ask(self.broker.socket_path, {"op": "status"}))["sentry_token_present"])


class LifecycleTest(BrokerTestCase):
    async def test_stop_removes_the_socket_and_start_listens_again(self):
        await self.broker.stop()
        self.assertFalse(self.broker.socket_path.exists())

        self.assertTrue(await self.broker.start())
        self.assertTrue((await self.upload())["ok"])

    async def test_a_second_broker_refuses_while_one_listens(self):
        second = self.make_broker()

        self.assertFalse(await second.start())
        self.assertTrue((await self.upload())["ok"])

    async def test_a_stale_socket_file_is_replaced(self):
        await self.broker.stop()
        self.broker.socket_path.write_text("stale")

        self.assertTrue(await self.broker.start())
        self.assertTrue((await self.upload())["ok"])


if __name__ == "__main__":
    unittest.main()


class ConnectionCapTest(BrokerTestCase):
    async def idle_clients(self, count: int, header: dict | None = None):
        clients = [await asyncio.open_unix_connection(str(self.broker.socket_path)) for _ in range(count)]
        if header is not None:
            for _, writer in clients:
                writer.write(json.dumps(header).encode() + b"\n")
                await writer.drain()
        await asyncio.sleep(0.1)
        return clients

    async def test_uploads_past_their_cap_are_busy_while_credential_requests_still_answer(self):
        self.assertLessEqual(broker_module.MAX_CONNECTIONS, 8)
        header, _ = upload_request()
        clients = await self.idle_clients(broker_module.MAX_CONNECTIONS, header)

        self.assertEqual(await self.upload(), {"ok": False, "state": "busy", "message": broker_module.BUSY})
        self.assertTrue((await ask(self.broker.socket_path, {"op": "status"}))["ok"])
        self.assertEqual(self.uploads.requests, [])
        clients[0][1].close()
        await asyncio.sleep(0.1)
        self.assertTrue((await self.upload())["ok"])
        for _, writer in clients[1:]:
            writer.close()

    async def test_a_connection_past_the_total_cap_gets_a_fixed_refusal_and_a_freed_slot_works_again(self):
        total = broker_module.MAX_CONNECTIONS + broker_module.MAX_CREDENTIAL_CONNECTIONS
        self.assertLessEqual(total, 16)
        clients = await self.idle_clients(total)

        reply = await ask(self.broker.socket_path, {"op": "status"})

        self.assertEqual(reply, {"ok": False, "state": "busy", "message": broker_module.BUSY})
        clients[0][1].close()
        await asyncio.sleep(0.1)
        self.assertTrue((await ask(self.broker.socket_path, {"op": "status"}))["ok"])
        for _, writer in clients[1:]:
            writer.close()


class UnexpectedErrorTest(BrokerTestCase):
    async def broker_with(self, **overrides):
        await self.broker.stop()
        self.broker = broker_module.Broker(
            self.home, [REPO], lambda: self.token, resolve_repository_id=lambda repo: 7,
            base_url=self.uploads.base, **overrides,
        )
        self.assertTrue(await self.broker.start())
        self.addAsyncCleanup(self.broker.stop)

    async def test_an_unexpected_upload_error_is_audited_and_answered_with_a_fixed_message(self):
        def explode(*args):
            raise RuntimeError(f"boom {TOKEN}")

        await self.broker_with(upload=explode)

        with self.assertLogs("factory_uploads.broker", level="ERROR"):
            reply = await self.upload()

        self.assertEqual(reply, {"ok": False, "state": "error", "message": broker_module.FIXED_MESSAGES["error"]})
        [line] = (self.home / "factory-uploads" / "log.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(line)["state"], "error")
        self.assertNotIn(TOKEN, json.dumps(reply) + "".join(self.state_files().values()))

    async def test_an_error_outside_the_upload_still_gets_a_fixed_reply(self):
        with mock.patch.object(self.broker, "_status", side_effect=RuntimeError("boom")):
            with self.assertLogs("factory_uploads.broker", level="ERROR"):
                reply = await ask(self.broker.socket_path, {"op": "status"})

        self.assertEqual(reply, {"ok": False, "state": "error", "message": broker_module.FIXED_MESSAGES["error"]})


class StopTest(BrokerTestCase):
    async def test_stop_closes_open_clients_and_does_not_wait_for_them(self):
        reader, writer = await asyncio.open_unix_connection(str(self.broker.socket_path))
        await asyncio.sleep(0.1)

        await asyncio.wait_for(self.broker.stop(), 5)

        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
        writer.close()


class NamedPathTest(BrokerTestCase):
    async def test_a_path_named_in_a_request_is_never_opened(self):
        outside = self.home / "secret-image.png"
        outside.write_bytes(PNG + b"from the file")
        opened = []
        real_open, real_os_open = builtins.open, os.open

        def spy_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        def spy_os_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_os_open(path, *args, **kwargs)

        header, body = upload_request(name=str(outside))
        header.update({"path": str(outside), "file": str(outside), "source": str(outside)})
        with mock.patch.object(builtins, "open", spy_open), mock.patch.object(os, "open", spy_os_open):
            reply = await ask(self.broker.socket_path, header, body)

        self.assertTrue(reply["ok"])
        self.assertEqual(self.uploads.requests[0]["body"], PNG)
        self.assertFalse([path for path in opened if "secret-image" in path], opened)


class CallerTest(BrokerTestCase):

    allowed: frozenset = frozenset()

    async def asyncSetUp(self):
        self.credentials = FakeCredentials()
        self.run_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.run_dir.cleanup)
        await super().asyncSetUp()

    def make_broker(self, repositories=(REPO,)):
        return broker_module.Broker(
            self.home, list(repositories), lambda: self.token, base_url=self.uploads.base,
            clock=lambda: self.now, credentials=self.credentials,
            socket_path=Path(self.run_dir.name) / "broker.sock", state_dir=self.home / "state",
            socket_mode=0o660, allowed_uids=self.allowed,
        )

    def audit(self) -> list:
        return [json.loads(line) for line in (self.home / "state" / "log.jsonl").read_text().splitlines()]


class AllowedCallerTest(CallerTest):
    allowed = frozenset({os.getuid()})

    async def test_the_socket_is_at_the_named_path_with_the_named_mode(self):
        self.assertEqual(self.broker.socket_path, Path(self.run_dir.name) / "broker.sock")
        self.assertEqual(stat.S_IMODE(os.stat(self.broker.socket_path).st_mode), 0o660)
        self.assertFalse((self.home / "factory-uploads.sock").exists())

    async def test_an_allowed_uid_gets_its_credential_and_the_audit_lands_in_the_state_directory(self):
        reply = await ask(self.broker.socket_path, {"op": "proxy_token"})

        self.assertEqual(reply, {"ok": True, "token": "proxy-FAKE-token", "expires_at": None})
        self.assertEqual([(e["op"], e["state"]) for e in self.audit()], [("proxy_token", "ok")])


class RefusedCallerTest(CallerTest):
    allowed = frozenset({os.getuid() + 1})

    async def test_any_other_uid_is_refused_before_the_broker_reads_its_request(self):
        for header in ({"op": "status"}, {"op": "proxy_token"}, {"op": "linear_token"}, upload_request()[0]):
            with self.subTest(op=header["op"]):
                reply = await ask(self.broker.socket_path, header)
                self.assertEqual(reply, {"ok": False, "state": "refused", "message": broker_module.NOT_PERMITTED})

        self.assertEqual(self.uploads.requests, [])
        self.assertEqual({(e["op"], e["state"], e["uid"]) for e in self.audit()}, {("connect", "refused", os.getuid())})
        self.assertNotIn("proxy-FAKE-token", json.dumps(self.audit()))


class PeerUidTest(unittest.TestCase):
    def test_the_peer_uid_is_the_connecting_process_uid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen(1)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(path)
                    accepted, _ = server.accept()
                    with accepted:
                        self.assertEqual(broker_module.peer_uid(accepted), os.getuid())
