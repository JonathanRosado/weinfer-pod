#!/usr/bin/env python3
"""REST-v1-shaped RunPod fake for the deploy-tail regression.

Covers exactly the surface deploy_controlplane.sh touches:
  GET  /networkvolumes           -> array
  POST /networkvolumes           -> {"id": ...}
  POST /pods                     -> scenario-controlled create
  GET  /pods                     -> array of pods
  GET  /pods/{id}                -> pod or 404
  DELETE /pods/{id}              -> EXITED first, TERMINATED second
  GET  /healthz                  -> 200 (the test-mode health base)
Scenario control:
  POST /control/mode    {"mode": "ok"|"lost"|"norate"|"badrate"|"highrate",
                         "list_delay": N}   # pod invisible for N list reads
  POST /control/outage  {"count": N}        # next N list calls 500
Deletes are logged in order to /tmp/fake-v1-deletes.log.
"""
import http.server
import json
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18992
DELETES = "/tmp/fake-v1-deletes.log"
BODIES = "/tmp/fake-v1-bodies.jsonl"
STATE = {"mode": "ok", "list_delay": 0, "outage": 0, "counter": 0}
PODS = {}
VOLUMES = {}


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
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        elif self.path.startswith("/networkvolumes"):
            self._send(200, list(VOLUMES.values()))
        elif self.path.startswith("/pods/"):
            pod = PODS.get(self.path.rsplit("/", 1)[1])
            if pod is None:
                self._send(404, {"detail": "not found"})
            else:
                self._send(200, {k: v for k, v in pod.items() if k != "hidden_reads"})
        elif self.path.startswith("/pods"):
            if STATE["outage"] > 0:
                STATE["outage"] -= 1
                self._send(500, {"detail": "synthetic outage"})
                return
            visible = []
            for pod in PODS.values():
                if pod["hidden_reads"] > 0:
                    pod["hidden_reads"] -= 1
                    continue
                visible.append({k: v for k, v in pod.items() if k != "hidden_reads"})
            self._send(200, visible)
        else:
            self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/control/mode":
            STATE.update(body)
            self._send(200, {})
        elif self.path == "/control/outage":
            STATE["outage"] = body["count"]
            self._send(200, {})
        elif self.path == "/networkvolumes":
            vol_id = f"vol-{len(VOLUMES) + 1}"
            VOLUMES[vol_id] = {"id": vol_id, "name": body.get("name", ""),
                               "size": body.get("size", 0)}
            self._send(200, VOLUMES[vol_id])
        elif self.path == "/pods":
            with open(BODIES, "a") as f:
                f.write(json.dumps(body, separators=(",", ":")) + "\n")
            STATE["counter"] += 1
            pod_id = f"cpupod{STATE['counter']}"
            pod = {
                "id": pod_id,
                "name": body.get("name", ""),
                "status": "RUNNING",
                "costPerHr": 0.07,
                "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "hidden_reads": STATE.get("list_delay", 0),
            }
            mode = STATE["mode"]
            if mode == "norate":
                pod.pop("costPerHr")
            elif mode == "badrate":
                pod["costPerHr"] = "garbage"
            elif mode == "highrate":
                pod["costPerHr"] = 0.50
            PODS[pod_id] = pod
            if mode == "lost":
                # The create COMMITTED but the response never arrives.
                self.send_response(500)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            self._send(200, {k: v for k, v in pod.items() if k != "hidden_reads"})
        else:
            self._send(404, {})

    def do_DELETE(self):
        pod_id = self.path.rsplit("/", 1)[1]
        with open(DELETES, "a") as f:
            f.write(pod_id + "\n")
        if pod_id in PODS:
            pod = PODS[pod_id]
            pod["status"] = "TERMINATED" if pod["status"] == "EXITED" else "EXITED"
        self._send(200, {})


http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
