from __future__ import annotations

import contextlib
import io
import socketserver
import json
import stat
import sys
import tempfile
import threading
import unittest
import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from tests.support import PNG, TOKEN, UUID_URL, FakeUploads, upload_token, with_interpreter


def run_probe(argv, env, prompt=lambda _: "", base=None):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = upload_token.probe(argv, env, prompt, base, out)
    return code, out.getvalue(), err.getvalue()


class UploadTest(unittest.TestCase):
    def test_success_returns_the_attachment_url_and_sends_the_documented_request(self):
        with FakeUploads(headers={upload_token.EXPIRY_HEADER: "2027-01-01 00:00:00 UTC"}) as server:
            url, expires, status = upload_token.upload("shots/login.png", PNG, 384286360, TOKEN, server.base)
        self.assertEqual((url, expires, status), (UUID_URL, "2027-01-01 00:00:00 UTC", 201))
        [request] = server.requests
        self.assertEqual(
            request["path"],
            "/user-attachments/assets?name=login.png&content_type=image%2Fpng&repository_id=384286360",
        )
        self.assertEqual(request["headers"]["Authorization"], f"token {TOKEN}")
        self.assertEqual(request["headers"]["Content-Type"], "application/octet-stream")
        self.assertEqual(request["body"], PNG)

    def test_404_says_the_token_lacks_write(self):
        with FakeUploads(status=404, body={"message": "Not Found"}) as server:
            with self.assertRaises(upload_token.UploadError) as caught:
                upload_token.upload("a.png", PNG, 1, TOKEN, server.base)
        self.assertEqual(caught.exception.state, "no-access")
        self.assertEqual(
            str(caught.exception),
            "The upload token lacks write on the repository (GitHub answers 404 when it cannot write).",
        )

    def test_401_says_the_token_is_expired_or_revoked(self):
        with FakeUploads(status=401, body={"message": "Bad credentials"}) as server:
            with self.assertRaises(upload_token.UploadError) as caught:
                upload_token.upload("a.png", PNG, 1, TOKEN, server.base)
        self.assertEqual(caught.exception.state, "rejected")
        self.assertEqual(str(caught.exception), "The upload token was rejected (expired or revoked).")

    def test_error_body_that_echoes_the_token_is_redacted(self):
        body = {"message": f"bad {TOKEN} and ghp_ABCDEF123456 and ghs_xyz789"}
        with FakeUploads(status=422, body=body) as server:
            with self.assertRaises(upload_token.UploadError) as caught:
                upload_token.upload("a.png", PNG, 1, TOKEN, server.base)
        self.assertEqual(
            str(caught.exception),
            'GitHub answered HTTP 422: {"message": "bad [redacted] and [redacted] and [redacted]"}',
        )

    def test_non_image_is_refused_before_any_request(self):
        with FakeUploads() as server:
            with self.assertRaises(upload_token.UploadError) as caught:
                upload_token.upload("a.png", b"<svg></svg>", 1, TOKEN, server.base)
        self.assertEqual(str(caught.exception), "Only png, jpeg, gif, and webp images can be uploaded.")
        self.assertEqual(server.requests, [])

    def test_oversize_and_empty_are_refused_before_any_request(self):
        big = PNG + b"\0" * upload_token.MAX_BYTES
        with FakeUploads() as server:
            with self.assertRaises(upload_token.UploadError) as caught:
                upload_token.upload("a.png", big, 1, TOKEN, server.base)
            self.assertEqual(str(caught.exception), "The image is larger than 10 MB.")
            with self.assertRaises(upload_token.UploadError) as caught:
                upload_token.upload("a.png", b"", 1, TOKEN, server.base)
            self.assertEqual(str(caught.exception), "The image is empty.")
        self.assertEqual(server.requests, [])

    def test_each_allowed_image_type_is_recognized(self):
        self.assertEqual(upload_token.image_type(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(upload_token.image_type(b"GIF89a rest"), "image/gif")
        self.assertEqual(upload_token.image_type(b"RIFF\0\0\0\0WEBPrest"), "image/webp")

    def test_a_url_outside_the_allowlist_is_refused_before_any_request(self):
        with FakeUploads() as server:
            for url in (
                server.base + "/repos/o/r",
                "https://api.github.com/repos/o/r",
                "https://uploads.github.com/user-attachments/assets/extra",
                server.base.replace("http://", "https://") + upload_token.UPLOAD_PATH,
            ):
                with self.assertRaises(upload_token.UploadError) as caught:
                    upload_token.send_with_token(url, TOKEN, b"x", "application/octet-stream")
                self.assertEqual(caught.exception.state, "refused", url)
            self.assertEqual(server.requests, [])

    def test_production_allowlist_is_only_the_upload_endpoint(self):
        self.assertEqual(
            upload_token.ALLOWED_ENDPOINTS, {"https://uploads.github.com/user-attachments/assets"}
        )

    def test_a_lookalike_of_the_production_endpoint_is_refused_before_any_request(self):
        with mock.patch.object(upload_token.urllib.request, "build_opener", side_effect=AssertionError("request built")):
            for url in (
                "https://uploads.github.com/user-attachments/assets/extra",
                "https://uploads.github.com/user-attachments/assetsX",
                "https://uploads.github.com.evil.com/user-attachments/assets",
                "https://uploads.github.com@evil.com/user-attachments/assets",
            ):
                with self.assertRaises(upload_token.UploadError) as caught:
                    upload_token.send_with_token(url, TOKEN, b"x", "application/octet-stream")
                self.assertEqual(caught.exception.state, "refused", url)

    def test_a_redirect_is_not_followed_so_the_token_goes_nowhere_else(self):
        for status in (302, 303, 307):
            with self.subTest(status=status), FakeUploads() as target:
                with FakeUploads(status=status, headers={"Location": target.base + "/stolen"}) as server:
                    with self.assertRaises(upload_token.UploadError):
                        upload_token.upload("a.png", PNG, 1, TOKEN, server.base)
                    self.assertEqual(len(server.requests), 1)
                self.assertEqual(target.requests, [])

    def test_a_reply_without_an_attachment_url_is_an_error(self):
        with FakeUploads(body={"url": "https://example.com/x.png"}) as server:
            with self.assertRaises(upload_token.UploadError) as caught:
                upload_token.upload("a.png", PNG, 1, TOKEN, server.base)
        self.assertEqual(str(caught.exception), "GitHub answered HTTP 201 without an attachment URL.")


class StatusRecordTest(unittest.TestCase):
    def test_record_has_only_the_token_free_fields(self):
        now = datetime.datetime(2026, 10, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
        record = upload_token.status_record("ok", "o/r", "2027-01-01", now)
        self.assertEqual(
            record,
            {"checked_at": "2026-10-13T12:00:00Z", "state": "ok", "expires_at": "2027-01-01", "repo": "o/r"},
        )

    def test_written_file_is_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "upload-token.json"
            upload_token.write_status(path, upload_token.status_record("ok", "o/r", None))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text())["state"], "ok")


class ProbeTest(unittest.TestCase):
    def test_success_prints_status_shape_and_url_only(self):
        with FakeUploads() as server:
            code, out, err = run_probe(
                ["--probe", "--repository-id", "7"], {"GITHUB_UPLOAD_TOKEN": TOKEN}, base=server.base
            )
        self.assertEqual(code, 0)
        self.assertEqual(out, f"HTTP status: 201\nurl shape ok: True\nurl: {UUID_URL}\n")
        self.assertNotIn(TOKEN, out + err)

    def test_prompt_is_used_when_the_variable_is_unset(self):
        with FakeUploads() as server:
            code, out, _ = run_probe(
                ["--probe", "--repository-id", "7"], {}, prompt=lambda _: TOKEN, base=server.base
            )
            self.assertEqual(server.requests[0]["headers"]["Authorization"], f"token {TOKEN}")
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, out)

    def test_failure_prints_status_and_a_redacted_body_without_the_token(self):
        body = {"message": f"echo {TOKEN}"}
        with FakeUploads(status=422, body=body) as server:
            code, out, err = run_probe(
                ["--probe", "--repository-id", "7"], {"GITHUB_UPLOAD_TOKEN": TOKEN}, base=server.base
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            out, 'HTTP status: 422\nerror: GitHub answered HTTP 422: {"message": "echo [redacted]"}\n'
        )
        self.assertNotIn(TOKEN, out + err)

    def test_404_prints_the_lacks_write_message(self):
        with FakeUploads(status=404) as server:
            code, out, _ = run_probe(
                ["--probe", "--repository-id", "7"], {"GITHUB_UPLOAD_TOKEN": TOKEN}, base=server.base
            )
        self.assertEqual(code, 1)
        self.assertIn("HTTP status: 404\nerror: The upload token lacks write on the repository", out)

    def test_repo_lookup_runs_without_the_upload_token_in_the_environment(self):
        seen = {}

        def fake_run(cmd, env, **kwargs):
            seen.update(cmd=cmd, env=env)
            return mock.Mock(returncode=0, stdout="384286360\n")

        with mock.patch.dict("os.environ", {"GITHUB_UPLOAD_TOKEN": TOKEN}):
            with mock.patch.object(upload_token.subprocess, "run", fake_run):
                self.assertEqual(upload_token.repository_id("o/r"), 384286360)
        self.assertNotIn("GITHUB_UPLOAD_TOKEN", seen["env"])
        self.assertEqual(seen["cmd"][1:], ["api", "repos/o/r", "--jq", ".id"])

    def run_repository_id_under_fake_gh(self, env):
        fake = "#!/usr/bin/env python3\nimport json, os, sys\nopen(sys.argv[0] + '.env', 'w').write(json.dumps(dict(os.environ)))\nprint(7)\n"
        with tempfile.TemporaryDirectory() as home:
            gh = Path(home) / ".local/bin/gh"
            gh.parent.mkdir(parents=True)
            gh.write_text(with_interpreter(fake))
            gh.chmod(0o755)
            with mock.patch.dict("os.environ", {"HOME": home, **env}, clear=True):
                self.assertEqual(upload_token.repository_id("o/r"), 7)
            return json.loads(Path(str(gh) + ".env").read_text())

    def test_gh_gets_only_the_names_it_needs_to_act_as_the_app(self):
        seen = self.run_repository_id_under_fake_gh({
            "PATH": "/usr/bin:/bin", "GH_CONFIG_DIR": "/gh",
            "LINEAR_CLIENT_SECRET": "x", "SLACK_BOT_TOKEN": "x",
            "GITHUB_UPLOAD_TOKEN": TOKEN, "OP_SERVICE_ACCOUNT_TOKEN": "x",
        })
        interpreter_added = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
        self.assertEqual(sorted(set(seen) - interpreter_added), ["GH_CONFIG_DIR", "HOME", "PATH"])
        self.assertEqual(seen["GH_CONFIG_DIR"], "/gh")

    def test_gh_falls_back_to_the_worker_gh_dir_never_a_personal_login(self):
        seen = self.run_repository_id_under_fake_gh({"PATH": "/usr/bin:/bin"})
        self.assertEqual(seen["GH_CONFIG_DIR"], str(upload_token.WORKER_GH_DIR))

    def test_the_cli_requires_the_probe_flag_and_a_repository(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                run_probe(["--repository-id", "7"], {"GITHUB_UPLOAD_TOKEN": TOKEN})
            with self.assertRaises(SystemExit):
                run_probe(["--probe"], {"GITHUB_UPLOAD_TOKEN": TOKEN})


if __name__ == "__main__":
    unittest.main()
