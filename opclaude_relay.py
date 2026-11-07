#!/usr/bin/env python3
"""Local relay in front of Claude Code: subscription traffic passes to Anthropic
untouched; requests whose first message carries a `Route: <name>` line, or whose
model is served by LiteLLM, go to the local LiteLLM proxy with its master key.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

RELAY_HOST = "127.0.0.1"
RELAY_PORT = int(os.environ.get("OPCLAUDE_RELAY_PORT", "4001"))
LITELLM = "http://127.0.0.1:4000"
ANTHROPIC = "https://api.anthropic.com"
ROUTE_LINE = re.compile(r"^[ \t]*Route: ([\w.:-]+)[ \t]*$")
ROUTE_SCAN_LINES = 10
HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length", "accept-encoding",
}
MESSAGE_PATHS = {"/v1/messages", "/v1/messages/count_tokens"}
CREDENTIAL_HEADERS = {"authorization", "x-api-key"}
MODEL_NAME = re.compile(r"""^-?\s*model_name:\s*['"]?([^'"\s]+)""", re.M)
LOG_MAX_BYTES = 10 * 1024 * 1024
UPSTREAM_TIMEOUT = 600
CHUNK = 65536

REPO_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = REPO_DIR / "config.generated.yaml"
DEFAULT_LOG = Path.home() / ".config" / "opclaude" / "relay.log"
LITELLM_DOWN = "opclaude LiteLLM proxy at 127.0.0.1:4000 is down; run opclaude-proxy start"


@dataclass(frozen=True)
class Decision:
    branch: str  # "anthropic" | "routed" | "litellm" | "reject"
    model_in: str | None
    model_out: str | None
    body_out: bytes
    error: str | None = None


def route_name(body: dict) -> str | None:
    try:
        content = body["messages"][0]["content"]
        if isinstance(content, list):
            content = next(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(content, str):
            return None
    except (KeyError, IndexError, TypeError, StopIteration):
        return None
    for line in content.splitlines()[:ROUTE_SCAN_LINES]:
        m = ROUTE_LINE.match(line)
        if m:
            return m.group(1)
    return None


class ServedModels:
    """model_name set from the LiteLLM config, reloaded when its mtime changes."""

    def __init__(self, config_path: Path):
        self.path = Path(config_path)
        self._mtime: float | None = None
        self._models: frozenset[str] = frozenset()
        self._lock = threading.Lock()

    def get(self) -> frozenset[str]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return frozenset()
        with self._lock:
            if mtime != self._mtime:
                self._models = served_models(self.path)
                self._mtime = mtime
            return self._models


def served_models(config_path) -> frozenset[str]:
    try:
        text = Path(config_path).read_text()
    except OSError:
        return frozenset()
    return frozenset(MODEL_NAME.findall(text))


def decide(method: str, path: str, body_bytes: bytes, served: frozenset[str]) -> Decision:
    if method != "POST" or urlsplit(path).path not in MESSAGE_PATHS:
        return Decision("anthropic", None, None, body_bytes)
    try:
        body = json.loads(body_bytes)
    except ValueError:
        return Decision("anthropic", None, None, body_bytes)
    if not isinstance(body, dict):
        return Decision("anthropic", None, None, body_bytes)
    model_in = body.get("model") if isinstance(body.get("model"), str) else None
    name = route_name(body)
    if name is not None:
        target = f"claude-{name}"
        if target not in served:
            valid = ", ".join(sorted(m.removeprefix("claude-") for m in served)) or "(none)"
            return Decision("reject", model_in, None, body_bytes,
                            f"unknown Route: {name}; valid names: {valid}")
        body["model"] = target
        return Decision("routed", model_in, target, json.dumps(body).encode())
    if model_in in served:
        return Decision("litellm", model_in, model_in, body_bytes)
    return Decision("anthropic", model_in, model_in, body_bytes)


def forward_headers(headers, branch: str, master_key: str | None) -> list[tuple[str, str]]:
    out = [(k, v) for k, v in headers.items() if k.lower() not in HOP]
    if branch == "anthropic":
        return out
    out = [(k, v) for k, v in out if k.lower() not in CREDENTIAL_HEADERS]
    out.append(("Authorization", f"Bearer {master_key}"))
    return out


def error_body(kind: str, message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()


class RelayLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, **fields) -> None:
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.stat().st_size > LOG_MAX_BYTES:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            except FileNotFoundError:
                pass
            with open(self.path, "a") as f:
                f.write(line)


def _connection(base: str) -> http.client.HTTPConnection:
    u = urlsplit(base)
    if u.scheme == "https":
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=UPSTREAM_TIMEOUT)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=UPSTREAM_TIMEOUT)


