from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from typing import Callable

UPLOAD_TOKEN = "GITHUB_UPLOAD_TOKEN"
LINEAR_CREDENTIALS = ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET")
GATEWAY_ONLY = frozenset({
    *LINEAR_CREDENTIALS,
    UPLOAD_TOKEN,
    "FACTORY_INFISICAL_CLIENT_ID",
    "FACTORY_INFISICAL_CLIENT_SECRET",
    "BROKER_INFISICAL_CLIENT_ID",
    "BROKER_INFISICAL_CLIENT_SECRET",
    "LINEAR_WEBHOOK_SECRET",
    "GITHUB_WEBHOOK_SECRET",
})


def worker_variable(name) -> str:
    """A settings typo must not turn a token op into a way to read the upload token or an app credential."""
    return name if isinstance(name, str) and name not in GATEWAY_ONLY else ""


class GatewayCredentials:
    """Linear revokes every client credentials token of the app when a request asks for another scope set."""

    def __init__(self, get_secret: Callable[[str], "str | None"], transport: dict, linear_path: Path, cache_home: Path):
        self._get_secret = get_secret
        self._linear_path = Path(linear_path)
        self._cache_home = Path(cache_home)
        self._proxy_env = worker_variable(transport.get("auth_token_env"))
        self._sentry_env = worker_variable(transport.get("sentry_token_env"))
        self._lock = threading.Lock()
        self._client = None
        self._client_for = None

    def _app_credentials(self):
        values = tuple(self._get_secret(name) for name in LINEAR_CREDENTIALS)
        return values if all(values) else None

    def linear_ready(self) -> bool:
        return self._app_credentials() is not None

    def linear_token(self):
        credentials = self._app_credentials()
        if credentials is None:
            return None
        with self._lock:
            if self._client_for != credentials:
                linear = self._linear_module()
                cache = linear.token_cache_path({"HERMES_HOME": str(self._cache_home)})
                self._client = linear.LinearClient(*credentials, token_cache_path=cache)
                self._client_for = credentials
            return self._client.issue_token()

    def proxy_token(self):
        return self._get_secret(self._proxy_env) if self._proxy_env else None

    def sentry_token(self):
        return self._get_secret(self._sentry_env) if self._sentry_env else None

    def _linear_module(self):
        spec = importlib.util.spec_from_file_location("factory_uploads_linear", self._linear_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
