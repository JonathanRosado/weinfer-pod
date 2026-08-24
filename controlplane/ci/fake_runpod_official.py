#!/usr/bin/env python3
"""Official-v2-shaped RunPod fake for the GPU-watchdog regression.

Serves the OFFICIAL list schema (status/cost — not the create-response
desiredStatus/costPerHr variant) so the watchdog is proven against the
documented shape.  The test drives scenarios through /control:
  POST /control/spawn   {"id","name","cost","age_secs"}  (cost may be garbage)
  POST /control/kill    {"id"}          mark TERMINATED without a DELETE
  POST /control/outage  {"count"}       next N list calls return 500
Deletes are recorded in order to /tmp/fake-official-deletes.log.
"""
import http.server
import json
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18991
DELETES = "/tmp/fake-official-deletes.log"
PODS = {}
OUTAGE = {"count": 0}


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


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
        if self.path.startswith("/v2/pods/"):
            pod = PODS.get(self.path.rsplit("/", 1)[1])
            if pod is None:
                self._send(404, {"detail": "not found"})
            else:
                self._send(200, pod)
        elif self.path.startswith("/v2/pods"):
            if OUTAGE["count"] > 0:
                OUTAGE["count"] -= 1
                self._send(500, {"detail": "synthetic outage"})
            else:
                self._send(200, {"pods": list(PODS.values())})
        else:
            self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/control/spawn":
            PODS[body["id"]] = {
                "id": body["id"],
                "name": body["name"],
                "status": "RUNNING",
                "cost": body.get("cost", 0.19),
                "createdAt": iso(time.time() - body.get("age_secs", 0)),
            }
            self._send(200, {})
        elif self.path == "/control/kill":
            PODS[body["id"]]["status"] = "TERMINATED"
            self._send(200, {})
        elif self.path == "/control/outage":
            OUTAGE["count"] = body["count"]
            self._send(200, {})
        else:
            self._send(404, {})

    def do_DELETE(self):
        # Official semantics: the FIRST delete stops the pod (EXITED —
        # machine still assigned); only a follow-up delete terminates.
        # The watchdog/deploy must keep sweeping until TERMINATED.
        pod_id = self.path.rsplit("/", 1)[1]
        with open(DELETES, "a") as f:
            f.write(pod_id + "\n")
        if pod_id in PODS:
            pod = PODS[pod_id]
            pod["status"] = "TERMINATED" if pod["status"] == "EXITED" else "EXITED"
        self._send(200, {})


http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
