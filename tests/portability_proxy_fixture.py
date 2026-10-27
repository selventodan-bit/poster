"""Actual local HTTP/HTTPS/SOCKS5 proxy endpoints; never log private headers."""

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import select
import socket
import socketserver
import ssl
import threading
from urllib.parse import urlsplit


def tunnel(left, right):
    try:
        while True:
            ready, _, _ = select.select([left, right], [], [], 3)
            if not ready:
                return
            for source in ready:
                raw = source.recv(65536)
                if not raw:
                    return
                (right if source is left else left).sendall(raw)
    except OSError:
        pass


class HTTPProxy(BaseHTTPRequestHandler):
    def authorized(self):
        expected = "Basic " + base64.b64encode(b"poster:PROXY_PRIVATE_CANARY").decode()
        if self.server.auth and self.headers.get("Proxy-Authorization") != expected:
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="CI fixture"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        self.server.hits += 1
        return True

    def do_CONNECT(self):
        if not self.authorized():
            return
        host, port = self.path.rsplit(":", 1)
        with socket.create_connection((host.strip("[]"), int(port)), timeout=5) as upstream:
            self.send_response(200)
            self.end_headers()
            tunnel(self.connection, upstream)

    def do_GET(self):
        if not self.authorized():
            return
        target = urlsplit(self.path)
        if target.hostname != "127.0.0.1":
            self.send_error(403)
            return
        with socket.create_connection((target.hostname, target.port), timeout=5) as upstream:
            upstream.sendall((f"GET {target.path or '/'} HTTP/1.0\r\nHost: {target.netloc}\r\n\r\n").encode())
            while raw := upstream.recv(65536):
                self.wfile.write(raw)

    def log_message(self, *_):
        pass


class Socks(socketserver.BaseRequestHandler):
    def handle(self):
        connection = self.request
        connection.settimeout(5)

        def read(size):
            result = b""
            while len(result) < size:
                raw = connection.recv(size - len(result))
                if not raw:
                    raise OSError()
                result += raw
            return result

        try:
            assert read(1) == b"\x05"
            methods = read(read(1)[0])
            method = 2 if self.server.auth else 0
            if method not in methods:
                connection.sendall(b"\x05\xff")
                return
            connection.sendall(bytes([5, method]))
            if method == 2:
                assert read(1) == b"\x01"
                user, password = read(read(1)[0]), read(read(1)[0])
                okay = user == b"poster" and password == b"PROXY_PRIVATE_CANARY"
                connection.sendall(bytes([1, 0 if okay else 1]))
                if not okay:
                    return
            header = read(4)
            assert header[:3] == b"\x05\x01\x00"
            if header[3] == 1:
                host = socket.inet_ntoa(read(4))
            elif header[3] == 3:
                host = read(read(1)[0]).decode()
            else:
                return
            port = int.from_bytes(read(2), "big")
            if host != "127.0.0.1":
                return
            with socket.create_connection((host, port), timeout=5) as upstream:
                self.server.hits += 1
                connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                tunnel(connection, upstream)
        except (OSError, AssertionError):
            pass


class ThreadServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextmanager
def proxy(kind, tmp_path, *, auth=True):
    server = (
        ThreadServer(("127.0.0.1", 0), Socks)
        if kind == "socks5"
        else ThreadingHTTPServer(("127.0.0.1", 0), HTTPProxy)
    )
    server.auth, server.hits = auth, 0
    config = dict(kind=kind, host="127.0.0.1", port=server.server_address[1])
    if auth:
        config.update(username="poster", password="PROXY_PRIVATE_CANARY")
    if kind == "https":
        from poster.remote.setup import tls_files

        cert, key = tls_files(tmp_path / "tls-proxy", "127.0.0.1")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        config["ca"] = cert.read_text()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, config
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
