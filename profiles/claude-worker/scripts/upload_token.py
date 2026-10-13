"""Upload an evidence image to GitHub's attachment store with the operator's upload token.

The token is for uploads only. `send_with_token` is the one function that
attaches it to a request, and it refuses any URL outside ALLOWED_ENDPOINTS
before a request exists. Nothing in this module prints, logs, or returns the
token.

Run `python3 upload_token.py --probe --repo OWNER/REPO` in your own terminal to
check a token. It reads the token from GITHUB_UPLOAD_TOKEN, or from a hidden
prompt when that variable is unset.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import re
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

UPLOAD_BASE = "https://uploads.github.com"
UPLOAD_PATH = "/user-attachments/assets"
ALLOWED_ENDPOINTS = frozenset({UPLOAD_BASE + UPLOAD_PATH})
MAX_BYTES = 10 * 1024 * 1024
TIMEOUT = 60
EXPIRY_HEADER = "github-authentication-token-expiration"
URL_SHAPE = re.compile(
    r"^https://github\.com/user-attachments/assets/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TOKEN_SHAPE = re.compile(r"gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+")
WORKER_GH_DIR = Path.home() / ".hermes/profiles/claude-worker/gh"


class UploadError(Exception):
    """An upload that did not produce a URL. `state` feeds the status record."""

    def __init__(self, state: str, message: str, status: int | None = None):
        super().__init__(message)
        self.state = state
        self.status = status


def redact(text: str, token: str = "") -> str:
    if token:
        text = text.replace(token, "[redacted]")
    return TOKEN_SHAPE.sub("[redacted]", text)


def image_type(data: bytes) -> str:
    """The content type of an allowed image, from its first bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise UploadError("refused", "Only png, jpeg, gif, and webp images can be uploaded.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send_with_token(url: str, token: str, data: bytes, content_type: str):
    """The only place the token is attached to a request.

    Refuses a URL that is not an exact allowed endpoint, with its query
    removed, before any request is built. Does not follow redirects, so the
    token cannot be forwarded to another host.
    """
    parsed = urllib.parse.urlsplit(url)
    endpoint = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if endpoint not in ALLOWED_ENDPOINTS:
        raise UploadError("refused", "The upload token is sent only to the attachment upload endpoint.")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": content_type,
        },
    )
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT)


def upload(name: str, data: bytes, repository_id: int, token: str, base_url: str = UPLOAD_BASE):
    """Upload one image. Returns (url, expires_at or None, HTTP status). Raises UploadError."""
    if not data:
        raise UploadError("refused", "The image is empty.")
    if len(data) > MAX_BYTES:
        raise UploadError("refused", f"The image is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    kind = image_type(data)
    query = urllib.parse.urlencode(
        {"name": Path(name).name, "content_type": kind, "repository_id": repository_id}
    )
    url = f"{base_url}{UPLOAD_PATH}?{query}"
    try:
        with send_with_token(url, token, data, "application/octet-stream") as response:
            status = response.status
            body = response.read().decode("utf-8", "replace")
            expires_at = response.headers.get(EXPIRY_HEADER)
    except urllib.error.HTTPError as error:
        with error:
            raise _http_error(error, token) from None
    except (urllib.error.URLError, OSError) as error:
        raise UploadError("error", redact(f"The upload request failed: {error}", token)) from None
    try:
        found = json.loads(body).get("url", "")
    except (ValueError, AttributeError):
        found = ""
    if not URL_SHAPE.match(found):
        raise UploadError("error", f"GitHub answered HTTP {status} without an attachment URL.", status)
    return found, expires_at, status


def _http_error(error: urllib.error.HTTPError, token: str) -> UploadError:
    status = error.code
    if status == 401:
        return UploadError("rejected", "The upload token was rejected (expired or revoked).", status)
    if status == 404:
        return UploadError(
            "no-access",
            "The upload token lacks write on the repository (GitHub answers 404 when it cannot write).",
            status,
        )
    detail = redact(error.read(2000).decode("utf-8", "replace"), token)
    if status == 429:
        detail = f"Retry-After {error.headers.get('Retry-After', 'unknown')}: {detail}"
    return UploadError("error", f"GitHub answered HTTP {status}: {detail}", status)


def status_record(state: str, repo: str, expires_at: str | None, now: datetime.datetime | None = None) -> dict:
    """The token-free record a later check reads."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": state,
        "expires_at": expires_at,
        "repo": repo,
    }


def write_status(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle)
    os.chmod(path, 0o600)


def one_pixel_png() -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\xff\xff")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")


# The gateway runs this with every factory secret in its environment, so gh sees only these.
GH_ENV_NAMES = ("PATH", "HOME", "GH_CONFIG_DIR")


def repository_id(repo: str) -> int:
    """Look the id up as the GitHub App. gh gets only GH_ENV_NAMES, never the caller's secrets."""
    env = {k: os.environ[k] for k in GH_ENV_NAMES if k in os.environ}
    env.setdefault("GH_CONFIG_DIR", str(WORKER_GH_DIR))
    gh = Path.home() / ".local/bin/gh"
    result = subprocess.run(
        [str(gh), "api", f"repos/{repo}", "--jq", ".id"],
        env=env, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        raise UploadError("error", f"Could not look up the id of {repo} as the GitHub App.")
    return int(result.stdout.strip())


def probe(argv: list[str], env, prompt, base_url: str = UPLOAD_BASE, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="upload_token.py")
    parser.add_argument("--probe", action="store_true", required=True)
    parser.add_argument("--repo", help="OWNER/REPO; its id is looked up as the GitHub App")
    parser.add_argument("--repository-id", type=int)
    args = parser.parse_args(argv)
    if args.repository_id is None and not args.repo:
        parser.error("pass --repo OWNER/REPO or --repository-id N")
    token = env.get("GITHUB_UPLOAD_TOKEN") or prompt("Upload token (input hidden): ")
    if not token:
        print("error: no upload token given", file=out)
        return 1
    try:
        repo_id = args.repository_id if args.repository_id is not None else repository_id(args.repo)
        url, _, status = upload("probe.png", one_pixel_png(), repo_id, token, base_url)
    except UploadError as error:
        print(f"HTTP status: {error.status if error.status is not None else 'none'}", file=out)
        print(f"error: {redact(str(error), token)}", file=out)
        return 1
    print(f"HTTP status: {status}", file=out)
    print(f"url shape ok: {bool(URL_SHAPE.match(url))}", file=out)
    print(f"url: {url}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(probe(sys.argv[1:], os.environ, getpass.getpass))
