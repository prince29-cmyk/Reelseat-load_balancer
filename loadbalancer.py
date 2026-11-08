"""
The load balancer: the "front door" on port 8080.

It chooses a server for every request using one of three strategies:
    round_robin        - take turns: 1, 2, 3, 1, 2, 3 ...
    least_connections  - send to the server that is busy with the fewest requests right now
    weighted           - stronger servers get more: weight 3 gets three times as much as weight 1

It also protects visitors from broken servers:
    - Health checks every 2 seconds. A server that fails is skipped until it passes again.
    - If a server can't be reached, the request is retried on another server.
    - Circuit breaker: if a server returns 3 errors in a row, it is taken out of rotation.
"""

import http.client
import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from app import STATIC_DIR, Backend

HEALTH_CHECK_EVERY = 2
HEALTH_TIMEOUT = 3
ERRORS_BEFORE_DOWN = 3
STRATEGIES = {"round_robin": "Round robin", "least_connections": "Least connections", "weighted": "Weighted"}
MODES = ("normal", "slow", "errors")


class Pool:
    def __init__(self, ports):
        self.backends = [Backend(i + 1, p) for i, p in enumerate(ports)]
        self.by_port = {b.port: b for b in self.backends}
        self.lock = threading.Lock()
        self.strategy = "round_robin"
        self.rr = 0
        self.events = deque(maxlen=60)
        self.stats = {b.port: {"healthy": True, "sent": 0, "active": 0, "errors": 0, "streak": 0,
                               "total_ms": 0.0, "weight": 1, "current": 0} for b in self.backends}

    def log(self, message):
        line = time.strftime("%H:%M:%S") + "  " + message
        self.events.appendleft(line)
        print("  " + line)

    # ---------- choosing a server ----------
    def pick(self, skip):
        with self.lock:
            ready = [b for b in self.backends if self.stats[b.port]["healthy"] and b.port not in skip]
            if not ready:
                return None
            n = len(self.backends)
            if self.strategy == "least_connections":
                start = self.rr
                self.rr += 1
                # fewest active requests wins; if tied, take turns
                chosen = min(ready, key=lambda b: (self.stats[b.port]["active"], (b.number - 1 - start) % n))
            elif self.strategy == "weighted":
                # "smooth weighted round robin" (the method nginx uses)
                total = sum(self.stats[b.port]["weight"] for b in ready)
                for b in ready:
                    self.stats[b.port]["current"] += self.stats[b.port]["weight"]
                chosen = max(ready, key=lambda b: self.stats[b.port]["current"])
                self.stats[chosen.port]["current"] -= total
            else:
                chosen = None
                for _ in range(n):
                    b = self.backends[self.rr % n]
                    self.rr += 1
                    if b in ready:
                        chosen = b
                        break
            self.stats[chosen.port]["active"] += 1
            return chosen

    def finished(self, b, started, ok):
        with self.lock:
            s = self.stats[b.port]
            s["active"] -= 1
            s["sent"] += 1
            s["total_ms"] += (time.time() - started) * 1000
            if ok:
                s["streak"] = 0
                return False
            s["errors"] += 1
            s["streak"] += 1
            return s["streak"] >= ERRORS_BEFORE_DOWN and s["healthy"]

    def set_health(self, b, alive, reason):
        with self.lock:
            s = self.stats[b.port]
            changed = s["healthy"] != alive
            s["healthy"] = alive
            if alive:
                s["streak"] = 0
        if changed:
            if alive:
                self.log(f"{b.name} is back UP ({reason}). Sending it traffic again.")
            else:
                self.log(f"{b.name} is DOWN ({reason}). No more traffic will be sent to it.")

    def health_loop(self):
        while True:
            for b in self.backends:
                threading.Thread(target=self.check_one, args=(b,), daemon=True).start()
            time.sleep(HEALTH_CHECK_EVERY)

    def check_one(self, b):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", b.port, timeout=HEALTH_TIMEOUT)
            conn.request("GET", "/health")
            status = conn.getresponse().status
            conn.close()
            alive, reason = status == 200, ("health check passed" if status == 200 else "health check returned an error")
        except Exception:
            alive, reason = False, "health check got no answer"
        self.set_health(b, alive, reason)

    def status(self):
        with self.lock:
            servers = []
            for b in self.backends:
                s = self.stats[b.port]
                servers.append({"number": b.number, "name": b.name, "port": b.port, "running": b.running,
                                "mode": b.mode, "healthy": s["healthy"], "sent": s["sent"], "active": s["active"],
                                "errors": s["errors"], "weight": s["weight"],
                                "avg_ms": round(s["total_ms"] / s["sent"]) if s["sent"] else None})
            return {"strategy": self.strategy, "strategies": STRATEGIES, "servers": servers, "events": list(self.events),
                    "settings": {"health_every": HEALTH_CHECK_EVERY, "errors_before_down": ERRORS_BEFORE_DOWN}}


