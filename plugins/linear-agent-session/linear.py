from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
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
BROKER_SOCKET = "factory-uploads.sock"
BROKER_TIMEOUT_SECONDS = 30
BROKER_REPLY_LIMIT = 65536
# A busy or restarting broker gets a few seconds; a card waits on it far longer in the queue.
BROKER_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0)

BODY_ACTIVITY_TYPES = ("thought", "response", "elicitation", "error")
ACTIVITY_TYPES = BODY_ACTIVITY_TYPES + ("action",)

ACTIVITY_MUTATION = """mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) { success agentActivity { id } }
}"""

VIEWER_QUERY = "query { viewer { id } }"

SESSION_UPDATE_MUTATION = """mutation AgentSessionUpdate($id: String!, $input: AgentSessionUpdateInput!) {
  agentSessionUpdate(id: $id, input: $input) { success }
}"""

SESSION_THREAD_QUERY = """query AgentSessionThread($id: String!) {
  agentSession(id: $id) { comment { id } issue { id } }
}"""

ACTIVITY_FIELDS = "id createdAt signal content { __typename ... on AgentActivityPromptContent { body } }"

ACTIVITY_QUERY = f"""query AgentActivity($id: String!) {{
  agentActivity(id: $id) {{ {ACTIVITY_FIELDS} }}
}}"""

SESSION_ACTIVITIES_QUERY = f"""query AgentSessionActivities($id: String!) {{
  agentSession(id: $id) {{ activities(first: 50, orderBy: createdAt) {{ nodes {{ {ACTIVITY_FIELDS} }} }} }}
}}"""

COMMENT_CREATE_MUTATION = """mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) { success }
}"""

SESSION_CREATE_MUTATION = """mutation AgentSessionCreateOnIssue($input: AgentSessionCreateOnIssue!) {
  agentSessionCreateOnIssue(input: $input) { success agentSession { id } }
}"""

# Linear ranks Urgent 1 and Low 4; a card ranks higher numbers first.
PRIORITIES = {0: ("No priority", 0), 1: ("Urgent", 4), 2: ("High", 3), 3: ("Medium", 2), 4: ("Low", 1)}

ISSUE_QUERY = """query Issue($id: String!) {
  issue(id: $id) {
    id identifier title url branchName priority
    state { name type }
    delegate { id }
    labels { nodes { name } }
    attachments { nodes { url sourceType } }
    agentSessions(first: 20) { nodes { id status createdAt appUser { id } creator { id } } }
    inverseRelations(first: 50) { nodes { type issue { id identifier branchName state { type } delegate { id } } } }
  }
}"""

ISSUE_BLOCKERS_QUERY = """query IssueBlockers($id: String!) {
  viewer { id }
  issue(id: $id) { inverseRelations(first: 50) { nodes { type issue { id identifier branchName state { type } delegate { id } } } } }
}"""

DELEGATED_ISSUES_QUERY = """query DelegatedIssues($filter: IssueFilter!, $after: String) {
  issues(filter: $filter, first: 50, after: $after) {
    nodes {
      id identifier title url branchName
      state { name type }
      attachments { nodes { url sourceType } }
    }
    pageInfo { hasNextPage endCursor }
  }
}"""

ISSUE_DELEGATE_QUERY = """query IssueDelegate($id: String!) {
  viewer { id }
  issue(id: $id) { id identifier delegate { id } }
}"""

ISSUE_STATUS_QUERY = """query IssueStatus($id: String!) {
  issue(id: $id) { state { type } delegate { id } branchName }
}"""

ISSUE_LABELS_QUERY = """query IssueLabels($id: String!) {
  issue(id: $id) { labels { nodes { name } } }
}"""

ISSUE_DOCUMENTS_QUERY = """query IssueDocuments($id: String!) {
  viewer { id }
  issue(id: $id) { identifier documents(first: 100) { nodes { id title creator { id } } } }
}"""

