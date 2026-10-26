"""Opaque bounded HTTPS mailboxes. No GraphRunner, queue, project keys or dispatch decisions."""

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import ssl
import threading
import time

import httpx

from poster.dispatcher.models import DispatcherError
from .protocol import raw_json, MAX_MESSAGE
from .registry import profile

MAX_HTTP = MAX_MESSAGE + 1_000_000


class Client:
    def __init__(self, configuration, role):
        self.config = profile(configuration)
        self.role = role
        context = ssl.create_default_context(cafile=self.config.get("ca"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.http = httpx.Client(verify=context, timeout=5, follow_redirects=False)

    def close(self):
        self.http.close()

    def exchange(self, messages=(), acknowledgements=()):
        try:
            body = raw_json(
                dict(
                    protocol=1,
                    channel=self.config["channel"],
                    role=self.role,
                    messages=list(messages),
                    acks=list(acknowledgements),
                )
            )
            if len(body) > MAX_HTTP:
                raise ValueError()
            response = self.http.post(
                self.config["url"].rstrip("/") + "/v1/exchange",
                content=body,
                headers={
                    "Authorization": "Bearer " + self.config["token"],
                    "Content-Type": "application/json",
                },
            )
            if response.status_code != 200 or len(response.content) > MAX_HTTP:
                raise ValueError()
            value = response.json()
            if (
                value["protocol"] != 1
                or not isinstance(value["messages"], list)
                or len(value["messages"]) > 16
            ):
                raise ValueError()
            return value["messages"]
        except Exception as exc:
            raise DispatcherError("REMOTE_TRANSPORT") from exc


def server(configuration, database, cert, key, *, host="127.0.0.1", port=0):
    """TLS on accepted sockets with handshake/read deadlines; no HTTP cleartext mode."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    lock = threading.Lock()
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS mailbox(id TEXT PRIMARY KEY,channel TEXT,recipient TEXT,packet BLOB,expires REAL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS mailbox_recipient ON mailbox(channel,recipient)")
    credentials = configuration["channels"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            status, answer = 400, {"code": "REMOTE_PROTOCOL"}
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    self.path != "/v1/exchange"
                    or self.headers.get("Transfer-Encoding")
                    or not 0 < length <= MAX_HTTP
                ):
                    raise ValueError()
                value = json.loads(self.rfile.read(length))
                channel, role = value["channel"], value["role"]
                if value["protocol"] != 1 or role not in {"agent", "controller"}:
                    raise ValueError()
                expected = credentials.get(channel, {}).get(role, "")
                token = self.headers.get("Authorization", "")
                if not expected or not hmac.compare_digest(
                    hashlib.sha256(token.removeprefix("Bearer ").encode()).hexdigest(), expected
                ):
                    status = 403
                    raise ValueError()
                if len(value["messages"]) > 8 or len(value["acks"]) > 32:
                    raise ValueError()
                recipient = "controller" if role == "agent" else "agent"
                with lock, sqlite3.connect(database) as db:
                    db.execute("DELETE FROM mailbox WHERE expires<?", (time.time(),))
                    for message_id in value["acks"]:
                        db.execute(
                            "DELETE FROM mailbox WHERE id=? AND channel=? AND recipient=?",
                            (message_id, channel, role),
                        )
                    for packet in value["messages"]:
                        raw = raw_json(packet)
                        if (
                            len(raw) > MAX_MESSAGE
                            or not time.time() - 3 < packet["expires"] < time.time() + 12
                        ):
                            raise ValueError()
                        size = db.execute(
                            "SELECT coalesce(sum(length(packet)),0),count(*) FROM mailbox WHERE channel=?",
                            (channel,),
                        ).fetchone()
                        if size[0] + len(raw) > 128_000_000 or size[1] >= 128:
                            status = 429
                            raise ValueError()
                        db.execute(
                            "INSERT OR IGNORE INTO mailbox VALUES(?,?,?,?,?)",
                            (packet["id"], channel, recipient, raw, packet["expires"]),
                        )
                    packets = []
                    size = 0
                    for (raw,) in db.execute(
                        "SELECT packet FROM mailbox WHERE channel=? AND recipient=? ORDER BY rowid LIMIT 16",
                        (channel, role),
                    ):
                        if size + len(raw) > MAX_MESSAGE:
                            break
                        packets.append(json.loads(raw))
                        size += len(raw)
                    answer = {"protocol": 1, "messages": packets}
                    status = 200
            except Exception:
                pass
            raw = raw_json(answer)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def get_request(self):
            sock, address = super().get_request()
            sock.settimeout(5)
            try:
                return context.wrap_socket(sock, server_side=True), address
            except Exception:
                sock.close()
                raise

    return Server((host, port), Handler)
