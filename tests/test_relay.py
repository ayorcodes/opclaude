from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opclaude_relay as relay  # noqa: E402

TOKEN = "sk-ant-oat-TEST"
MASTER = "sk-master-TEST"
SERVED = frozenset({"claude-glm-5.2", "claude-or-glm-5.3", "claude-kimi"})
CONFIG = """model_list:
- model_name: claude-glm-5.2
  litellm_params: {}
- model_name: 'claude-or-glm-5.3'
- model_name: claude-kimi
"""


def body(first, *later, model="claude-sonnet-4-6"):
    msgs = [{"role": "user", "content": first}] + [{"role": "user", "content": t} for t in later]
    return {"model": model, "messages": msgs}


class RouteNameTest(unittest.TestCase):
    def test_string_and_list_content(self):
        self.assertEqual(relay.route_name(body("Route: glm-5.2\nwork")), "glm-5.2")
        blocks = [{"type": "image"}, {"type": "text", "text": "x\n  Route: or-glm-5.3  \n"}]
        self.assertEqual(relay.route_name(body(blocks)), "or-glm-5.3")

    def test_line_eleven_is_not_routed(self):
        self.assertEqual(relay.route_name(body("\n" * 9 + "Route: kimi")), "kimi")
        self.assertIsNone(relay.route_name(body("\n" * 10 + "Route: kimi")))

    def test_later_message_does_not_route(self):
        self.assertIsNone(relay.route_name(body("hello", "Route: glm-5.2")))

    def test_shape_mismatch_returns_none(self):
        for b in ({}, {"messages": []}, {"messages": "x"}, {"messages": [{"content": 3}]},
                  {"messages": [{"content": [{"type": "image"}]}]}, {"messages": [None]}):
            self.assertIsNone(relay.route_name(b))


class DecideTest(unittest.TestCase):
    def test_routing_table(self):
        routed = json.dumps(body("Route: glm-5.2")).encode()
        cases = [
            ("POST", "/v1/messages", routed, "routed", "claude-glm-5.2"),
            ("POST", "/v1/messages?beta=true", routed, "routed", "claude-glm-5.2"),
            ("POST", "/v1/messages/count_tokens?beta=true", routed, "routed", "claude-glm-5.2"),
            ("POST", "/v1/complete", routed, "anthropic", None),
            ("GET", "/v1/messages", routed, "anthropic", None),
            ("POST", "/v1/messages", json.dumps(body("Route: nope")).encode(), "reject", None),
            ("POST", "/v1/messages", json.dumps(body("hi", model="claude-kimi")).encode(), "litellm", "claude-kimi"),
            ("POST", "/v1/messages", json.dumps(body("hi")).encode(), "anthropic", "claude-sonnet-4-6"),
            ("POST", "/v1/messages", b"{not json", "anthropic", None),
        ]
        for method, path, raw, branch, model_out in cases:
            with self.subTest(path=path, branch=branch):
                d = relay.decide(method, path, raw, SERVED)
                self.assertEqual(d.branch, branch)
                self.assertEqual(d.model_out, model_out)
                if branch == "routed":
                    self.assertEqual(json.loads(d.body_out)["model"], "claude-glm-5.2")
                else:
                    self.assertIs(d.body_out, raw)

    def test_reject_names_valid_short_names(self):
        d = relay.decide("POST", "/v1/messages", json.dumps(body("Route: nope")).encode(), SERVED)
        self.assertIn("glm-5.2, kimi, or-glm-5.3", d.error)


class ServedModelsTest(unittest.TestCase):
    def test_parse_missing_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            self.assertEqual(relay.served_models(path), frozenset())
            path.write_text(CONFIG)
            self.assertEqual(relay.served_models(path), SERVED)
            cache = relay.ServedModels(path)
            self.assertEqual(cache.get(), SERVED)
            path.write_text("- model_name: claude-new\n")
            os.utime(path, (time.time() + 5, time.time() + 5))
            self.assertEqual(cache.get(), frozenset({"claude-new"}))


