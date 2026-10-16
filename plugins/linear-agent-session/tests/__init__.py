from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.parse

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "linear_agent_session", os.path.join(PLUGIN_DIR, "__init__.py"), submodule_search_locations=[PLUGIN_DIR]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class FakeLinear:
    def __init__(self, graphql_responses=(), expires_in: int = 2591999) -> None:
        self.graphql_responses = list(graphql_responses)
        self.expires_in = expires_in
        self.token_requests = []
        self.graphql_requests = []
        self.tokens_issued = 0

    def __call__(self, request, timeout=None):
        if request.full_url == "https://api.linear.app/oauth/token":
            self.token_requests.append(
                {
                    "form": dict(urllib.parse.parse_qsl(request.data.decode())),
                    "authorization": request.get_header("Authorization"),
                    "content_type": request.get_header("Content-type"),
                }
            )
            self.tokens_issued += 1
            body = {"access_token": f"token-{self.tokens_issued}", "token_type": "Bearer",
                    "expires_in": self.expires_in, "scope": "read write"}
            return FakeResponse(200, json.dumps(body).encode())
        assert request.full_url == "https://api.linear.app/graphql", request.full_url
        self.graphql_requests.append(
            {"authorization": request.get_header("Authorization"), **json.loads(request.data.decode())}
        )
        status, body = self.graphql_responses.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(json.dumps(body).encode()))
        return FakeResponse(status, json.dumps(body).encode())


def activity_ok(activity_id: str = "activity-1"):
    return 200, {"data": {"agentActivityCreate": {"success": True, "agentActivity": {"id": activity_id}}}}


def session_update_ok():
    return 200, {"data": {"agentSessionUpdate": {"success": True}}}


class FakeBroker:
    def __init__(self, path: str, replies) -> None:
        self.requests = []
        self.replies = list(replies)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client:
                request = b""
                while not request.endswith(b"\n"):
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                self.requests.append(json.loads(request))
                client.sendall(json.dumps(self.replies.pop(0)).encode() + b"\n")

    def close(self) -> None:
        self.server.close()


def linear_token(token: str, expires_at: float = 1e12) -> dict:
    return {"ok": True, "token": token, "expires_at": expires_at}
