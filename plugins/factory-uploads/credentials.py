from __future__ import annotations

import importlib.util
import re
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


# The pattern check-install uses on the same manifest.
DECLARED_NAME = re.compile(r"^\s*-\s*name:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", re.MULTILINE)


def servable_names(manifest: Path) -> frozenset:
    """The names the factory-secret-names manifest declares, less the gateway-only ones.

    A missing manifest raises, and so does one that leaves nothing servable: serving nothing
    silently would fail every card on the proxy token with no cause.
    """
    names = frozenset(DECLARED_NAME.findall(Path(manifest).read_text())) - GATEWAY_ONLY
    if not names:
        raise ValueError(f"{manifest} declares no servable secret name")
    return names


def worker_variable(name, servable: frozenset) -> str:
    """A settings typo must not turn a token op into a way to read a secret no worker may hold."""
    return name if isinstance(name, str) and name in servable else ""


class GatewayCredentials:
    """Linear revokes every client credentials token of the app when a request asks for another scope set."""

    def __init__(
        self, get_secret: Callable[[str], "str | None"], transport: dict, linear_path: Path, cache_home: Path, manifest: Path,
    ):
        self._get_secret = get_secret
        self._linear_path = Path(linear_path)
        self._cache_home = Path(cache_home)
        servable = servable_names(manifest)
        self._proxy_env = worker_variable(transport.get("auth_token_env"), servable)
        servers = transport.get("mcp_servers")
        self._mcp_envs = {
            name: worker_variable(spec.get("token_env"), servable)
            for name, spec in (servers.items() if isinstance(servers, dict) else ())
            if isinstance(spec, dict)
        }
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

    def mcp_servers(self):
        return sorted(self._mcp_envs)

    def mcp_token(self, server: str):
        name = self._mcp_envs.get(server)
        return self._get_secret(name) if name else None

    def _linear_module(self):
        spec = importlib.util.spec_from_file_location("factory_uploads_linear", self._linear_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