def make_server(port: int, anthropic_base: str = ANTHROPIC, litellm_base: str = LITELLM,
                config_path: Path = DEFAULT_CONFIG, log_path: Path = DEFAULT_LOG) -> ThreadingHTTPServer:
    served = ServedModels(config_path)
    log = RelayLog(log_path)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format, *args):  # silence default stderr access log
            pass

        def _send_json(self, status: int, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _health(self) -> None:
            try:
                with urllib.request.urlopen(f"{litellm_base}/health/liveliness", timeout=1):
                    litellm = "up"
            except OSError:
                litellm = "down"
            self._send_json(200, json.dumps({"relay": "up", "litellm": litellm}).encode())

        def _handle(self) -> None:
            port = self.server.server_address[1]
            if self.headers.get("Host", "").lower() not in {
                    "127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"}:
                return self._send_json(400, error_body("invalid_request_error", "relay accepts only local Host"))
            if self.command == "GET" and self.path == "/_relay/health":
                return self._health()
            started = time.monotonic()
            d = None
            status: int | str = "error"
            try:
                length = self.headers.get("Content-Length")
                if length is None and "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                    status = 411
                    return self._send_json(411, error_body("invalid_request_error", "Content-Length required"))
                try:
                    size = int(length) if length else 0
                except ValueError:
                    size = -1
                if size < 0:
                    status = 400
                    return self._send_json(400, error_body("invalid_request_error", "invalid Content-Length"))
                body = self.rfile.read(size)
                d = decide(self.command, self.path, body, served.get())
                status = self._dispatch(d)
            finally:
                log.write(method=self.command, path=urlsplit(self.path).path,
                          branch=d.branch if d else None,
                          model_in=d.model_in if d else None,
                          model_out=d.model_out if d else None,
                          status=status, ms=round((time.monotonic() - started) * 1000))

        def _dispatch(self, d: Decision) -> int | str:
            if d.branch == "reject":
                self._send_json(400, error_body("invalid_request_error", d.error))
                return 400
            to_anthropic = d.branch == "anthropic"
            master_key = os.environ.get("LITELLM_MASTER_KEY")
            if not to_anthropic and not master_key:
                self._send_json(400, error_body("invalid_request_error", "LITELLM_MASTER_KEY unset"))
                return 400
            conn = _connection(anthropic_base if to_anthropic else litellm_base)
            try:
                try:
                    conn.connect()
                except OSError:
                    if to_anthropic:
                        self._send_json(502, error_body("api_error", "relay could not reach Anthropic"))
                        return 502
                    self._send_json(400, error_body("invalid_request_error", LITELLM_DOWN))
                    return 400
                try:
                    conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                    conn.putheader("Host", urlsplit(anthropic_base if to_anthropic else litellm_base).netloc)
                    for k, v in forward_headers(self.headers, d.branch, master_key):
                        conn.putheader(k, v)
                    if d.body_out or self.command in ("POST", "PUT", "PATCH"):
                        conn.putheader("Content-Length", str(len(d.body_out)))
                    conn.endheaders(d.body_out or None)
                    resp = conn.getresponse()
                except OSError:
                    self._send_json(502, error_body("api_error", "relay upstream request failed"))
                    return 502
                try:
                    self.send_response(resp.status, resp.reason)
                    for k, v in resp.getheaders():
                        if k.lower() not in HOP:
                            self.send_header(k, v)
                    if resp.getheader("Content-Length") is not None:
                        self.send_header("Content-Length", resp.getheader("Content-Length"))
                    self.end_headers()
                    while chunk := resp.read1(CHUNK):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return "client_closed"
                return resp.status
            finally:
                conn.close()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle

    server = ThreadingHTTPServer((RELAY_HOST, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    server = make_server(RELAY_PORT)
    print(f"opclaude relay listening on http://{RELAY_HOST}:{RELAY_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
