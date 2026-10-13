from __future__ import annotations

import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from tests.support import SCRIPTS

SCRIPT = SCRIPTS / "github-upload-image"
_loader = SourceFileLoader("github_upload_image", str(SCRIPT))
helper = module_from_spec(spec_from_loader("github_upload_image", _loader))
_loader.exec_module(helper)

REPO = "example-org/example-repo"
URL = "https://github.com/user-attachments/assets/123e4567-e89b-12d3-a456-426614174000"


def png(width: int, height: int = 1) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")


class FakeGateway:
    """A Unix socket that answers each request with the next canned reply and records what it got."""

    def __init__(self, path: Path, replies):
        self.requests, self.replies = [], list(replies)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                stream = conn.makefile("rwb")
                header = json.loads(stream.readline())
                body = stream.read(header["size"])
                self.requests.append((header, body))
                reply = self.replies.pop(0)
                if reply is not None:
                    stream.write(json.dumps(reply).encode() + b"\n")
                    stream.flush()
                stream.close()

    def close(self):
        self.server.close()


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.socket = self.dir / "u.sock"
        self.env = {helper.SOCKET_ENV: str(self.socket)}
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def gateway(self, replies):
        gateway = FakeGateway(self.socket, replies)
        self.addCleanup(gateway.close)
        return gateway

    def image(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def run_helper(self, *args, repo=REPO):
        out, err = io.StringIO(), io.StringIO()
        code = helper.main(
            [str(a) for a in args], self.env, out, err,
            clock=lambda: self.now, sleep=self.sleep, repository=lambda: repo,
        )
        return code, out.getvalue(), err.getvalue()

    def test_it_prints_one_tag_per_file_and_sends_the_bytes_not_the_path(self):
        gateway = self.gateway([{"ok": True, "url": URL}, {"ok": True, "url": URL + "?x"}])
        first, second = self.image("01-login.png", png(640)), self.image("02-error.png", png(1600))

        code, out, err = self.run_helper(first, second)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out.splitlines(), [
            f'<img src="{URL}" alt="01-login" width="640">',
            f'<img src="{URL}?x" alt="02-error" width="800">',
        ])
        self.assertEqual(
            [(header, body) for header, body in gateway.requests],
            [
                ({"op": "upload", "repo": REPO, "name": "01-login.png", "size": first.stat().st_size}, first.read_bytes()),
                ({"op": "upload", "repo": REPO, "name": "02-error.png", "size": second.stat().st_size}, second.read_bytes()),
            ],
        )

    def test_a_refusal_exits_1_with_the_gateways_message_and_keeps_the_tags_already_printed(self):
        self.gateway([
            {"ok": True, "url": URL},
            {"ok": False, "state": "refused", "message": "That repository is not on the upload allowlist."},
        ])
        good, bad = self.image("a.png", png(10)), self.image("b.png", png(10))

        code, out, err = self.run_helper(good, bad)

        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [f'<img src="{URL}" alt="a" width="10">'])
        self.assertEqual(err, f"error: {bad}: That repository is not on the upload allowlist.\n")

    def test_a_connection_that_closes_without_an_answer_is_a_failed_file(self):
        self.gateway([None])

        code, out, err = self.run_helper(self.image("a.png", png(10)))

        self.assertEqual((code, out), (1, ""))
        self.assertIn("closed the connection before it answered", err)

    def test_a_gateway_that_never_answers_exits_3_after_30_seconds_and_names_the_fallback(self):
        code, out, err = self.run_helper(self.image("a.png", png(10)))

        self.assertEqual((code, out), (3, ""))
        self.assertEqual(err, f"error: {helper.GATEWAY_DOWN}\n")
        self.assertIn("Attach the images to the Linear issue instead", err)
        self.assertGreaterEqual(sum(self.sleeps), 30)
        self.assertLess(sum(self.sleeps), 31)

    def test_a_gateway_that_comes_back_inside_the_wait_gets_the_upload(self):
        shot = self.image("a.png", png(10))
        started = []

        def sleep(seconds):
            self.sleep(seconds)
            if len(self.sleeps) == 10 and not started:
                started.append(self.gateway([{"ok": True, "url": URL}]))

        out, err = io.StringIO(), io.StringIO()
        code = helper.main([str(shot)], self.env, out, err, clock=lambda: self.now, sleep=sleep, repository=lambda: REPO)

        self.assertEqual(code, 0)
        self.assertIn(URL, out.getvalue())

    def test_a_gateway_that_goes_away_between_files_exits_3_and_keeps_the_first_tag(self):
        gateway = self.gateway([{"ok": True, "url": URL}])
        first, second = self.image("a.png", png(10)), self.image("b.png", png(10))
        out, err = io.StringIO(), io.StringIO()

        def sleep(seconds):
            self.sleep(seconds)

        original = helper.exchange

        def exchange_then_stop(*args):
            reply = original(*args)
            gateway.close()
            self.socket.unlink()
            return reply

        helper.exchange = exchange_then_stop
        self.addCleanup(setattr, helper, "exchange", original)
        code = helper.main([str(first), str(second)], self.env, out, err, clock=lambda: self.now, sleep=sleep, repository=lambda: REPO)

        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue().splitlines(), [f'<img src="{URL}" alt="a" width="10">'])

    def test_a_missing_file_exits_2_before_it_contacts_the_gateway(self):
        gateway = self.gateway([])

        code, out, err = self.run_helper(self.dir / "missing.png")

        self.assertEqual((code, out), (2, ""))
        self.assertIn("is not a file", err)
        self.assertEqual(gateway.requests, [])
        self.assertEqual(self.sleeps, [])

    def test_a_file_over_the_size_limit_exits_2_before_it_contacts_the_gateway(self):
        big = self.dir / "big.png"
        with big.open("wb") as handle:
            handle.truncate(helper.MAX_BYTES + 1)

        code, _, err = self.run_helper(big)

        self.assertEqual(code, 2)
        self.assertIn("larger than 10 MB", err)

    def test_it_exits_2_when_the_repository_cannot_be_told(self):
        code, _, err = self.run_helper(self.image("a.png", png(10)), repo=None)

        self.assertEqual(code, 2)
        self.assertIn("--repo OWNER/REPO", err)

    def test_the_alt_text_is_escaped(self):
        self.gateway([{"ok": True, "url": URL}])

        _, out, _ = self.run_helper(self.image('a"><b.png', png(10)))

        self.assertEqual(out.strip(), f'<img src="{URL}" alt="a&quot;&gt;&lt;b" width="10">')

    def test_the_default_socket_is_the_root_homes(self):
        self.assertEqual(helper.DEFAULT_SOCKET, Path.home() / ".hermes/factory-uploads.sock")

    def test_it_runs_as_a_script_with_python3_and_no_executable_bit(self):
        self.gateway([{"ok": True, "url": URL}])
        shot = self.image("a.png", png(10))

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", REPO, str(shot)],
            env={**os.environ, **self.env}, capture_output=True, text=True, check=False,
        )

        self.assertEqual((result.returncode, result.stdout), (0, f'<img src="{URL}" alt="a" width="10">\n'))
        self.assertFalse(os.access(SCRIPT, os.X_OK))


class ImageWidthTest(unittest.TestCase):
    def test_each_allowed_format_reports_its_width(self):
        gif = b"GIF89a" + struct.pack("<HH", 321, 5)
        jpeg = b"\xff\xd8\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00" + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 7, 654) + b"\x00" * 8
        webp_lossless = b"RIFF\x00\x00\x00\x00WEBPVP8L" + b"\x00" * 5 + struct.pack("<I", 99) + b"\x00" * 4
        webp_extended = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (413).to_bytes(3, "little") + b"\x00" * 3
        self.assertEqual(
            [helper.image_width(data) for data in (png(77), gif, jpeg, webp_lossless, webp_extended)],
            [77, 321, 654, 100, 414],
        )

    def test_an_unreadable_header_has_no_width(self):
        self.assertIsNone(helper.image_width(b"\xff\xd8\xff"))


if __name__ == "__main__":
    unittest.main()
