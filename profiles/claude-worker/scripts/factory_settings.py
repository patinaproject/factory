"""The factory settings every worker script reads: where they live, how to read them, and which values refuse a run.

`validate` is pure. claude-session refuses a run with its errors, and check-install reports the same text, so a
value that would stop a worker is found before a card hits it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

SETTINGS_KEY = "plugins.entries.linear-agent-session.settings"
DEFAULT_FRESH_AFTER_TOKENS = 100_000
MODEL_TIERS = ("sonnet", "haiku", "fable")
MCP_SERVER_KEYS = frozenset({"url", "token_env", "scheme", "headers"})
# Fixed headers and the URL reach argv in --mcp-config, where ps shows them, so a name or value that could carry a secret is refused.
CREDENTIAL_NAME = re.compile(r"auth|key|token|secret|cookie|password|cred|session|sig|pass|pwd|jwt|bearer|access", re.IGNORECASE)
AUTHORIZATION_VALUE = re.compile(r"\s*(?:bearer|basic|token|sentry-bearer)(?:\s|$)", re.IGNORECASE)
# `sk-` alone is a language tag such as sk-SK, so it counts only with a token's worth of characters after it.
TOKEN_SHAPE = re.compile(
    r"\s*(?:(?:ghp_|gho_|ghs_|ghu_|github_pat_|glpat-|sntrys_|sntryu_|lin_api_|phx_|xoxb-|xoxp-)|sk-[A-Za-z0-9_-]{20})"
)


class SettingsError(Exception):
    pass


def find_hermes() -> str:
    # A cron job's PATH can lack the installer's bin directory, so fall back to where it puts hermes.
    candidates = [os.environ.get("HERMES_BIN"), shutil.which("hermes"), str(Path.home() / ".local" / "bin" / "hermes")]
    for candidate in filter(None, candidates):
        if os.access(candidate, os.X_OK):
            return candidate
    raise SettingsError("hermes is not on PATH or in ~/.local/bin; set HERMES_BIN")


def read(raw: bool, home: str | None = None) -> dict:
    """The factory settings from Hermes. Without `raw`, Hermes masks secrets."""
    proc = subprocess.run(
        [find_hermes(), "config", "get", SETTINGS_KEY, "--json", *(["--raw"] if raw else [])],
        env={**os.environ, "HERMES_HOME": home} if home else None, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SettingsError(f"hermes config get {SETTINGS_KEY} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def fresh_after_tokens(transport: dict) -> int:
    value = transport.get("fresh_after_tokens", DEFAULT_FRESH_AFTER_TOKENS)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SettingsError(f"claude_session.fresh_after_tokens {value!r} must be a positive integer")
    return value


def max_turns(transport: dict) -> int | None:
    """The turn limit, or None for no limit."""
    value = transport.get("max_turns")
    if value is None or value == 0:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SettingsError(f"claude_session.max_turns {value!r} must be a positive integer, or 0 for no limit")
    return value


def tier_models(transport: dict) -> dict:
    value = transport.get("tier_models")
    if value is None:
        return {}
    ok = isinstance(value, dict) and set(value) <= set(MODEL_TIERS) and all(
        isinstance(model, str) and model for model in value.values()
    )
    if not ok:
        raise SettingsError(
            f"claude_session.tier_models {value!r} must map {', '.join(MODEL_TIERS)} to non-empty model ids"
        )
    return value


def mcp_servers(transport: dict) -> dict:
    if "sentry_token_env" in transport:
        raise SettingsError("claude_session.sentry_token_env is replaced by claude_session.mcp_servers.sentry")
    servers = transport.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        raise SettingsError(f"claude_session.mcp_servers {servers!r} must map server names to servers")
    for name, server in servers.items():
        where = f"claude_session.mcp_servers.{name}"
        if name == "linear":
            raise SettingsError(f"{where}: the factory owns the linear server")
        if not isinstance(server, dict) or not set(server) <= MCP_SERVER_KEYS:
            raise SettingsError(f"{where} must be an object with only {', '.join(sorted(MCP_SERVER_KEYS))}")
        if not (isinstance(server.get("url"), str) and server["url"].startswith("https://")):
            raise SettingsError(f"{where}.url must be an https URL")
        if not (isinstance(server.get("token_env"), str) and server["token_env"]):
            raise SettingsError(f"{where}.token_env must name the gateway's variable that holds the token")
        scheme = server.get("scheme", "Bearer")
        if not (isinstance(scheme, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", scheme)):
            raise SettingsError(f"{where}.scheme must be one Authorization scheme word, such as Bearer")
        headers = server.get("headers", {})
        if not (isinstance(headers, dict) and all(isinstance(v, str) for v in headers.values())):
            raise SettingsError(f"{where}.headers must map header names to fixed strings")
        if any(CREDENTIAL_NAME.search(header) for header in headers):
            raise SettingsError(f"{where}.headers must hold no credential; mcp-headers sends the token as Authorization")
        url = urlsplit(server["url"])
        if "@" in url.netloc:
            raise SettingsError(f"{where}.url must hold no userinfo; mcp-headers sends the token as Authorization")
        query = parse_qsl(url.query, keep_blank_values=True)
        if any(CREDENTIAL_NAME.search(key) for key, _ in query):
            raise SettingsError(f"{where}.url must hold no credential in its query string; mcp-headers sends the token as Authorization")
        values = (*headers.values(), *(value for _, value in query))
        if any(AUTHORIZATION_VALUE.match(value) for value in values):
            raise SettingsError(f"{where} must hold no Authorization scheme in a header value or URL query value; mcp-headers sends the token")
        if any(TOKEN_SHAPE.match(value) for value in values):
            raise SettingsError(f"{where} must hold no known token prefix in a header value or URL query value; mcp-headers sends the token")
    return servers


def disabled_mcp_servers(transport: dict) -> list[str]:
    value = transport.get("disabled_mcp_servers") or []
    if not isinstance(value, list) or not all(isinstance(name, str) and name for name in value):
        raise SettingsError(f"claude_session.disabled_mcp_servers {value!r} must be a list of MCP server names")
    return value


def worker_entry(repo: dict) -> str:
    prefix = repo.get("worker_entry") or ""
    if "{" in prefix:
        raise SettingsError(
            f"worker_entry {prefix!r} must be a plain prefix such as /patina-mode; "
            "claude-session adds the issue, URL, repository, and branch itself"
        )
    return prefix


def toolchain_checks(repo: dict) -> list[str]:
    """The repository's toolchain commands. Only their shape is checked here; claude-session runs them."""
    checks = repo.get("toolchain_checks") or []
    if not isinstance(checks, list) or not all(isinstance(check, str) for check in checks):
        raise SettingsError(f"toolchain_checks {checks!r} must be a list of shell commands")
    return checks


