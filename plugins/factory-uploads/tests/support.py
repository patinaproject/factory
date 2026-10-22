from __future__ import annotations

import importlib.util
import json
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent


def load_package(name: str, directory: Path = PLUGIN):
    """Import a plugin directory as a package, the way Hermes loads it."""
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


factory_uploads = load_package("factory_uploads")
upload_token = importlib.import_module("factory_uploads.upload_token")

TOKEN = "github_pat_FAKE0123456789_abcdef"
UUID_URL = "https://github.com/user-attachments/assets/123e4567-e89b-12d3-a456-426614174000"
PNG = upload_token.one_pixel_png()
SHEBANG = "#!/usr/bin/env python3\n"


REAL_MANIFEST = PLUGIN.parent / "factory-secret-names" / "plugin.yaml"


def secret_names_manifest(*names: str) -> str:
    return "optional_env:\n" + "".join(f"  - name: {name}\n    description: example\n" for name in names)


def with_interpreter(script: str) -> str:
    """Pin a fake script to the interpreter running the tests.

    `env python3` follows the PATH the script is given. On macOS that can
    reach /usr/bin/python3, a shim that adds SDKROOT, CPATH, LIBRARY_PATH, and
    MANPATH to the environment the fake then records.
    """
    assert script.startswith(SHEBANG)
    return f"#!{sys.executable}\n" + script[len(SHEBANG):]


class QuickServer(HTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind calls getfqdn, which is slow on some networks.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.socket.getsockname()[1]


class FakeUploads:
    """A local stand-in for the upload endpoint. Records every request it gets."""

    def __init__(self, status=201, body=None, headers=None):
        self.requests = []
        self.reply = (status, json.dumps(body if body is not None else {"url": UUID_URL}), headers or {})
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(length)}
                )
                status, body, headers = outer.reply
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body.encode())

            do_GET = do_POST  # a followed 302 or 303 turns the POST into a GET

            def log_message(self, *args):
                pass

        self.server = QuickServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.allow = mock.patch.object(
            upload_token, "ALLOWED_ENDPOINTS", frozenset({self.base + upload_token.UPLOAD_PATH})
        )
        self.allow.start()
        return self

    def __exit__(self, *exc):
        self.allow.stop()
        self.server.shutdown()
        self.server.server_close()
