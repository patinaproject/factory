"""Linear GraphQL client for an agent app authenticated with client credentials."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Optional

API_URL = "https://api.linear.app/graphql"
TOKEN_URL = "https://api.linear.app/oauth/token"
# Every process must request the same scopes: Linear revokes all of an app's
# client credentials tokens when a request asks for a different scope set.
TOKEN_SCOPE = "read,write,app:assignable,app:mentionable"
TOKEN_REFRESH_MARGIN_SECONDS = 60

BODY_ACTIVITY_TYPES = ("thought", "response", "elicitation", "error")
ACTIVITY_TYPES = BODY_ACTIVITY_TYPES + ("action",)

ACTIVITY_MUTATION = """mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) { success agentActivity { id } }
}"""

SESSION_UPDATE_MUTATION = """mutation AgentSessionUpdate($id: String!, $input: AgentSessionUpdateInput!) {
  agentSessionUpdate(id: $id, input: $input) { success }
}"""

ISSUE_QUERY = """query Issue($id: String!) {
  issue(id: $id) {
    id identifier title url branchName priority
    state { name type }
    delegate { id }
    attachments { nodes { url sourceType } }
  }
}"""

UrlOpen = Callable[..., Any]


class LinearError(Exception):
    pass


def activity_content(
    type: str,
    body: Optional[str] = None,
    action: Optional[str] = None,
    parameter: Optional[str] = None,
    result: Optional[str] = None,
) -> dict:
    if type == "action":
        if not action or parameter is None:
            raise LinearError("an action activity needs action and parameter")
        content = {"type": "action", "action": action, "parameter": parameter}
        if result is not None:
            content["result"] = result
        return content
    if type in BODY_ACTIVITY_TYPES:
        if not body:
            raise LinearError(f"a {type} activity needs body")
        return {"type": type, "body": body}
    raise LinearError(f"unknown activity type {type!r}; expected one of {', '.join(ACTIVITY_TYPES)}")


class LinearClient:
    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        *,
        access_token: str = "",
        token_cache_path: Optional[str] = None,
        urlopen: Optional[UrlOpen] = None,
        clock: Callable[[], float] = time.time,
        timeout: float = 10.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._static_token = access_token
        self._token_cache_path = token_cache_path
        self._urlopen = urlopen or urllib.request.urlopen
        self._clock = clock
        self._timeout = timeout
        self._token: Optional[dict] = None

    def create_activity(self, agent_session_id: str, content: dict, *, ephemeral: bool = False) -> str:
        activity_input: dict = {"agentSessionId": agent_session_id, "content": content}
        if ephemeral:
            activity_input["ephemeral"] = True
        payload = self._graphql(ACTIVITY_MUTATION, {"input": activity_input})["agentActivityCreate"]
        if not payload["success"]:
            raise LinearError("agentActivityCreate reported success: false")
        return payload["agentActivity"]["id"]

    def set_external_urls(self, agent_session_id: str, urls: list) -> None:
        external_urls = [{"label": u["label"], "url": u["url"]} for u in urls]
        payload = self._graphql(
            SESSION_UPDATE_MUTATION,
            {"id": agent_session_id, "input": {"externalUrls": external_urls}},
        )["agentSessionUpdate"]
        if not payload["success"]:
            raise LinearError("agentSessionUpdate reported success: false")

    def get_issue(self, id_or_identifier: str) -> dict:
        issue = self._graphql(ISSUE_QUERY, {"id": id_or_identifier})["issue"]
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "url": issue["url"],
            "gitBranchName": issue["branchName"],
            "priority": int(issue["priority"]),
            "state": issue["state"]["name"],
            "stateType": issue["state"]["type"],
            "delegate": issue["delegate"]["id"] if issue["delegate"] else None,
            "attachments": [
                {"url": a["url"], "sourceType": a["sourceType"]} for a in issue["attachments"]["nodes"]
            ],
        }

    def _graphql(self, query: str, variables: dict) -> dict:
        try:
            return self._post_graphql(query, variables)
        except _Unauthorized:
            if self._static_token:
                raise LinearError("Linear rejected LINEAR_ACCESS_TOKEN (HTTP 401)") from None
            # Client credentials tokens have no refresh token; Linear's documented recovery is a new token.
            self._discard_token()
            return self._post_graphql(query, variables)

    def _post_graphql(self, query: str, variables: dict) -> dict:
        request = urllib.request.Request(
            API_URL,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._access_token()}"},
            method="POST",
        )
        status, body = self._send(request)
        if status == 401:
            raise _Unauthorized()
        document = _parse_json(body, status)
        if document.get("errors"):
            raise LinearError("; ".join(e.get("message", str(e)) for e in document["errors"]))
        if status >= 400 or not isinstance(document.get("data"), dict):
            raise LinearError(f"Linear API returned HTTP {status}")
        return document["data"]

    def _access_token(self) -> str:
        if self._static_token:
            return self._static_token
        if self._token is None:
            self._token = self._read_cached_token()
        if self._token is None or self._clock() >= self._token["expires_at"] - TOKEN_REFRESH_MARGIN_SECONDS:
            self._token = self._mint_token()
            self._write_cached_token(self._token)
        return self._token["access_token"]

    def _mint_token(self) -> dict:
        credentials = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": TOKEN_SCOPE}).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {credentials}",
            },
            method="POST",
        )
        status, body = self._send(request)
        document = _parse_json(body, status)
        if status >= 400 or "access_token" not in document:
            detail = document.get("error_description") or document.get("error") or f"HTTP {status}"
            raise LinearError(f"client credentials token request failed: {detail}")
        return {
            "client_id": self._client_id,
            "access_token": document["access_token"],
            "expires_at": self._clock() + float(document["expires_in"]),
        }

    def _discard_token(self) -> None:
        self._token = None
        if self._token_cache_path and os.path.exists(self._token_cache_path):
            os.remove(self._token_cache_path)

    def _read_cached_token(self) -> Optional[dict]:
        if not self._token_cache_path or not os.path.exists(self._token_cache_path):
            return None
        with open(self._token_cache_path) as f:
            token = json.load(f)
        return token if token.get("client_id") == self._client_id else None

    def _write_cached_token(self, token: dict) -> None:
        if not self._token_cache_path:
            return
        os.makedirs(os.path.dirname(self._token_cache_path) or ".", exist_ok=True)
        tmp = f"{self._token_cache_path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(token, f)
        os.replace(tmp, self._token_cache_path)

    def _send(self, request: urllib.request.Request) -> tuple:
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


class _Unauthorized(Exception):
    pass


def _parse_json(body: bytes, status: int) -> dict:
    try:
        document = json.loads(body or b"{}")
    except ValueError:
        raise LinearError(f"Linear returned non-JSON HTTP {status}") from None
    if not isinstance(document, dict):
        raise LinearError(f"Linear returned unexpected JSON with HTTP {status}")
    return document


def client_from_env(environ: Mapping[str, str] = os.environ, *, timeout: float = 10.0, **kwargs: Any) -> LinearClient:
    access_token = environ.get("LINEAR_ACCESS_TOKEN", "").strip()
    if access_token:
        return LinearClient(access_token=access_token, timeout=timeout, **kwargs)
    client_id = environ.get("LINEAR_CLIENT_ID")
    client_secret = environ.get("LINEAR_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise LinearError("set LINEAR_ACCESS_TOKEN, or LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET")
    return LinearClient(
        client_id,
        client_secret,
        token_cache_path=token_cache_path(environ),
        timeout=timeout,
        **kwargs,
    )


def token_cache_path(environ: Mapping[str, str]) -> Optional[str]:
    if environ.get("LINEAR_TOKEN_CACHE"):
        return environ["LINEAR_TOKEN_CACHE"]
    if environ.get("HERMES_HOME"):
        return os.path.join(environ["HERMES_HOME"], "plugin-data", "linear-agent-session", "token.json")
    return None
