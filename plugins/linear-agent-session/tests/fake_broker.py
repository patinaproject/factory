from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

UNKNOWN_OP = {"ok": False, "state": "refused", "message": "unknown op"}


class FakeBroker:
    """`answers` maps each op to its replies, given in order; the last reply repeats.

    A reply is a dict sent as one JSON line, bytes sent as they are, or None to close without an answer. A request
    that names a `size` is followed by that many payload bytes.
    """

    def __init__(self, path, **answers: list) -> None:
        self.path = Path(path)
        self.answers = {op: list(replies) for op, replies in answers.items()}
        self.requests: list = []
        self.bodies: list = []
        self.payloads: list = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client, client.makefile("rwb") as stream:
                body = json.loads(stream.readline())
                payload = stream.read(body["size"]) if "size" in body else b""
                self.requests.append(body["op"])
                self.bodies.append(body)
                self.payloads.append(payload)
                replies = self.answers.get(body["op"]) or [UNKNOWN_OP]
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if reply is not None:
                    stream.write(reply if isinstance(reply, bytes) else json.dumps(reply).encode() + b"\n")

    def close(self) -> None:
        self.server.close()
        self.path.unlink(missing_ok=True)
