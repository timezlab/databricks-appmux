"""Spike 6, the neighbour: a second App of the same workspace that reports its own pod address and runs a TCP echo, so the probe's
container can try one address that is known to be ours. Not part of appmux."""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ECHO_PORT = 47200
CONNECT_TIMEOUT_S = 3.0


def try_connect(host: str, port: int) -> str:
    """One word for what a connect did, so the driver can tell a closed port from a silent drop."""
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S):
            return "connected"
    except OSError as exc:
        return type(exc).__name__


def own_address() -> str:
    with open("/proc/net/route") as handle:
        for line in handle.read().splitlines()[1:]:
            fields = line.split()
            if fields[1] == "00000000":
                gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.connect((gateway, 9))
                    return probe.getsockname()[0]
    return "unknown"


def echo(connection: socket.socket) -> None:
    with connection:
        try:
            while data := connection.recv(4096):
                connection.sendall(data)
        except OSError:
            return  # the peer vanished


def serve_echo() -> None:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", ECHO_PORT))
    listener.listen(16)
    while True:
        connection, _ = listener.accept()
        threading.Thread(target=echo, args=(connection,), daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/selftest":  # control: the listeners answer on the pod address, so a silent miss elsewhere is the network
            result = {str(port): try_connect(own_address(), port) for port in (ECHO_PORT, PORT)}
        elif url.path == "/reach":  # the reverse direction: this App to an address the driver names
            result = {"result": try_connect(query["host"][0], int(query["port"][0]))}
        else:
            result = {"ip": own_address(), "echo_port": ECHO_PORT, "app_port": PORT}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


PORT = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))

if __name__ == "__main__":
    threading.Thread(target=serve_echo, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
