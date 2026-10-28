from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, NamedTuple, Optional

if __package__:
    from . import broker_client
else:
    import broker_client

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

VIEWER_QUERY = "query { viewer { id } }"

SESSION_VERBS = {"triage": "Triage", "work": "Work on"}

SESSION_UPDATE_MUTATION = """mutation AgentSessionUpdate($id: String!, $input: AgentSessionUpdateInput!) {
  agentSessionUpdate(id: $id, input: $input) { success }
}"""

SESSION_THREAD_QUERY = """query AgentSessionThread($id: String!) {
  agentSession(id: $id) { comment { id } issue { id } }
}"""

ISSUE_SESSIONS_QUERY = """query AgentSessionSiblings($id: String!) {
  agentSession(id: $id) {
    url createdAt appUser { id }
    issue { agentSessions(first: 50) { nodes { id status createdAt appUser { id } } } }
  }
}"""

# An awaitingInput session never goes stale and may hold questions a person has yet to answer.
OPEN_SESSION_STATUSES = ("pending", "active", "stale")

ACTIVITY_FIELDS = "id createdAt signal content { __typename ... on AgentActivityPromptContent { body } }"

ACTIVITY_QUERY = f"""query AgentActivity($id: String!) {{
  agentActivity(id: $id) {{ {ACTIVITY_FIELDS} }}
}}"""

SESSION_ACTIVITIES_QUERY = f"""query AgentSessionActivities($id: String!) {{
  agentSession(id: $id) {{ activities(first: 50, orderBy: createdAt) {{ nodes {{ {ACTIVITY_FIELDS} }} }} }}
}}"""

# Linear returns the newest activities first with `first` and `orderBy: createdAt`, so the limit keeps the newest.
THREAD_LIMIT = 30
THREAD_BODY_LIMIT = 2000
THREAD_TRUNCATED = "... [truncated]"
THREAD_TYPES = {
    "AgentActivityPromptContent": "prompt",
    "AgentActivityResponseContent": "response",
    "AgentActivityElicitationContent": "elicitation",
    "AgentActivityThoughtContent": "thought",
    "AgentActivityActionContent": "action",
    "AgentActivityErrorContent": "error",
}

