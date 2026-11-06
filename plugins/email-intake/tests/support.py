from __future__ import annotations

import hashlib
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fetch
import ledger

SECRET = "email-route-test-secret"
HOST = "inbox-objects.example.com"
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
RAW = b"From: billing@vendor.example\r\nSubject: Invoice\r\n\r\nYour invoice is attached.\r\n"


def worker_body(message_id="<invoice-1@vendor.example>", **overrides) -> dict:
    body = {
        "message_id": message_id,
        "message_id_source": "header",
        "from": "billing@vendor.example",
        "to": "engineers@example.com",
        "subject": "Your invoice",
        "date": "Fri, 06 Nov 2026 08:00:00 +0000",
        "authentication_results": "spf=pass",
        "text": "Your invoice is attached.",
        "truncated": False,
        "raw": {"key": "raw/invoice-1.eml", "size": len(RAW)},
        "attachments": [
            {
                "filename": "invoice.pdf",
                "content_type": "application/pdf",
                "size": len(PDF),
                "sha256": hashlib.sha256(PDF).hexdigest(),
                "key": "attachments/invoice-1/invoice.pdf",
            }
        ],
        "attachments_truncated": False,
    }
    body.update(overrides)
    return body


class ObjectHost:
    """A fake Worker object host on localhost that checks each GET's signature the way the Worker does."""

    def __init__(self, objects: dict):
        self.objects = objects
        self.requests = []
        host = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                timestamp = self.headers.get("X-Webhook-Timestamp") or ""
                expected = fetch.signature(SECRET, timestamp, self.path)
                signed = self.headers.get("X-Webhook-Signature-V2") == expected
                host.requests.append((self.path, signed))
                data = host.objects.get(self.path.lstrip("/"))
                if not signed or data is None:
                    self.send_response(401 if not signed else 404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def opener(self, request, timeout):
        """Send the https request the service builds to this plain-HTTP server, keeping its path and headers."""
        url = request.full_url.replace(f"https://{HOST}", f"http://127.0.0.1:{self.server.server_address[1]}")
        return urllib.request.urlopen(urllib.request.Request(url, headers=dict(request.header_items())), timeout=timeout)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeBoard:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.cards = {}
        self.comments = []
        self.unblocked = []
        self._lock = threading.Lock()

    def create_card(self, delivery):
        key = ledger.IDEMPOTENCY_PREFIX + delivery.message_id
        with self._lock:
            for card_id, card in self.cards.items():
                if card["key"] == key:
                    return card_id
            card_id = f"t_{len(self.cards) + 1:04d}"
            self.cards[card_id] = {"key": key, "status": "blocked", "assignee": ledger.MAIL_PROFILE}
            return card_id

    def attachments_dir(self, card_id):
        return self.root / "attachments" / card_id

    def unblock(self, card_id):
        self.cards[card_id]["status"] = "ready"
        self.unblocked.append(card_id)

    def comment(self, card_id, body):
        self.comments.append((card_id, body))

    def board(self):
        return fetch.Board(self.create_card, self.attachments_dir, self.unblock, self.comment)
