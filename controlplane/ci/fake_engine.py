#!/usr/bin/env python3
"""OpenAI-compatible fake engine for the CI worker e2e.

The REAL weinfer-worker binary points here instead of vLLM: every
chat completion answers exactly `canary-ok` with fixed usage, and
each request is appended to /tmp/fake-engine-requests.jsonl so the
workflow can prove DISTINCT engine calls per canary run.
"""
import http.server
import json
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18999
LOG = "/tmp/fake-engine-requests.jsonl"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_metrics(self):
        # Match vLLM 0.11's Prometheus WIRE, including the `_total`
        # suffix added by prometheus_client.Counter.  The real worker
        # refuses READY if this effective runtime observation is absent.
        body = (
            'vllm:cache_config_info{block_size="16",'
            'enable_prefix_caching="True",engine="0"} 1.0\n'
            'vllm:prefix_cache_queries_total{engine="0"} 0.0\n'
            'vllm:prefix_cache_hits_total{engine="0"} 0.0\n'
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "text/plain; version=0.0.4")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/metrics":
            self._send_metrics()
            return
        self._send({"object": "list",
                    "data": [{"id": "Qwen/Qwen2.5-7B-Instruct", "object": "model"}]})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n)
        with open(LOG, "ab") as f:
            f.write(raw + b"\n")
        self._send({
            "id": f"chatcmpl-fake-{time.time_ns()}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "canary-ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 32, "completion_tokens": 4, "total_tokens": 36},
        })


http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