SESSION_HISTORY_QUERY = f"""query AgentSessionHistory($id: String!) {{
  agentSession(id: $id) {{
    activities(first: {THREAD_LIMIT}, orderBy: createdAt) {{
      nodes {{
        id createdAt ephemeral user {{ name }}
        content {{
          __typename
          ... on AgentActivityPromptContent {{ body }}
          ... on AgentActivityResponseContent {{ body }}
          ... on AgentActivityElicitationContent {{ body }}
          ... on AgentActivityThoughtContent {{ body }}
          ... on AgentActivityErrorContent {{ body }}
          ... on AgentActivityActionContent {{ action parameter result }}
        }}
      }}
    }}
  }}
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
    id identifier title description url branchName priority
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

# The team's labels and the workspace's own (no team) are the labels an issue of the team can carry.
ISSUE_ROLE_LABELS_QUERY = """query IssueRoleLabels($id: String!) {
  issue(id: $id) { team { labels(first: 250) { nodes { name isGroup retiredAt archivedAt } } } }
  issueLabels(first: 250, filter: { team: { null: true } }) { nodes { name isGroup retiredAt archivedAt } }
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

ISSUE_EDIT_QUERY = """query IssueEdit($id: String!) {
  issue(id: $id) {
    id identifier title description
    state { name }
    labels { nodes { id name parent { id } } }
    team { states { nodes { id name } } labels(first: 250) { nodes { id name isGroup parent { id } } } }
  }
  issueLabels(first: 250, filter: { team: { null: true } }) { nodes { id name isGroup parent { id } } }
}"""

ISSUE_RELATIONS_QUERY = """query IssueRelations($a: String!, $b: String!) {
  a: issue(id: $a) { ...RelationFields }
  b: issue(id: $b) { ...RelationFields }
}
fragment RelationFields on Issue {
  id identifier
  state { name type }
  team { states { nodes { id name type } } }
  relations(first: 51) { pageInfo { hasNextPage } nodes { id type issue { id identifier } relatedIssue { id identifier } } }
  inverseRelations(first: 51) { pageInfo { hasNextPage } nodes { id type issue { id identifier } relatedIssue { id identifier } } }
}"""

RELATIONS_READ = 50

ISSUE_RELATION_CREATE_MUTATION = """mutation IssueRelationCreate($input: IssueRelationCreateInput!) {
  issueRelationCreate(input: $input) { success }
}"""

ISSUE_RELATION_DELETE_MUTATION = """mutation IssueRelationDelete($id: String!) {
  issueRelationDelete(id: $id) { success }
}"""

ISSUE_SEARCH_QUERY = """query IssueSearch($term: String!, $first: Int!) {
  searchIssues(term: $term, first: $first) { nodes { identifier title url team { name } state { name } } }
}"""

GITHUB_ISSUE_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/issues/[0-9]+")

ISSUE_UPDATE_MUTATION = """mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success }
}"""

CLOSED_STATE_TYPES = ("completed", "canceled", "duplicate")
READY_STATE_NAME = "Todo"

# The triage roles are the factory's own vocabulary; a workspace names each label as it likes.
TRIAGE_ROLES = ("needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix")

UrlOpen = Callable[..., Any]


class LinearError(Exception):
    pass


CLIENT_ERRORS = (LinearError, broker_client.BrokerError)


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


def card_title(identifier: str, title: str) -> str:
    kept = re.sub(r"[^A-Za-z0-9 .-]", "", title)
    return f"{identifier}: {' '.join(kept.split())}"


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

    def close_older_sessions(self, agent_session_id: str) -> list:
        """Ends this app's older open sessions on the issue, which Linear would otherwise mark stale."""
        session = self._graphql(ISSUE_SESSIONS_QUERY, {"id": agent_session_id})["agentSession"]
        if session is None or session["issue"] is None:
            return []
        closed = []
        for other in session["issue"]["agentSessions"]["nodes"]:
            if (
                other["id"] != agent_session_id
                and other["appUser"] is not None
                and other["appUser"]["id"] == session["appUser"]["id"]
                and other["createdAt"] < session["createdAt"]
                and other["status"] in OPEN_SESSION_STATUSES
            ):
                body = f"Work on this issue continues in [this session]({session['url']})."
                self.create_activity(other["id"], activity_content("response", body))
                closed.append(other["id"])
        return closed

    def set_external_urls(self, agent_session_id: str, urls: list) -> None:
        external_urls = [{"label": u["label"], "url": u["url"]} for u in urls]
        self._update_session(agent_session_id, {"externalUrls": external_urls})

    def title_session(self, agent_session_id: str, issue_id: str, purpose: str) -> str:
        """Replace Linear's default `Work on <identifier>` session title with one that says what the session is for."""
        issue = self.get_issue(issue_id)
        summary = _session_summary(f"{SESSION_VERBS[purpose]} {issue['identifier']}: {issue['title']}")
        self._update_session(agent_session_id, {"summary": summary})
        return summary

    def _update_session(self, agent_session_id: str, fields: dict) -> None:
        payload = self._graphql(SESSION_UPDATE_MUTATION, {"id": agent_session_id, "input": fields})[
            "agentSessionUpdate"
        ]
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

    def session_thread(self, agent_session_id: str) -> list:
        session = self._graphql(SESSION_HISTORY_QUERY, {"id": agent_session_id})["agentSession"]
        if session is None:
            raise LinearError(f"Linear has no agent session {agent_session_id} that the agent app can read")
        nodes = sorted(
            (n for n in session["activities"]["nodes"] if not n["ephemeral"]), key=lambda n: n["createdAt"]
        )
        return [
            {
                "id": n["id"],
                "type": THREAD_TYPES[n["content"]["__typename"]],
                "author": n["user"]["name"] if n["user"] else None,
                "created_at": n["createdAt"],
                "body": _bounded(_activity_text(n["content"])),
            }
            for n in nodes
        ]

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
            "cardTitle": card_title(issue["identifier"], issue["title"]),
            "priority": priority,
            "priorityName": priority_name,
            "cardPriority": card_priority,
            "description": issue["description"] or "",
            "githubIssues": [a["url"] for a in issue["attachments"]["nodes"] if GITHUB_ISSUE_URL.fullmatch(a["url"])],
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

    def role_labels(self, id_or_identifier: str) -> dict:
        """Each triage role's label as Linear names it for this issue, so text for people never names the role."""
        data = self._graphql(ISSUE_ROLE_LABELS_QUERY, {"id": id_or_identifier})
        if data["issue"] is None:
            raise LinearError(f"Linear has no issue {id_or_identifier} that the agent app can read")
        available = [
            label["name"]
            for label in data["issue"]["team"]["labels"]["nodes"] + data["issueLabels"]["nodes"]
            if not (label["isGroup"] or label["retiredAt"] or label["archivedAt"])
        ]
        by_key: dict = {}
        for name in available:
            by_key.setdefault(role_key(name), name)
        return {role: by_key[role_key(role)] for role in TRIAGE_ROLES if role_key(role) in by_key}

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

    def edit_issue(
        self,
        id_or_identifier: str,
        *,
        add_labels: list = (),
        remove_labels: list = (),
        state: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict:
        """An added label replaces the issue's other labels in its group, because Linear allows one per group."""
        data = self._graphql(ISSUE_EDIT_QUERY, {"id": id_or_identifier})
        issue = data["issue"]
        if issue is None:
            raise LinearError(f"Linear has no issue {id_or_identifier} that the agent app can read")
        known = {n["id"]: n for n in issue["team"]["labels"]["nodes"] + data["issueLabels"]["nodes"]}
        current = {n["id"]: n for n in issue["labels"]["nodes"]}
        labels = dict(current)
        for name in remove_labels:
            for label_id in [i for i, n in labels.items() if role_key(n["name"]) == role_key(name)]:
                del labels[label_id]
        for name in add_labels:
            label = _by_name(known.values(), name, "label")
            if label["isGroup"]:
                raise LinearError(f"{label['name']} is a label group; name one of its labels")
            group = (label["parent"] or {}).get("id")
            if group:
                labels = {i: n for i, n in labels.items() if (n["parent"] or {}).get("id") != group}
            labels[label["id"]] = label
        update: dict = {}
        if title is not None and title != issue["title"]:
            update["title"] = title
        if description is not None and description != (issue["description"] or ""):
            update["description"] = description
        if labels.keys() != current.keys():
            update["labelIds"] = sorted(labels)
        target = _by_name(issue["team"]["states"]["nodes"], state, "status") if state else None
        if target and target["name"] != issue["state"]["name"]:
            update["stateId"] = target["id"]
        if update:
            payload = self._graphql(ISSUE_UPDATE_MUTATION, {"id": issue["id"], "input": update})
            if not payload["issueUpdate"]["success"]:
                raise LinearError("issueUpdate reported success: false")
        return {
            "identifier": issue["identifier"],
            "changed": bool(update),
            "title": update.get("title", issue["title"]),
            "labels": sorted(n["name"] for n in labels.values()),
            "state": target["name"] if target else issue["state"]["name"],
        }

    def relate_issues(self, id_or_identifier: str, other: str, kind: str) -> dict:
        """Make `issue` a duplicate of `other` (kind `duplicate`), or make `other` block it (kind `blocks`).

        A duplicate also loses the `related` relation between the two and moves to the team's duplicate status.
        No other relation is touched.
        """
        if kind not in RELATION_RULES:
            raise LinearError(f"unknown relation kind {kind!r}; expected one of {', '.join(RELATION_RULES)}")
        a, b, relations = self._read_relations(id_or_identifier, other)
        existing = decide_relation(kind, a, b, relations)
        if existing is None:
            # A `blocks` relation runs from the blocker to the blocked issue; a duplicate from the duplicate to the original.
            source, target = (b, a) if kind == "blocks" else (a, b)
            payload = self._graphql(
                ISSUE_RELATION_CREATE_MUTATION,
                {"input": {"issueId": source["id"], "relatedIssueId": target["id"], "type": kind}},
            )
            if not payload["issueRelationCreate"]["success"]:
                raise LinearError("issueRelationCreate reported success: false")
        result = {
            "identifier": a["identifier"],
            "related_issue": b["identifier"],
            "relation": kind,
            "created": existing is None,
            "removed_related": False,
            "state": a["state"]["name"],
        }
        if kind == "duplicate":
            # The relation this call created can make a list of 50 one longer.
            a, b, relations = self._read_relations(id_or_identifier, other, most=RELATIONS_READ + 1)
            for relation in relations.values():
                if relation.type == "related" and {relation.source, relation.target} == {a["id"], b["id"]}:
                    payload = self._graphql(ISSUE_RELATION_DELETE_MUTATION, {"id": relation.id})
                    if not payload["issueRelationDelete"]["success"]:
                        raise LinearError("issueRelationDelete reported success: false")
                    result["removed_related"] = True
            result["state"] = self._move_to_duplicate_status(a)
        return result

    def _read_relations(self, first: str, second: str, most: int = RELATIONS_READ) -> tuple:
        data = self._graphql(ISSUE_RELATIONS_QUERY, {"a": first, "b": second})
        for key, name in (("a", first), ("b", second)):
            if data[key] is None:
                raise LinearError(f"Linear has no issue {name} that the agent app can read")
        relations: dict = {}
        names = {i["id"]: i["identifier"] for i in (data["a"], data["b"])}
        for issue in (data["a"], data["b"]):
            for side in ("relations", "inverseRelations"):
                if issue[side]["pageInfo"]["hasNextPage"] or len(issue[side]["nodes"]) > most:
                    raise LinearError(
                        f"{issue['identifier']} has more than {RELATIONS_READ} {side}, more than the agent reads, so nothing was changed"
                    )
                for node in issue[side]["nodes"]:
                    relations[node["id"]] = Relation(
                        node["id"], node["type"], node["issue"]["id"], node["relatedIssue"]["id"]
                    )
                    names[node["issue"]["id"]] = node["issue"]["identifier"]
                    names[node["relatedIssue"]["id"]] = node["relatedIssue"]["identifier"]
        return {**data["a"], "names": names}, {**data["b"], "names": names}, relations

    def _move_to_duplicate_status(self, issue: dict) -> str:
        if issue["state"]["type"] == "duplicate":
            return issue["state"]["name"]
        states = [s for s in issue["team"]["states"]["nodes"] if s["type"] == "duplicate"]
        if len(states) != 1:
            problem = (
                "no status of type duplicate"
                if not states
                else "more than one status of type duplicate: " + ", ".join(sorted(s["name"] for s in states))
            )
            raise LinearError(
                f"the duplicate relation exists, but {issue['identifier']}'s team has {problem}, so its status did not change"
            )
        payload = self._graphql(ISSUE_UPDATE_MUTATION, {"id": issue["id"], "input": {"stateId": states[0]["id"]}})
        if not payload["issueUpdate"]["success"]:
            raise LinearError("issueUpdate reported success: false")
        return states[0]["name"]

    def search_issues(self, term: str) -> list:
        nodes = self._graphql(ISSUE_SEARCH_QUERY, {"term": term, "first": 10})["searchIssues"]["nodes"]
        return [
            {"identifier": n["identifier"], "title": n["title"], "url": n["url"], "team": n["team"]["name"], "state": n["state"]["name"]}
            for n in nodes
        ]

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
        reply = broker_client.request(self._socket_path, "linear_token")
        return {"client_id": "", "access_token": reply["token"], "expires_at": float(reply["expires_at"])}


class TokenLinearClient(LinearClient):
    """A client for a process that already holds an access token, such as Claude's `LINEAR_API_KEY`."""

    def __init__(self, token: str, **kwargs: Any) -> None:
        super().__init__("", "", **kwargs)
        self._static_token = token

    def _mint_token(self) -> dict:
        return {"client_id": "", "access_token": self._static_token, "expires_at": float("inf")}


class _Unauthorized(Exception):
    pass


def _session_summary(title: str) -> str:
    """Linear accepts a session summary of 1 to 255 characters with no line breaks."""
    return " ".join(title.split())[:255].strip()


def _activity_text(content: dict) -> str:
    if "action" not in content:
        return content["body"]
    text = f"{content['action']} {content['parameter']}".strip()
    return f"{text} -> {content['result']}" if content.get("result") else text


def _bounded(text: str) -> str:
    return text if len(text) <= THREAD_BODY_LIMIT else text[:THREAD_BODY_LIMIT] + THREAD_TRUNCATED


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


class Relation(NamedTuple):
    id: str
    type: str
    source: str
    target: str


# Each rule is (relation type, source role, target role, verdict, message). A role is `a` (the issue being
# changed), `b` (the other issue), or `*` (any issue but those two). `existing` means the request already holds.
RELATION_RULES = {
    "duplicate": (
        ("duplicate", "a", "b", "existing", None),
        ("duplicate", "a", "*", "conflict", "{a} is already a duplicate of {target}"),
        ("duplicate", "b", "a", "conflict", "{b} is already a duplicate of {a}"),
    ),
    "blocks": (
        ("blocks", "b", "a", "existing", None),
        ("blocks", "a", "b", "conflict", "{b} is already blocked on {a}"),
    ),
}


def decide_relation(kind: str, a: dict, b: dict, relations: dict) -> Optional[Relation]:
    """The relation that already satisfies the request, or None when one must be created. Raises on a conflict."""
    if a["id"] == b["id"]:
        raise LinearError(f"{a['identifier']} cannot be related to itself")
    roles = {a["id"]: "a", b["id"]: "b"}
    for type_, source, target, verdict, message in RELATION_RULES[kind]:
        for relation in relations.values():
            if (
                relation.type == type_
                and roles.get(relation.source, "*") == source
                and roles.get(relation.target, "*") == target
            ):
                if verdict == "existing":
                    return relation
                text = message.format(a=a["identifier"], b=b["identifier"], target=a["names"][relation.target])
                raise LinearError(f"{text} (relation {relation.id}), so nothing was changed")
    return None


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


def role_key(name: str) -> str:
    """A role name or a label name without its case and punctuation, so `Won't fix` is `wontfix`."""
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def _by_name(nodes, name: str, kind: str) -> dict:
    matches = [n for n in nodes if role_key(n["name"]) == role_key(name)]
    exact = [n for n in matches if n["name"] == name]
    if len(exact) == 1 or len(matches) == 1:
        return (exact or matches)[0]
    if matches:
        raise LinearError(f"more than one {kind} matches {name!r}: " + ", ".join(sorted(n["name"] for n in matches)))
    names = ", ".join(sorted({n["name"] for n in nodes if not n.get("isGroup")}))
    raise LinearError(f"no {kind} named {name!r}; the issue's team has: {names}")


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
        return BrokerLinearClient(broker_client.socket_path(environ), timeout=timeout, **kwargs)
    return LinearClient(
        client_id,
        client_secret,
        token_cache_path=token_cache_path(environ),
        timeout=timeout,
        **kwargs,
    )


def token_cache_path(environ: Mapping[str, str]) -> Optional[str]:
    if environ.get("HERMES_HOME"):
        return os.path.join(environ["HERMES_HOME"], "plugin-data", "linear-agent-session", "token.json")
    return None
