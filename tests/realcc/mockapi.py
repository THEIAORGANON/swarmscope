"""Scripted Anthropic Messages API for a real Claude Code binary (no model, no key).

Script (JSON, env MOCK_SCRIPT): {"main": [call, ...], "sub": [call, ...]} where a call is
{"name": tool, "input": {...}}. A request whose tools include "Task" or "Agent" is the main session, any
other is a subagent. Each side's next call is the number of tool_result blocks it has received so far; past
the end it answers with text "done". Every tool_result received is logged to MOCK_LOG as JSON lines.
"""
import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = json.loads(os.environ["MOCK_SCRIPT"])
LOG = os.environ["MOCK_LOG"]


def results_in(body):
    out = []
    for m in body.get("messages") or []:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            out += [b for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    return out


def sse(h, ev, data):
    h.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode())


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not self.path.startswith("/v1/messages") or self.path.startswith("/v1/messages/count_tokens"):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"input_tokens": 10}).encode())
            return
        tools = [t.get("name") for t in body.get("tools") or []]
        side = "main" if ("Task" in tools or "Agent" in tools) else "sub"
        res = results_in(body)
        with open(LOG, "a") as f:
            f.write(json.dumps({"side": side, "tools": tools, "results": res[-1:] if res else []}) + "\n")
        calls = SCRIPT.get(side) or []
        k = len(res)
        call = calls[k] if k < len(calls) and tools else None
        model = body.get("model", "claude-sonnet-5-5")
        mid = "msg_" + uuid.uuid4().hex[:20]
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
        if call:
            content = [{"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": call["name"],
                        "input": call["input"]}]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "done"}]
            stop = "end_turn"
        if not body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": mid, "type": "message", "role": "assistant", "model": model,
                                         "content": content, "stop_reason": stop, "usage": usage}).encode())
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        sse(self, "message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "usage": usage}})
        for i, b in enumerate(content):
            if b["type"] == "text":
                sse(self, "content_block_start", {"type": "content_block_start", "index": i,
                                                  "content_block": {"type": "text", "text": ""}})
                sse(self, "content_block_delta", {"type": "content_block_delta", "index": i,
                                                  "delta": {"type": "text_delta", "text": b["text"]}})
            else:
                sse(self, "content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
                sse(self, "content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
            sse(self, "content_block_stop", {"type": "content_block_stop", "index": i})
        sse(self, "message_delta", {"type": "message_delta", "delta": {"stop_reason": stop},
                                    "usage": {"output_tokens": 5}})
        sse(self, "message_stop", {"type": "message_stop"})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H)
    srv.serve_forever()
