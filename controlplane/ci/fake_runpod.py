#!/usr/bin/env python3
"""Zero-spend RunPod REST v2 fake for the managed control-plane CI.

The live catalog advertises two bootstrap SKUs.  The cheapest
(A5000) is definitively denied at create truth and the A4500 succeeds,
so the real gateway must fall through inside one plan.  Every request
body and delete is recorded to files the workflow asserts on, and
billing reports a covered provisional charge so drain settlement
completes.
"""
import http.server
import json
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18990
BODIES = "/tmp/fake-runpod-bodies.jsonl"
DELETES = "/tmp/fake-runpod-deletes.log"
CREATED = {}
COUNTER = {"n": 0}
open(BODIES, "w").close()
open(DELETES, "w").close()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v2/catalog/gpus"):
            self._send(200, {"gpus": [
                {
                    "id": "NVIDIA RTX A5000", "memory": 24,
                    "community": True, "secure": True,
                    "availability": "LOW",
                    "price": {"community": 0.16},
                    "cudaVersions": [{"version": "12.8", "available": True}],
                },
                {
                    "id": "NVIDIA RTX A4500", "memory": 20,
                    "community": True, "secure": True,
                    "availability": "LOW",
                    "price": {"community": 0.19},
                    "cudaVersions": [{"version": "12.8", "available": True}],
                },
            ]})
        elif self.path.startswith("/v2/billing/pods"):
            self._send(200, {
                "records": [{"endTime": "2999-01-01T00:00:00Z"}],
                "metadata": {"totals": {"totalAmount": 0.0021}},
            })
        elif self.path.startswith("/v2/pods/"):
            pod_id = self.path.rsplit("/", 1)[1]
            pod = CREATED.get(pod_id)
            if pod is None:
                self._send(404, {"detail": "not found"})
            else:
                self._send(200, pod)
        else:
            self._send(200, {"pods": list(CREATED.values())})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n)
        with open(BODIES, "ab") as f:
            f.write(raw + b"\n")
        body = json.loads(raw)
        if body.get("gpu", {}).get("id") == "NVIDIA RTX A5000":
            self._send(400, {
                "error": "There are no longer any instances available with the requested specifications"
            })
            return
        # Monotonic ids, NEVER reused after a delete (the real
        # provider never reuses pod ids; reuse collided with the
        # managed_pods primary key in the drain-reboot e2e).
        COUNTER["n"] += 1
        pod_id = f"fakegpu{COUNTER['n']}"
        CREATED[pod_id] = {
            "id": pod_id,
            "name": body.get("name", ""),
            "desiredStatus": "RUNNING",
            "costPerHr": 0.19,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._send(201, CREATED[pod_id])

    def do_DELETE(self):
        with open(DELETES, "a") as f:
            f.write(self.path + "\n")
        CREATED.pop(self.path.rsplit("/", 1)[1], None)
        self._send(200, {})


http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
