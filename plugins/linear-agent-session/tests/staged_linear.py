"""The real plugin client against a fake Linear, for tests that run a helper in a subprocess.

A test links this file as linear.py in a staged home's plugins/linear-agent-session.
$HERMES_HOME/linear-fake.json lists the tokens Linear accepts. Each request is
appended to $HERMES_HOME/linear-fake.log as one JSON line, and every token
request issues minted-<n>, which Linear then accepts unless reject_all is set.
An issue's labels come from its `labels` list, `["ready-for-agent"]` when unset, and its delegate
from `delegate`, none when unset; `hidden` makes Linear return no issue.
The labels the issue can carry come from `team_labels` and `workspace_labels`, lists of names, none when unset.
Its Documents come from its `documents` list of {id, title, creator}, and a
DocumentUpdate rewrites the title there, `documents_error` fails the read, and
`documents_timeout` times it out.
Its assignee and the team's current triage person are the profile URLs in `assignee` and `on_triage`, and
its creator is `creator`, an app when `creator_app` is set; each is no one when unset, `people_error`
fails the read, and `people_timeout` times it out.
A posted activity is logged as `activity`, with the session's issue holding no other session.
Its blockers come from its `blocked_by` list of {id, identifier, gitBranchName, stateType, delegate}, none
when unset, and `blockers_error` fails the read.
The app's own user id is app-user.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_REAL = Path(__file__).resolve().parents[1] / "linear.py"
_HOME = Path(os.environ["HERMES_HOME"])


class _Response(io.BytesIO):
    def __init__(self, status: int, body: dict) -> None:
        super().__init__(json.dumps(body).encode())
        self.status = status


def _fake_urlopen(request, timeout=None):
    state_path = _HOME / "linear-fake.json"
    state = json.loads(state_path.read_text())
    entry = {"url": request.full_url, "authorization": request.get_header("Authorization")}
    if request.full_url.endswith("/oauth/token"):
        entry["form"] = dict(urllib.parse.parse_qsl(request.data.decode()))
        token = f"minted-{sum(1 for _ in _log_entries() if 'form' in _) + 1}"
        if not state.get("reject_all"):
            state["accepted"].append(token)
            state_path.write_text(json.dumps(state))
        response = _Response(200, {"access_token": token, "token_type": "Bearer", "expires_in": 2591999})
    elif request.get_header("Authorization", "").removeprefix("Bearer ") in state["accepted"]:
        body = json.loads(request.data.decode())
        query = body["query"]
        if "IssueRoleLabels" in query:
            def nodes(names):
                return [{"name": n, "isGroup": False, "retiredAt": None, "archivedAt": None} for n in names]
            team = {"labels": {"nodes": nodes(state.get("team_labels", []))}}
            response = _Response(200, {"data": {"issue": {"team": team},
                                                "issueLabels": {"nodes": nodes(state.get("workspace_labels", []))}}})
        elif "IssueLabels" in query:
            labels = [{"name": name} for name in state.get("labels", ["ready-for-agent"])]
            response = _Response(200, {"data": {"issue": {"labels": {"nodes": labels}}}})
        elif "IssueDelegate" in query:
            delegate = {"id": state["delegate"]} if state.get("delegate") else None
            issue = None if state.get("hidden") else {"id": "issue-uuid", "identifier": "ABC-123", "delegate": delegate}
            response = _Response(200, {"data": {"viewer": {"id": "app-user"}, "issue": issue}})
        elif "IssueBlockers" in query and state.get("blockers_error"):
            response = _Response(200, {"errors": [{"message": state["blockers_error"]}]})
        elif "IssueBlockers" in query:
            nodes = [
                {"type": "blocks", "issue": {
                    "id": b["id"], "identifier": b["identifier"], "branchName": b["gitBranchName"],
                    "state": {"type": b["stateType"]}, "delegate": {"id": b["delegate"]} if b["delegate"] else None,
                }}
                for b in state.get("blocked_by", [])
            ]
            response = _Response(200, {"data": {"viewer": {"id": "app-user"}, "issue": {"inverseRelations": {"nodes": nodes}}}})
        elif "AgentActivityCreate" in query:
            entry["activity"] = body["variables"]["input"]
            response = _Response(200, {"data": {"agentActivityCreate": {"success": True, "agentActivity": {"id": "activity-1"}}}})
        elif "AgentSessionSiblings" in query:
            session = {"url": "https://linear.app/example/agent-session/live", "createdAt": "2026-10-27T10:00:00.000Z",
                       "appUser": {"id": "app-user"}, "issue": {"agentSessions": {"nodes": []}}}
            response = _Response(200, {"data": {"agentSession": session}})
        elif "TriagePeople" in query and state.get("people_timeout"):
            raise TimeoutError("The read operation timed out")
        elif "TriagePeople" in query and state.get("people_error"):
            response = _Response(200, {"errors": [{"message": state["people_error"]}]})
        elif "TriagePeople" in query:
            def user(url):
                return {"url": url} if url else None
            creator = {"url": state["creator"], "app": bool(state.get("creator_app"))} if state.get("creator") else None
            issue = {"assignee": user(state.get("assignee")), "creator": creator,
                     "team": {"triageResponsibility": {"currentUser": user(state.get("on_triage"))}}}
            response = _Response(200, {"data": {"issue": issue}})
        elif "IssueDocuments" in query and state.get("documents_timeout"):
            raise TimeoutError("The read operation timed out")
        elif "IssueDocuments" in query and state.get("documents_error"):
            response = _Response(200, {"errors": [{"message": state["documents_error"]}]})
        elif "IssueDocuments" in query:
            nodes = [{**d, "creator": {"id": d["creator"]}} for d in state.get("documents", [])]
            issue = {"identifier": "ABC-123", "documents": {"nodes": nodes}}
            response = _Response(200, {"data": {"viewer": {"id": "app-user"}, "issue": issue}})
        elif "DocumentUpdate" in query:
            variables = body["variables"]
            for document in state.get("documents", []):
                if document["id"] == variables["id"]:
                    document["title"] = variables["input"]["title"]
            state_path.write_text(json.dumps(state))
            response = _Response(200, {"data": {"documentUpdate": {"success": True}}})
        else:
            response = _Response(200, {"data": {"viewer": {"id": "app-user"}}})
    else:
        response = None
    with open(_HOME / "linear-fake.log", "a") as log:
        log.write(json.dumps(entry) + "\n")
    if response is None:
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"errors":[]}'))
    return response


def _log_entries():
    path = _HOME / "linear-fake.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


urllib.request.urlopen = _fake_urlopen
_spec = importlib.util.spec_from_file_location("_real_linear", _REAL)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
globals().update({name: value for name, value in vars(_module).items() if not name.startswith("__")})
