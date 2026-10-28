"""Mint the factory's GitHub App installation token from the App's private key.

The broker holds the key, so no process that Claude Code starts can read it.
The token is the only thing that leaves: the key and the JWT signed with it
never reach a reply or a log. It runs on the macOS system Python, 3.9.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

API_URL = "https://api.github.com"
OPENSSL = "/usr/bin/openssl"
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540
# The worker's refresh-gh-app-login job runs every 15 minutes and claude-session wants 15 more, so a token handed out must outlive both.
REFRESH_MARGIN = 35 * 60
TIMEOUT = 30


class GitHubAppError(Exception):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"{step}: {reason}")


def _numeric_id(value, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
        raise ValueError(f"github.{name} must be a numeric ID")
    return str(value)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_json(value: dict) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode())


class GitHubApp:
    def __init__(
        self, app_id, installation_id, key_path, *,
        api_url: str = API_URL, openssl: str = OPENSSL, clock: Callable[[], float] = time.time,
    ) -> None:
        self.app_id = _numeric_id(app_id, "app_id")
        self.installation_id = _numeric_id(installation_id, "installation_id")
        if not isinstance(key_path, str) or not os.path.isabs(key_path):
            raise ValueError("github.private_key_path must be an absolute path")
        self.key_path = Path(key_path)
        self._api_url = api_url.rstrip("/")
        self._openssl = openssl
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[dict] = None

    @classmethod
    def from_settings(cls, github, **kwargs) -> "Optional[GitHubApp]":
        """The App a `github` settings object names, or None when it names no private key."""
        if not isinstance(github, dict) or not github.get("private_key_path"):
            return None
        return cls(github.get("app_id"), github.get("installation_id"), github["private_key_path"], **kwargs)

    def ready(self) -> bool:
        return self.key_path.is_file()

    def installation_token(self) -> dict:
        """{"access_token", "expires_at", "permissions"}, minted again once it could expire before the next refresh. May block."""
        with self._lock:
            if self._token is None or self._token["expires_at"] - REFRESH_MARGIN <= self._clock():
                self._token = self._mint(self._jwt())
            return dict(self._token)

    def _jwt(self) -> str:
        if not self.ready():
            raise GitHubAppError("check private key", f"{self.key_path} does not exist")
        now = int(self._clock())
        claims = {"iat": now - JWT_BACKDATE_SECONDS, "exp": now + JWT_LIFETIME_SECONDS, "iss": self.app_id}
        signing_input = f"{_b64url_json({'alg': 'RS256', 'typ': 'JWT'})}.{_b64url_json(claims)}"
        try:
            proc = subprocess.run(
                [self._openssl, "dgst", "-sha256", "-sign", str(self.key_path)],
                input=signing_input.encode(), capture_output=True,
            )
        except OSError as error:
            raise GitHubAppError("sign JWT", str(error)) from None
        if proc.returncode != 0:
            raise GitHubAppError("sign JWT", f"openssl exited {proc.returncode}")
        return f"{signing_input}.{_b64url(proc.stdout)}"

    def _mint(self, jwt: str) -> dict:
        request = urllib.request.Request(
            f"{self._api_url}/app/installations/{self.installation_id}/access_tokens",
            method="POST",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            raise GitHubAppError("mint installation token", f"HTTP {error.code}: {_message(error)}") from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise GitHubAppError("mint installation token", str(error)) from None
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubAppError("mint installation token", "the response has no token")
        try:
            expires = datetime.strptime(body.get("expires_at"), "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            raise GitHubAppError("mint installation token", "the response has no expiry") from None
        granted = body.get("permissions")
        permissions = {
            key: level for key, level in (granted.items() if isinstance(granted, dict) else ())
            if isinstance(key, str) and isinstance(level, str)
        }
        return {
            "access_token": token,
            "expires_at": expires.replace(tzinfo=timezone.utc).timestamp(),
            "permissions": permissions,
        }


def _message(error: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(error.read())
    except ValueError:
        return "no message"
    finally:
        error.close()
    message = body.get("message") if isinstance(body, dict) else None
    return message if isinstance(message, str) else "no message"
