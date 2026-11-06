"""Fakes at sentry-linear-sync's HTTP boundary: Linear's GraphQL API and Sentry's REST API.

Both hold recorded response shapes only. No value here came from a real Sentry event.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

APP_USER = "app-user"
PERSON = "person-1"
STATES = [
    {"id": "state-triage", "name": "Triage", "type": "triage"},
    {"id": "state-backlog", "name": "Backlog", "type": "backlog"},
    {"id": "state-todo", "name": "Todo", "type": "unstarted"},
    {"id": "state-started", "name": "In Progress", "type": "started"},
    {"id": "state-done", "name": "Done", "type": "completed"},
    {"id": "state-canceled", "name": "Canceled", "type": "canceled"},
    {"id": "state-duplicate", "name": "Duplicate", "type": "duplicate"},
]
LABELS = ["Sentry", "Performance & stability"]


class _Server:
    def __init__(self, handle) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner._respond(self, handle("GET", self.path, None))

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner._respond(self, handle("POST", self.path, body))

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    @staticmethod
    def _respond(handler, reply) -> None:
        status, body, headers = reply
        data = json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        for name, value in headers.items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(data)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def linear_issue(identifier: str, *, state: str = "triage", created_at: str = "2026-10-01T00:00:00.000Z", creator=APP_USER,
                 description: str = "", priority: int = 3, assigned: bool = False, attachments=(), comments=(),
                 completed_at=None, canceled_at=None, duplicate_of=None, history=(), labels=("Sentry",)) -> dict:
    return {
        "id": f"id-{identifier}", "identifier": identifier, "createdAt": created_at, "completedAt": completed_at,
        "canceledAt": canceled_at, "description": description, "priority": priority,
        "state": next(s for s in STATES if s["type"] == state), "assignee": {"id": PERSON} if assigned else None,
        "creator": {"id": creator} if creator else None, "attachments": [dict(a) for a in attachments],
        "comments": list(comments), "duplicateOf": duplicate_of, "relations": [], "history": list(history),
        "labels": list(labels), "title": f"Title of {identifier}",
    }


class FakeLinear(_Server):
    """An in-memory Linear team. `writes` records each mutation as (operation, variables)."""

    def __init__(self, issues=(), *, states=STATES, fail=()) -> None:
        self.issues = {i["identifier"]: i for i in issues}
        self.states = list(states)
        self.fail = set(fail)
        self.writes: list = []
        self.next_number = 900
        self.lock = threading.Lock()
        super().__init__(self.handle)

    def find(self, id_or_identifier: str) -> dict | None:
        return next((i for i in self.issues.values() if id_or_identifier in (i["id"], i["identifier"])), None)

    def handle(self, method: str, path: str, body: dict):
        operation = re.search(r"(?:query|mutation) (\w+)", body["query"])[1]
        if operation in self.fail:
            return 500, {"errors": [{"message": f"{operation} failed"}]}, {}
        with self.lock:
            data = getattr(self, f"op_{operation}")(body["variables"])
        return 200, {"data": data}, {}

    def write(self, operation: str, variables: dict) -> None:
        self.writes.append((operation, variables))

    def _team_states(self) -> dict:
        return {"states": {"nodes": [dict(s, position=n) for n, s in enumerate(self.states)]}}

    def op_LabeledIssues(self, v: dict) -> dict:
        label = v["filter"]["labels"]["name"]["eq"]
        nodes = [{
            **{k: i[k] for k in ("id", "identifier", "createdAt", "completedAt", "canceledAt", "description", "priority",
                                 "state", "assignee", "creator")},
            "attachments": {"nodes": i["attachments"]},
            "relations": {"nodes": [{"type": "duplicate", "relatedIssue": {"identifier": i["duplicateOf"]}}] if i["duplicateOf"] else []},
            "comments": {"nodes": [{"body": c} for c in i["comments"]], "pageInfo": {"hasNextPage": False}},
        } for i in self.issues.values() if label in i["labels"]]
        return {"viewer": {"id": APP_USER}, "issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}

    def op_PriorityHistory(self, v: dict) -> dict:
        issue = self.find(v["id"])
        nodes = [{"fromPriority": f, "toPriority": t, "actor": {"id": a} if a else None} for f, t, a in issue["history"]]
        return {"viewer": {"id": APP_USER}, "issue": {"history": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}

    def op_TeamForCreate(self, v: dict) -> dict:
        labels = [{"id": f"label-{name}", "name": name, "isGroup": False, "parent": None} for name in LABELS]
        team = {"id": "team-1", "key": v["key"], "states": {"nodes": [{"id": s["id"], "type": s["type"]} for s in self.states]},
                "labels": {"nodes": labels}}
        return {"viewer": {"id": APP_USER}, "teams": {"nodes": [team]}, "issueLabels": {"nodes": []}}

    def op_IssueCreate(self, v: dict) -> dict:
        self.write("IssueCreate", v["input"])
        identifier = f"ABC-{self.next_number}"
        self.next_number += 1
        state = next(s for s in self.states if s["id"] == v["input"]["stateId"])
        issue = linear_issue(identifier, state=state["type"], created_at="2026-11-03T12:00:00.000Z",
                             description=v["input"]["description"], priority=v["input"].get("priority", 0),
                             labels=[label.removeprefix("label-") for label in v["input"].get("labelIds", [])])
        issue["title"] = v["input"]["title"]
        self.issues[identifier] = issue
        return {"issueCreate": {"success": True, "issue": {"identifier": identifier, "url": f"https://linear.app/x/issue/{identifier}"}}}

    def op_IssueIdentity(self, v: dict) -> dict:
        issue = self.find(v["id"])
        return {"issue": {"id": issue["id"]} if issue else None}

    def op_IssueDelegate(self, v: dict) -> dict:
        issue = self.find(v["id"])
        return {"viewer": {"id": APP_USER}, "issue": {"id": issue["id"], "identifier": issue["identifier"], "delegate": None} if issue else None}

    def op_AttachmentCreate(self, v: dict) -> dict:
        self.write("AttachmentCreate", v["input"])
        issue = self.find(v["input"]["issueId"])
        if not any(a["url"] == v["input"]["url"] for a in issue["attachments"]):
            issue["attachments"].append({"url": v["input"]["url"], "sourceType": "api"})
        return {"attachmentCreate": {"success": True}}

    def op_Issue(self, v: dict) -> dict:
        i = self.find(v["id"])
        return {"issue": {
            "id": i["id"], "identifier": i["identifier"], "title": i["title"], "description": i["description"],
            "url": f"https://linear.app/x/issue/{i['identifier']}", "branchName": "b", "priority": i["priority"],
            "archivedAt": None, "state": i["state"], "delegate": None, "assignee": None, "labels": {"nodes": []},
            "attachments": {"nodes": i["attachments"]}, "agentSessions": {"nodes": []}, "inverseRelations": {"nodes": []},
        }}

    def op_CommentOnIssue(self, v: dict) -> dict:
        self.write("CommentOnIssue", v["input"])
        self.find(v["input"]["issueId"])["comments"].append(v["input"]["body"])
        return {"commentCreate": {"success": True, "comment": {"id": "c", "url": "https://linear.app/x/comment"}}}

    def op_IssueUpdate(self, v: dict) -> dict:
        self.write("IssueUpdate", {"id": v["id"], **v["input"]})
        issue = self.find(v["id"])
        if "stateId" in v["input"]:
            issue["state"] = next(s for s in self.states if s["id"] == v["input"]["stateId"])
        if "priority" in v["input"]:
            if v["input"]["priority"] != issue["priority"]:
                issue["history"].append((issue["priority"], v["input"]["priority"], APP_USER))
            issue["priority"] = v["input"]["priority"]
        return {"issueUpdate": {"success": True}}

    def op_IssueStates(self, v: dict) -> dict:
        i = self.find(v["id"])
        return {"issue": {"id": i["id"], "identifier": i["identifier"], "state": i["state"], "team": self._team_states()}}

    def _relation_fields(self, i: dict) -> dict:
        relations = [
            {"id": f"rel-{n}", "type": r["type"], "issue": {"id": i["id"], "identifier": i["identifier"]},
             "relatedIssue": {"id": self.find(r["to"])["id"], "identifier": r["to"]}}
            for n, r in enumerate(i["relations"])
        ]
        return {
            "id": i["id"], "identifier": i["identifier"], "archivedAt": None, "parent": None, "state": i["state"],
            "team": self._team_states(),
            "relations": {"pageInfo": {"hasNextPage": False}, "nodes": relations},
            "inverseRelations": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        }

    def op_IssueRelations(self, v: dict) -> dict:
        return {"a": self._relation_fields(self.find(v["a"])), "b": self._relation_fields(self.find(v["b"]))}

    def op_IssueRelationCreate(self, v: dict) -> dict:
        self.write("IssueRelationCreate", v["input"])
        source, target = self.find(v["input"]["issueId"]), self.find(v["input"]["relatedIssueId"])
        source["relations"].append({"type": v["input"]["type"], "to": target["identifier"]})
        if v["input"]["type"] == "duplicate":
            source["duplicateOf"] = target["identifier"]
        return {"issueRelationCreate": {"success": True}}


def sentry_issue(short_id: str, *, number: int, project: str = "web", type_: str | None = "TypeError", value: str = "boom",
                 filename: str | None = None, function: str | None = None, users: int = 5, count: int = 20,
                 unhandled: bool = False, first_seen: str = "2026-10-30T00:00:00Z", **extra) -> dict:
    metadata = {"title": "t"}
    if type_:
        metadata.update(type=type_, value=value)
    if filename:
        metadata["filename"] = filename
    if function:
        metadata["function"] = function
    return {
        "id": str(number), "shortId": short_id, "project": {"slug": project, "id": f"p-{project}"},
        "metadata": metadata, "title": value if not type_ else f"{type_}: {value}", "isUnhandled": unhandled,
        "firstSeen": first_seen, "lastSeen": "2026-11-03T00:00:00Z", "userCount": users, "count": str(count), **extra,
    }


class FakeSentry(_Server):
    """A Sentry organization. `requests` records each path and query. `fail` maps (route, n) to the HTTP status of
    the nth request to that route, such as ("issues/", 2) for the second page of issues."""

    def __init__(self, *, projects=("web", "cms"), issues=(), page_size: int = 100, details=None, latest=None,
                 recent=None, releases=None, release_events=None, fail=None) -> None:
        self.projects = list(projects)
        self.issues = list(issues)
        self.page_size = page_size
        self.details = details or {}
        self.latest = latest or {}
        self.recent = recent or {}
        self.releases = releases or {}
        self.release_events = release_events or {}
        self.fail = fail or {}
        self.requests: list = []
        super().__init__(self.handle)

    def handle(self, method: str, path: str, body):
        parts = urlsplit(path)
        query = parse_qs(parts.query)
        self.requests.append((method, parts.path, query))
        if method != "GET":
            return 405, {"detail": "read-only"}, {}
        route = parts.path.removeprefix("/api/0/organizations/example-org/")
        seen = sum(1 for _, p, _ in self.requests if p == parts.path)
        if (route, seen) in self.fail:
            return self.fail[(route, seen)], {"detail": "failed"}, {}
        if route == "projects/":
            return 200, [{"slug": p, "id": f"p-{p}"} for p in self.projects], {}
        if route == "issues/":
            return self._page(self.issues, query, path)
        if found := re.fullmatch(r"issues/(\d+)/events/latest/", route):
            event = self.latest.get(found[1])
            return (200, event, {}) if event else (404, {"detail": "none"}, {})
        if found := re.fullmatch(r"issues/(\d+)/", route):
            detail = self.details.get(found[1])
            return (200, detail, {}) if detail else (404, {"detail": "none"}, {})
        if route == "releases/":
            return 200, self.releases.get(query["project"][0], []), {}
        if route == "events/":
            ids = re.fullmatch(r"issue\.id:\[(.*)\]", query["query"][0])[1].split(",")
            if query["field"][0] == "issue.id":
                rows = [{"issue.id": int(i), "count()": self.recent[i]} for i in ids if self.recent.get(i)]
            else:
                rows = [{"release": r, "count()": n} for (i, r), n in self.release_events.items() if i in ids]
            return 200, {"data": rows, "meta": {}}, {}
        return 404, {"detail": "no route"}, {}

    def _page(self, rows: list, query: dict, path: str):
        start = int(query.get("cursor", ["0"])[0])
        page = rows[start:start + self.page_size]
        more = start + self.page_size < len(rows)
        base = f"{self.url}{urlsplit(path).path}?{urlsplit(path).query.split('&cursor=')[0]}"
        link = (f'<{base}&cursor={start}>; rel="previous"; results="false"; cursor="{start}", '
                f'<{base}&cursor={start + self.page_size}>; rel="next"; results="{"true" if more else "false"}"; cursor="{start + self.page_size}"')
        return 200, page, {"Link": link}