class ForwardHeadersTest(unittest.TestCase):
    def test_anthropic_untouched_and_routed_stripped(self):
        h = {"Authorization": f"Bearer {TOKEN}", "X-Api-Key": TOKEN, "anthropic-Beta": "a,b",
             "Host": "x", "Connection": "close", "Content-Length": "3", "Accept-Encoding": "gzip"}
        self.assertEqual(relay.forward_headers(h, "anthropic", MASTER),
                         [("Authorization", f"Bearer {TOKEN}"), ("X-Api-Key", TOKEN), ("anthropic-Beta", "a,b")])
        for branch in ("routed", "litellm"):
            self.assertEqual(relay.forward_headers(h, branch, MASTER),
                             [("anthropic-Beta", "a,b"), ("Authorization", f"Bearer {MASTER}")])


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeUpstream:
    """Records requests; answers JSON, or two SSE events with a gated gap."""

    def __init__(self):
        self.requests = []
        self.release = threading.Event()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append((self.path, list(self.headers.items()), raw))
                if json.loads(raw).get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b"event: one\ndata: {}\n\n")
                    self.wfile.flush()
                    outer.release.wait(5)
                    self.wfile.write(b"event: two\ndata: {}\n\n")
                    return
                out = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class RelayServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "config.yaml").write_text(CONFIG)
        self.log = tmp / "relay.log"
        self.anthropic = FakeUpstream()
        self.litellm = FakeUpstream()
        self.start_relay(self.litellm.base)
        env = mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": MASTER})
        env.start()
        self.addCleanup(env.stop)

    def start_relay(self, litellm_base):
        self.relay = relay.make_server(0, anthropic_base=self.anthropic.base, litellm_base=litellm_base,
                                       config_path=Path(self.tmp.name) / "config.yaml", log_path=self.log)
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()

    def tearDown(self):
        self.relay.shutdown()
        self.relay.server_close()
        self.anthropic.close()
        self.litellm.close()
        self.tmp.cleanup()

    def post(self, payload, path="/v1/messages?beta=true", raw=None, headers=()):
        conn = http.client.HTTPConnection("127.0.0.1", self.relay.server_address[1], timeout=10)
        data = raw if raw is not None else json.dumps(payload).encode()
        conn.request("POST", path, data, {"Authorization": f"Bearer {TOKEN}", "anthropic-version": "2023-06-01",
                                          **dict(headers)})
        return conn.getresponse()

    def test_foreign_host_is_400_and_reaches_no_upstream(self):
        port = self.relay.server_address[1]
        for host in (f"evil.example:{port}", "evil.example", f"127.0.0.1:{port + 1}"):
            with self.subTest(host=host):
                resp = self.post(body("Route: glm-5.2"), headers={"Host": host})
                self.assertEqual(resp.status, 400)
                self.assertEqual(json.loads(resp.read())["error"]["type"], "invalid_request_error")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/_relay/health", headers={"Host": "evil.example"})
        self.assertEqual(conn.getresponse().status, 400)
        self.assertEqual(self.anthropic.requests + self.litellm.requests, [])
        resp = self.post(body("Route: glm-5.2"), headers={"Host": f"localhost:{port}"})
        self.assertEqual(resp.status, 200)

    def test_malformed_content_length_is_400(self):
        for length in ("abc", "-1"):
            with self.subTest(length=length):
                with socket.create_connection(self.relay.server_address, timeout=5) as s:
                    s.sendall(f"POST /v1/messages HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                              f"Content-Length: {length}\r\n\r\n".encode())
                    self.assertIn(b" 400 ", s.recv(4096).split(b"\r\n")[0] + b" ")
        self.assertEqual(self.anthropic.requests + self.litellm.requests, [])

    def test_anthropic_branch_is_byte_identical_with_token(self):
        raw = b'{"model": "claude-sonnet-4-6",  "messages": [{"role":"user","content":"SECRET-BODY"}]}'
        resp = self.post(None, raw=raw)
        self.assertEqual((resp.status, resp.read()), (200, b'{"ok":true}'))
        path, headers, got = self.anthropic.requests[0]
        self.assertEqual(path, "/v1/messages?beta=true")
        self.assertEqual(got, raw)
        self.assertIn(("Authorization", f"Bearer {TOKEN}"), headers)
        self.assertEqual(self.litellm.requests, [])

    def test_routed_branch_carries_master_key_only(self):
        resp = self.post(body("Route: glm-5.2\nSECRET-BODY"), path="/v1/messages/count_tokens")
        self.assertEqual(resp.status, 200)
        resp.read()
        path, headers, got = self.litellm.requests[0]
        self.assertEqual(path, "/v1/messages/count_tokens")
        self.assertNotIn("sk-ant", repr(headers))
        self.assertIn(("Authorization", f"Bearer {MASTER}"), headers)
        self.assertEqual(json.loads(got)["model"], "claude-glm-5.2")
        self.assertEqual(self.anthropic.requests, [])
        deadline = time.monotonic() + 5
        while not self.log.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        log = self.log.read_text()
        self.assertNotIn("sk-ant", log)
        self.assertNotIn("SECRET-BODY", log)
        entry = json.loads(log.splitlines()[-1])
        self.assertEqual((entry["branch"], entry["model_out"], entry["status"], entry["path"]),
                         ("routed", "claude-glm-5.2", 200, "/v1/messages/count_tokens"))

    def test_unknown_route_is_400(self):
        resp = self.post(body("Route: nope"))
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(resp.read())["error"]["type"], "invalid_request_error")
        self.assertEqual(self.anthropic.requests + self.litellm.requests, [])

    def test_litellm_down_is_400_and_anthropic_still_200(self):
        self.relay.shutdown()
        self.relay.server_close()
        self.start_relay(f"http://127.0.0.1:{free_port()}")
        resp = self.post(body("Route: glm-5.2"))
        self.assertEqual(resp.status, 400)
        self.assertIn("is down", json.loads(resp.read())["error"]["message"])
        self.assertEqual(self.anthropic.requests, [])
        resp = self.post(body("hi"))
        self.assertEqual(resp.status, 200)

    def test_missing_master_key_is_400_for_routed_only(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            resp = self.post(body("Route: glm-5.2"))
            self.assertEqual(resp.status, 400)
            self.assertIn("LITELLM_MASTER_KEY unset", resp.read().decode())
            self.assertEqual(self.post(body("hi")).status, 200)

    def test_sse_streams_before_upstream_eof(self):
        payload = body("hi")
        payload["stream"] = True
        resp = self.post(payload)
        self.assertEqual(resp.status, 200)
        first = resp.read1(65536)
        self.assertIn(b"event: one", first)
        self.assertNotIn(b"event: two", first)
        self.anthropic.release.set()
        self.assertIn(b"event: two", resp.read())


if __name__ == "__main__":
    unittest.main()