GLOBAL_FIELDS = (fresh_after_tokens, max_turns, tier_models, mcp_servers, disabled_mcp_servers)
REPOSITORY_FIELDS = (worker_entry, toolchain_checks)


@dataclass(frozen=True)
class Problems:
    global_errors: tuple[str, ...]
    repository_errors: dict[str, tuple[str, ...]]

    def for_repository(self, full_name: str) -> tuple[str, ...]:
        """What refuses a run in this repository: every global error and the repository's own."""
        return (*self.global_errors, *self.repository_errors.get(full_name, ()))


def errors(fields: tuple, section: dict) -> tuple[str, ...]:
    found = []
    for field in fields:
        try:
            field(section)
        except SettingsError as error:
            found.append(str(error))
    return tuple(found)


def validate(settings: dict) -> Problems:
    """Every value that would make a worker run refuse, keyed by repository `full_name` where it is one repository's."""
    transport = settings.get("claude_session")
    repositories = settings.get("repositories") or []
    return Problems(
        errors(GLOBAL_FIELDS, transport if isinstance(transport, dict) else {}),
        {
            repo["full_name"]: found
            for repo in repositories
            if isinstance(repo, dict) and isinstance(repo.get("full_name"), str) and (found := errors(REPOSITORY_FIELDS, repo))
        },
    )


def token_variables(settings) -> tuple[str, dict]:
    """The gateway variable that holds the proxy token, and the one for each MCP server, as the settings name them."""
    transport = settings.get("claude_session") if isinstance(settings, dict) else None
    transport = transport if isinstance(transport, dict) else {}
    servers = transport.get("mcp_servers")
    named = {
        name: server["token_env"] for name, server in servers.items()
        if isinstance(server, dict) and isinstance(server.get("token_env"), str) and server["token_env"]
    } if isinstance(servers, dict) else {}
    return transport.get("auth_token_env") or "", named
