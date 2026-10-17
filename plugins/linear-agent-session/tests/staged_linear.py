"""The real plugin client against a fake Linear, for tests that run a helper in a subprocess.

A test links this file as linear.py in a staged home's plugins/linear-agent-session.
$HERMES_HOME/linear-fake.json lists the tokens Linear accepts. Each request is
appended to $HERMES_HOME/linear-fake.log as one JSON line, and every token
request issues minted-<n>, which Linear then accepts unless reject_all is set.
An issue's labels come from its `labels` list, `["ready-for-agent"]` when unset, and its delegate
from `delegate`, none when unset; `hidden` makes Linear return no issue. The viewer is always `app-user`.
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
        query = json.loads(request.data.decode())["query"]
        if "IssueLabels" in query:
            labels = [{"name": name} for name in state.get("labels", ["ready-for-agent"])]
            response = _Response(200, {"data": {"issue": {"labels": {"nodes": labels}}}})
        elif "IssueDelegate" in query:
            delegate = {"id": state["delegate"]} if state.get("delegate") else None
            issue = None if state.get("hidden") else {"id": "issue-uuid", "identifier": "ABC-123", "delegate": delegate}
            response = _Response(200, {"data": {"viewer": {"id": "app-user"}, "issue": issue}})
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