TRIAGE_PEOPLE_QUERY = """query TriagePeople($id: String!) {
  issue(id: $id) {
    assignee { url }
    creator { url app }
    team { triageResponsibility { currentUser { url } } }
  }
}"""

DOCUMENT_UPDATE_MUTATION = """mutation DocumentUpdate($id: String!, $input: DocumentUpdateInput!) {
  documentUpdate(id: $id, input: $input) { success }
}"""

ISSUE_STATES_QUERY = """query IssueStates($id: String!) {
  issue(id: $id) {
    id identifier
    state { name type }
    team { states { nodes { id name type position } } }
  }
}"""

ISSUE_UPDATE_MUTATION = """mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success }
}"""

CLOSED_STATE_TYPES = ("completed", "canceled")
READY_STATE_NAME = "Todo"

UrlOpen = Callable[..., Any]


class LinearError(Exception):
    pass


class BrokerUnavailable(LinearError):
    """The broker was busy, unreachable, or slow, which a later run can outlast."""

    def __init__(self, message: str, *, retry: bool = True) -> None:
        super().__init__(message)
        self.retry = retry


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
        client_id: str,
        client_secret: str,
        *,
        token_cache_path: Optional[str] = None,
        urlopen: Optional[UrlOpen] = None,
        clock: Callable[[], float] = time.time,
        timeout: float = 10.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
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

    def reply_in_session(self, agent_session_id: str, body: str) -> None:
        """Reply in the session's comment thread. Linear turns a mention in a comment into one that notifies,
        but leaves a mention in an activity body as plain text or a link."""
        session = self._graphql(SESSION_THREAD_QUERY, {"id": agent_session_id})["agentSession"]
        payload = self._graphql(
            COMMENT_CREATE_MUTATION,
            {"input": {"issueId": session["issue"]["id"], "parentId": session["comment"]["id"], "body": body}},
        )["commentCreate"]
        if not payload["success"]:
            raise LinearError("commentCreate reported success: false")

    def read_prompt(self, *, agent_activity_id: Optional[str] = None, agent_session_id: Optional[str] = None) -> dict:
        """A person's prompt by its activity id, or else the newest prompt in the session that is not a stop."""
        if agent_activity_id:
            prompts = [self._graphql(ACTIVITY_QUERY, {"id": agent_activity_id})["agentActivity"]]
        else:
            session = self._graphql(SESSION_ACTIVITIES_QUERY, {"id": agent_session_id})["agentSession"]
            prompts = sorted(session["activities"]["nodes"], key=lambda n: n["createdAt"], reverse=True)
        for node in prompts:
            if node and node["content"]["__typename"] == "AgentActivityPromptContent" and node["signal"] != "stop":
                return {
                    "found": True,
                    "id": node["id"],
                    "createdAt": node["createdAt"],
                    "signal": node["signal"],
                    "body": node["content"]["body"],
                }
        return {"found": False}

    def create_session_on_issue(self, id_or_identifier: str) -> str:
        payload = self._graphql(SESSION_CREATE_MUTATION, {"input": {"issueId": id_or_identifier}})[
            "agentSessionCreateOnIssue"
        ]
        if not payload["success"]:
            raise LinearError("agentSessionCreateOnIssue reported success: false")
        return payload["agentSession"]["id"]

    def get_issue(self, id_or_identifier: str) -> dict:
        issue = self._graphql(ISSUE_QUERY, {"id": id_or_identifier})["issue"]
        priority = int(issue["priority"])
        priority_name, card_priority = PRIORITIES[priority]
        return {
            **_issue_summary(issue),
            "priority": priority,
            "priorityName": priority_name,
            "cardPriority": card_priority,
            "delegate": issue["delegate"]["id"] if issue["delegate"] else None,
            "labels": _label_names(issue),
            "blockedBy": _blocking_issues(issue["inverseRelations"]),
            "agentSessions": sorted(
                (
                    {
                        "id": n["id"],
                        "status": n["status"],
                        "createdAt": n["createdAt"],
                        "appUserId": n["appUser"]["id"],
                        "creatorId": n["creator"]["id"] if n["creator"] else None,
                    }
                    for n in issue["agentSessions"]["nodes"]
                ),
                key=lambda n: n["createdAt"],
                reverse=True,
            ),
        }

    def issue_status(self, id_or_identifier: str) -> dict:
        issue = self._graphql(ISSUE_STATUS_QUERY, {"id": id_or_identifier})["issue"]
        return {
            "stateType": issue["state"]["type"],
            "delegate": issue["delegate"]["id"] if issue["delegate"] else None,
            "branch": issue["branchName"],
        }

    def blockers(self, id_or_identifier: str) -> list:
        """The issues that block this one, each with `delegatedToSelf`: whether this app is its delegate."""
        data = self._graphql(ISSUE_BLOCKERS_QUERY, {"id": id_or_identifier})
        if data["issue"] is None:
            raise LinearError(f"Linear has no issue {id_or_identifier} that the agent app can read")
        return [
            {**blocker, "delegatedToSelf": blocker["delegate"] == data["viewer"]["id"]}
            for blocker in _blocking_issues(data["issue"]["inverseRelations"])
        ]

    def issue_labels(self, id_or_identifier: str) -> list:
        return _label_names(self._graphql(ISSUE_LABELS_QUERY, {"id": id_or_identifier})["issue"])

    def issue_documents(self, id_or_identifier: str) -> dict:
        data = self._graphql(ISSUE_DOCUMENTS_QUERY, {"id": id_or_identifier})
        viewer = data["viewer"]["id"]
        return {
            "identifier": data["issue"]["identifier"],
            "documents": [
                {"id": n["id"], "title": n["title"], "own": (n["creator"] or {}).get("id") == viewer}
                for n in data["issue"]["documents"]["nodes"]
            ],
        }

    def triage_people(self, id_or_identifier: str) -> dict:
        """Profile URLs of the people a triage question can go to. An app that created the issue is not a reporter."""
        issue = self._graphql(TRIAGE_PEOPLE_QUERY, {"id": id_or_identifier})["issue"]
        if issue is None:
            raise LinearError(f"Linear has no issue {id_or_identifier} that the agent app can read")
        creator = issue["creator"]
        responsibility = issue["team"]["triageResponsibility"]
        on_triage = responsibility and responsibility["currentUser"]
        return {
            "assignee": issue["assignee"]["url"] if issue["assignee"] else None,
            "creator": creator["url"] if creator and not creator["app"] else None,
            "triage": on_triage["url"] if on_triage else None,
        }

    def rename_document(self, document_id: str, title: str) -> None:
        payload = self._graphql(DOCUMENT_UPDATE_MUTATION, {"id": document_id, "input": {"title": title}})
        if not payload["documentUpdate"]["success"]:
            raise LinearError("documentUpdate reported success: false")

    def move_to_todo(self, id_or_identifier: str) -> dict:
        """Move an issue from the Triage status to its team's Todo status, and leave any other issue as it is."""
        issue = self._graphql(ISSUE_STATES_QUERY, {"id": id_or_identifier})["issue"]
        result = {"identifier": issue["identifier"], "moved": False, "state": issue["state"]["name"]}
        if issue["state"]["type"] != "triage":
            return result
        unstarted = sorted(
            (s for s in issue["team"]["states"]["nodes"] if s["type"] == "unstarted"), key=lambda s: s["position"]
        )
        if not unstarted:
            raise LinearError(f"{issue['identifier']}'s team has no unstarted status to move it to")
        target = next((s for s in unstarted if s["name"] == READY_STATE_NAME), unstarted[0])
        payload = self._graphql(ISSUE_UPDATE_MUTATION, {"id": issue["id"], "input": {"stateId": target["id"]}})
        if not payload["issueUpdate"]["success"]:
            raise LinearError("issueUpdate reported success: false")
        return {**result, "moved": True, "state": target["name"]}

    def list_delegated_issues(self, *, include_closed: bool = False) -> list:
        issue_filter: dict = {"delegate": {"isMe": {"eq": True}}}
        if not include_closed:
            issue_filter["state"] = {"type": {"nin": list(CLOSED_STATE_TYPES)}}
        issues: list = []
        after: Optional[str] = None
        while True:
            page = self._graphql(DELEGATED_ISSUES_QUERY, {"filter": issue_filter, "after": after})["issues"]
            issues.extend(_issue_summary(n) for n in page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return issues
            after = page["pageInfo"]["endCursor"]

    def delegated_to_self(self, id_or_identifier: str) -> bool:
        data = self._graphql(ISSUE_DELEGATE_QUERY, {"id": id_or_identifier})
        if data["issue"] is None:
            raise LinearError(f"Linear has no issue {id_or_identifier} that the agent app can read")
        delegate = data["issue"]["delegate"]
        return delegate is not None and delegate["id"] == data["viewer"]["id"]

    def undelegate_self(self, id_or_identifier: str) -> dict:
        data = self._graphql(ISSUE_DELEGATE_QUERY, {"id": id_or_identifier})
        issue = data["issue"]
        delegate = issue["delegate"]["id"] if issue["delegate"] else None
        if delegate is None:
            return {"identifier": issue["identifier"], "removed": False}
        if delegate != data["viewer"]["id"]:
            raise LinearError(
                f"{issue['identifier']} is delegated to another agent; only its own delegation can be removed"
            )
        update = {"id": issue["id"], "input": {"delegateId": None}}
        payload = self._graphql(ISSUE_UPDATE_MUTATION, update)["issueUpdate"]
        if not payload["success"]:
            raise LinearError("issueUpdate reported success: false")
        return {"identifier": issue["identifier"], "removed": True}

    def access_token(self) -> str:
        return self.issue_token()["access_token"]

    def issue_token(self) -> dict:
        """A cached token can be revoked before it expires, so a cheap query proves it first.
        A rejected token is replaced the same way any other call replaces it.
        """
        self._graphql(VIEWER_QUERY, {})
        self._access_token()
        return {"access_token": self._token["access_token"], "expires_at": self._token["expires_at"]}

    def _graphql(self, query: str, variables: dict) -> dict:
        try:
            return self._post_graphql(query, variables)
        except _Unauthorized:
            # Client credentials tokens have no refresh token; Linear's documented recovery is a new token.
            self._discard_token()
        try:
            return self._post_graphql(query, variables)
        except _Unauthorized:
            raise LinearError("Linear rejected a newly issued token") from None

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
        if self._token_cache_path:
            with contextlib.suppress(FileNotFoundError):
                os.remove(self._token_cache_path)

    def _read_cached_token(self) -> Optional[dict]:
        if not self._token_cache_path:
            return None
        try:
            with open(self._token_cache_path) as f:
                token = json.load(f)
        except (OSError, ValueError):
            # A missing or corrupt cache only costs a fresh token request.
            return None
        return token if isinstance(token, dict) and token.get("client_id") == self._client_id else None

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
        except urllib.error.URLError as e:
            raise LinearError(f"Linear is unreachable: {e.reason}") from None


class BrokerLinearClient(LinearClient):
    def __init__(self, socket_path: str, **kwargs: Any) -> None:
        super().__init__("", "", **kwargs)
        self._socket_path = socket_path

    def _mint_token(self) -> dict:
        reply = broker_request(self._socket_path, "linear_token")
        return {"client_id": "", "access_token": reply["token"], "expires_at": float(reply["expires_at"])}


def broker_request(socket_path: str, op: str, *, timeout: float = BROKER_TIMEOUT_SECONDS, **fields) -> dict:
    for delay in (*BROKER_RETRY_DELAYS, None):
        try:
            return _broker_attempt(socket_path, {"op": op, **fields}, timeout)
        except BrokerUnavailable as error:
            if delay is None or not error.retry:
                raise
            time.sleep(delay)


def _broker_attempt(socket_path: str, request: dict, timeout: float) -> dict:
    reply = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(socket_path)
            client.sendall(json.dumps(request).encode() + b"\n")
            while not reply.endswith(b"\n") and len(reply) < BROKER_REPLY_LIMIT:
                chunk = client.recv(4096)
                if not chunk:
                    break
                reply += chunk
    except TimeoutError:
        # A broker that accepted the request and then stalled would stall a retry the same way.
        raise BrokerUnavailable(f"the gateway's broker at {socket_path} did not answer in {timeout}s", retry=False) from None
    except OSError as error:
        raise BrokerUnavailable(
            f"the gateway's broker at {socket_path} did not answer: {error.strerror or error}"
        ) from None
    try:
        document = json.loads(reply)
    except ValueError:
        raise BrokerUnavailable(f"the gateway's broker at {socket_path} closed the connection without an answer") from None
    if isinstance(document, dict) and document.get("state") == "busy":
        raise BrokerUnavailable(f"the gateway's broker is busy and refused {request['op']}")
    if not isinstance(document, dict) or document.get("ok") is not True:
        message = document.get("message") if isinstance(document, dict) else None
        raise LinearError(f"the gateway's broker refused {request['op']}: {message or 'no reason given'}")
    return document


class _Unauthorized(Exception):
    pass


def _issue_summary(issue: dict) -> dict:
    return {
        "id": issue["id"],
        "identifier": issue["identifier"],
        "title": issue["title"],
        "url": issue["url"],
        "gitBranchName": issue["branchName"],
        "state": issue["state"]["name"],
        "stateType": issue["state"]["type"],
        "attachments": [{"url": a["url"], "sourceType": a["sourceType"]} for a in issue["attachments"]["nodes"]],
    }


def _blocking_issues(relations: dict) -> list:
    """The issues on the far side of the `blocks` relations that Linear files under the blocked issue's `inverseRelations`."""
    return [
        {
            "id": n["issue"]["id"],
            "identifier": n["issue"]["identifier"],
            "gitBranchName": n["issue"]["branchName"],
            "stateType": n["issue"]["state"]["type"],
            "delegate": n["issue"]["delegate"]["id"] if n["issue"]["delegate"] else None,
        }
        for n in relations["nodes"]
        if n["type"] == "blocks"
    ]


def _label_names(issue: dict) -> list:
    return sorted(label["name"] for label in issue["labels"]["nodes"])


def _parse_json(body: bytes, status: int) -> dict:
    try:
        document = json.loads(body or b"{}")
    except ValueError:
        raise LinearError(f"Linear returned non-JSON HTTP {status}") from None
    if not isinstance(document, dict):
        raise LinearError(f"Linear returned unexpected JSON with HTTP {status}")
    return document


def client_from_env(environ: Mapping[str, str] = os.environ, *, timeout: float = 10.0, **kwargs: Any) -> LinearClient:
    client_id = environ.get("LINEAR_CLIENT_ID")
    client_secret = environ.get("LINEAR_CLIENT_SECRET")
    if not client_id or not client_secret:
        return BrokerLinearClient(broker_socket_path(environ), timeout=timeout, **kwargs)
    return LinearClient(
        client_id,
        client_secret,
        token_cache_path=token_cache_path(environ),
        timeout=timeout,
        **kwargs,
    )


def broker_socket_path(environ: Mapping[str, str]) -> str:
    home = os.path.realpath(environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes"))
    parent = os.path.dirname(home)
    root = os.path.dirname(parent) if os.path.basename(parent) == "profiles" else home
    return os.path.join(root, BROKER_SOCKET)


def token_cache_path(environ: Mapping[str, str]) -> Optional[str]:
    if environ.get("HERMES_HOME"):
        return os.path.join(environ["HERMES_HOME"], "plugin-data", "linear-agent-session", "token.json")
    return None