pool = Pool([8001, 8002, 8003])


class LoadBalancerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, content_type, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj):
        self.send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    # ---------------- control panel ----------------
    def control(self, path):
        if path == "/admin":
            with open(os.path.join(STATIC_DIR, "admin.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/lb/status" and self.command == "GET":
            return self.send_json(200, pool.status())
        if self.command != "POST" or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.send_json(400, {"error": "Use the control panel"})
        parts = path.strip("/").split("/")          # e.g. lb/mode/8002/slow
        action = parts[1] if len(parts) > 1 else ""

        if action == "strategy" and len(parts) == 3 and parts[2] in STRATEGIES:
            pool.strategy = parts[2]
            pool.log(f"Strategy changed to {STRATEGIES[parts[2]].upper()}.")
            return self.send_json(200, {"ok": True})
        if action == "reset":
            with pool.lock:
                for s in pool.stats.values():
                    s.update(sent=0, errors=0, total_ms=0.0, streak=0)
            pool.log("Counters reset.")
            return self.send_json(200, {"ok": True})

        b = pool.by_port.get(int(parts[2])) if len(parts) >= 3 and parts[2].isdigit() else None
        if not b:
            return self.send_json(404, {"error": "No such server"})
        if action == "stop":
            b.stop()
            pool.log(f"You switched OFF {b.name}. The load balancer has not noticed yet...")
        elif action == "start":
            b.start()
            pool.log(f"You switched ON {b.name}. Waiting for the next health check...")
        elif action == "mode" and len(parts) == 4 and parts[3] in MODES:
            b.mode = parts[3]
            pool.log(f"You put {b.name} into {parts[3].upper()} mode.")
        elif action == "weight" and len(parts) == 4 and parts[3].isdigit() and 1 <= int(parts[3]) <= 5:
            with pool.lock:
                pool.stats[b.port]["weight"] = int(parts[3])
                for s in pool.stats.values():
                    s["current"] = 0
            pool.log(f"{b.name} weight set to {parts[3]}.")
        else:
            return self.send_json(400, {"error": "Unknown action"})
        return self.send_json(200, {"ok": True})

    # ---------------- forwarding ----------------
    def forward(self):
        path = urlparse(self.path).path
        if path == "/admin" or path.startswith("/lb/"):
            return self.control(path)

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
        headers["X-Forwarded-For"] = self.client_address[0]
        can_retry_after_error = self.command == "GET"      # never repeat a booking by accident

        tried, last = set(), None
        while True:
            b = pool.pick(tried)
            if b is None:
                break
            tried.add(b.port)
            started = time.time()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", b.port, timeout=15)
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                conn.close()
            except ConnectionRefusedError:
                # The request never reached the server, so it is always safe to try another one.
                pool.finished(b, started, ok=False)
                pool.set_health(b, False, "it refused a connection")
                pool.log(f"Retrying {self.command} {path} on another server - the visitor won't notice.")
                continue
            except Exception:
                pool.finished(b, started, ok=False)
                pool.set_health(b, False, "it stopped answering")
                if can_retry_after_error:
                    continue
                return self.send_json(502, {"error": "The server stopped answering. Check My bookings before trying again."})

            failed = resp.status >= 500
            if pool.finished(b, started, ok=not failed):
                pool.set_health(b, False, f"circuit breaker: {ERRORS_BEFORE_DOWN} errors in a row")
            last = (resp, data)
            if failed and can_retry_after_error:
                pool.log(f"{b.name} returned an error for {path}. Retrying on another server.")
                continue
            return self.relay(resp, data)

        if last:
            return self.relay(*last)
        pool.log(f"All servers are down! Could not answer {self.command} {path}.")
        if path.startswith("/api/"):
            return self.send_json(503, {"error": "All servers are down. Please try again in a moment."})
        page = ("<!doctype html><meta charset='utf-8'><title>Servers down</title>"
                "<body style='background:#0F1115;color:#EDEEF0;font-family:system-ui,sans-serif;display:grid;"
                "place-items:center;min-height:100vh;margin:0;text-align:center'><div><h1>All servers are down</h1>"
                "<p style='color:#A3A7B1'>The load balancer has no working server to send you to.</p>"
                "<p><a style='color:#F2B33D' href='/admin'>Open the server panel</a> and switch a server back on.</p>"
                "</div></body>")
        self.send(503, page.encode("utf-8"), "text/html; charset=utf-8")

    def relay(self, resp, data):
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = forward


def serve(port):
    threading.Thread(target=pool.health_loop, daemon=True).start()
    lb = ThreadingHTTPServer(("127.0.0.1", port), LoadBalancerHandler)
    lb.daemon_threads = True
    lb.serve_forever()
