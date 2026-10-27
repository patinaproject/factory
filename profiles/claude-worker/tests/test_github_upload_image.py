from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from tests.support import REPO as FACTORY, SCRIPTS, FakeBroker

BUSY = {"ok": False, "state": "busy", "message": "The upload broker is busy. Try again in a moment."}

SCRIPT = SCRIPTS / "github-upload-image"
REPO = "example-org/example-repo"
URL = "https://github.com/user-attachments/assets/123e4567-e89b-12d3-a456-426614174000"


def png(width: int, height: int = 1) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")


def staged_helper(root: Path):
    """The helper as installed in a worker home under `root`, beside the plugin's broker client."""
    scripts = root / "profiles" / "claude-worker" / "scripts"
    plugin = root / "profiles" / "claude-worker" / "plugins" / "linear-agent-session"
    scripts.mkdir(parents=True)
    plugin.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / SCRIPT.name)
    (plugin / "broker_client.py").symlink_to(FACTORY / "plugins" / "linear-agent-session" / "broker_client.py")
    loader = SourceFileLoader("github_upload_image", str(scripts / SCRIPT.name))
    module = module_from_spec(spec_from_loader("github_upload_image", loader))
    loader.exec_module(module)
    return module


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()
        self.helper = staged_helper(self.dir / "hermes")
        self.socket = self.dir / "u.sock"
        self.env = {self.helper.SOCKET_ENV: str(self.socket)}
        self.sleeps = []

    def gateway(self, replies, path=None):
        gateway = FakeBroker(path or self.socket, upload=replies)
        self.addCleanup(gateway.close)
        return gateway

    def image(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def run_helper(self, *args, repo=REPO, sleep=None, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = self.helper.main(
            [str(a) for a in args], self.env if env is None else env, out, err,
            sleep=sleep or self.sleeps.append, repository=lambda: repo,
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
            list(zip(gateway.bodies, gateway.payloads)),
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
        self.assertEqual(
            err, f"error: {bad}: the gateway's broker refused upload: That repository is not on the upload allowlist.\n"
        )

    def test_a_connection_that_closes_without_an_answer_is_a_failed_file_and_is_not_sent_again(self):
        gateway = self.gateway([None])

        code, out, err = self.run_helper(self.image("a.png", png(10)))

        self.assertEqual((code, out), (1, ""))
        self.assertIn("closed the connection before it answered, so it may have acted on upload", err)
        self.assertEqual((gateway.requests, self.sleeps), (["upload"], []))

    def test_a_busy_gateway_is_asked_again_until_it_answers(self):
        gateway = self.gateway([BUSY, {"ok": True, "url": URL}])

        code, out, _ = self.run_helper(self.image("a.png", png(10)))

        self.assertEqual((code, out), (0, f'<img src="{URL}" alt="a" width="10">\n'))
        self.assertEqual(gateway.requests, ["upload", "upload"])
        self.assertEqual(self.sleeps, [0.25])

    def test_a_gateway_that_never_answers_exits_3_after_the_broker_clients_retries_and_names_the_fallback(self):
        code, out, err = self.run_helper(self.image("a.png", png(10)))

        self.assertEqual((code, out), (3, ""))
        self.assertEqual(err, f"error: {self.helper.GATEWAY_DOWN}\n")
        self.assertIn("Attach the images to the Linear issue instead", err)
        self.assertEqual(self.sleeps, [0.25, 0.5, 1.0, 2.0])

    def test_a_gateway_that_comes_back_inside_the_retries_gets_the_upload(self):
        def sleep(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) == 2:
                self.gateway([{"ok": True, "url": URL}])

        code, out, _ = self.run_helper(self.image("a.png", png(10)), sleep=sleep)

        self.assertEqual(code, 0)
        self.assertIn(URL, out)

    def test_a_gateway_that_goes_away_between_files_exits_3_and_keeps_the_first_tag(self):
        gateway = self.gateway([{"ok": True, "url": URL}])
        first, second = self.image("a.png", png(10)), self.image("b.png", png(10))
        self.run_helper(self.dir / "missing.png")
        client = sys.modules["broker_client"]
        original = client.request

        def request_then_stop(*args, **kwargs):
            reply = original(*args, **kwargs)
            gateway.close()
            return reply

        client.request = request_then_stop
        self.addCleanup(setattr, client, "request", original)
        code, out, _ = self.run_helper(first, second)

        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines(), [f'<img src="{URL}" alt="a" width="10">'])

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
            handle.truncate(self.helper.MAX_BYTES + 1)

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

    def test_without_a_socket_it_uploads_through_the_root_homes(self):
        gateway = self.gateway([{"ok": True, "url": URL}], path=self.dir / "hermes" / "factory-uploads.sock")

        code, _, _ = self.run_helper(self.image("a.png", png(10)), env={})

        self.assertEqual((code, gateway.requests), (0, ["upload"]))

    def test_it_runs_as_a_script_with_python3_and_no_executable_bit(self):
        self.gateway([{"ok": True, "url": URL}])
        shot = self.image("a.png", png(10))

        result = subprocess.run(
            [sys.executable, self.helper.__file__, "--repo", REPO, str(shot)],
            env={**os.environ, **self.env}, capture_output=True, text=True, check=False,
        )

        self.assertEqual((result.returncode, result.stdout), (0, f'<img src="{URL}" alt="a" width="10">\n'))
        self.assertFalse(os.access(SCRIPT, os.X_OK))


class ImageWidthTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.helper = staged_helper(Path(tmp.name))

    def test_each_allowed_format_reports_its_width(self):
        gif = b"GIF89a" + struct.pack("<HH", 321, 5)
        jpeg = b"\xff\xd8\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00" + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 7, 654) + b"\x00" * 8
        webp_lossless = b"RIFF\x00\x00\x00\x00WEBPVP8L" + b"\x00" * 5 + struct.pack("<I", 99) + b"\x00" * 4
        webp_extended = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (413).to_bytes(3, "little") + b"\x00" * 3
        self.assertEqual(
            [self.helper.image_width(data) for data in (png(77), gif, jpeg, webp_lossless, webp_extended)],
            [77, 321, 654, 100, 414],
        )

    def test_an_unreadable_header_has_no_width(self):
        self.assertIsNone(self.helper.image_width(b"\xff\xd8\xff"))


if __name__ == "__main__":
    unittest.main()
