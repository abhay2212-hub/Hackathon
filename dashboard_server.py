"""Professional dashboards (no Streamlit needed, works offline).

    python dashboard_server.py

Then open in a browser:
    Control room (operator):  http://localhost:8600
    Public / commuters:       http://localhost:8600/public
    On a phone (same Wi-Fi):  http://<laptop-IP>:8600/public   (the IP is printed below)

Start the simulation first (python both.py ...). The pages update every 2 seconds.
"""
import csv
import json
import os
import socket
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import settings as S

PORT = 8600
os.chdir(S.HERE)
WEB = os.path.join(S.HERE, "web")
net = S.Net(S.NET)
ADVISORIES = "advisories.json"
ADVISORY_TYPES = {"official", "construction", "damage", "other"}
_COMPARISON_SIGNATURE = None
_COMPARISON_DATA = None


# ---------------------------------------------------------------- data for the pages
def net_json():
    return {
        "bounds": net.bounds,
        "edges": {e: {"shape": d["shape"], "lanes": d["lanes"], "from": d["from"], "to": d["to"]}
                  for e, d in net.edges.items()},
        "tls": [{"id": j, "x": net.junctions[j]["x"], "y": net.junctions[j]["y"]} for j in net.tls
                if j in net.junctions],
        "entries": [{"id": e, "name": net.name(e)} for e in net.entries],
        "exits": [{"id": e, "name": net.name(e)} for e in net.exits],
        "reverse": {e: net.reverse_of(e) for e in net.edges},
    }


def timeline(mode, max_points=360):
    path = f"timeline_{mode}.csv"
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    step = max(1, len(rows) // max_points)
    out = []
    for r in rows[::step]:
        try:
            out.append([int(r["time_s"]), r["clock"], int(r["waiting"]), float(r["avg_speed_kmh"]),
                        int(r["vehicles"])])
        except (KeyError, ValueError):
            pass
    return out


def tripinfo(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            return None
        end = text.rfind("</tripinfo>")
        text = text[:end + 11] if end >= 0 else text[:text.rfind("/>") + 2]
        start = text.find("<tripinfo ")
        if start < 0:
            return None
        try:
            root = ET.fromstring("<tripinfos>" + text[start:] + "</tripinfos>")
        except ET.ParseError:
            return None
    except OSError:
        return None

    out = []
    try:
        for trip in root.iter("tripinfo"):
            if trip.get("vaporized"):
                continue
            emissions = trip.find("emissions")
            out.append({
                "delay": float(trip.get("timeLoss", 0)),
                "wait": float(trip.get("waitingTime", 0)),
                "travel": float(trip.get("duration", 0)),
                "stops": float(trip.get("waitingCount", 0)),
                "co2": float(emissions.get("CO2_abs", 0)) / 1_000_000 if emissions is not None else 0,
            })
    except (TypeError, ValueError):
        return None
    return out


def comparison_summary():
    global _COMPARISON_SIGNATURE, _COMPARISON_DATA
    paths = ("tripinfo_fixed.xml", "tripinfo_smart.xml")
    try:
        signature = tuple((os.stat(path).st_mtime_ns, os.stat(path).st_size) for path in paths)
    except OSError:
        return {"available": False, "rows": []}
    if signature == _COMPARISON_SIGNATURE:
        return _COMPARISON_DATA

    baseline, smart = (tripinfo(path) for path in paths)
    if baseline is None or smart is None:
        data = {"available": False, "rows": []}
    else:
        metrics = (
            ("Vehicles finished", "count", True),
            ("Avg delay (s)", "delay", False),
            ("Avg waiting at red (s)", "wait", False),
            ("Avg travel time (s)", "travel", False),
            ("Avg stops per vehicle", "stops", False),
            ("Total CO2 (kg)", "co2", False),
        )
        rows = []
        for label, key, higher_better in metrics:
            fixed_value = len(baseline) if key == "count" else (
                sum(trip[key] for trip in baseline) if key == "co2" else
                sum(trip[key] for trip in baseline) / len(baseline) if baseline else 0)
            smart_value = len(smart) if key == "count" else (
                sum(trip[key] for trip in smart) if key == "co2" else
                sum(trip[key] for trip in smart) / len(smart) if smart else 0)
            change = (smart_value - fixed_value) / fixed_value * 100 if fixed_value else None
            rows.append({"label": label, "baseline": fixed_value, "smart": smart_value,
                         "change_pct": change, "higher_better": higher_better})
        data = {"available": True, "rows": rows, "updated": max(pair[0] for pair in signature) / 1e9}

    _COMPARISON_SIGNATURE = signature
    _COMPARISON_DATA = data
    return data


def state():
    smart = S.read_json("live_state_smart.json")
    fixed = S.read_json("live_state_fixed.json")
    now = time.time()
    frame_path = os.path.join(S.HERE, "camera_frame.jpg")
    try:
        camera_frame_updated = os.path.getmtime(frame_path)
    except OSError:
        camera_frame_updated = 0
    return {
        "smart": smart, "fixed": fixed,
        "advisories": S.read_json(ADVISORIES, []) or [],
        "camera_frame_updated": camera_frame_updated,
        "comparison": comparison_summary(),
        "smart_live": bool(smart) and now - smart.get("updated", 0) < 10,
        "fixed_live": bool(fixed) and now - fixed.get("updated", 0) < 10,
        "demo": S.read_json(S.DEMO_FILE, {}) or {},
        "settings": {"start_clock": S.START_CLOCK, "sim_time": S.SIM_TIME, "delay": S.DELAY,
                     "threshold": S.THRESHOLD, "low_traffic": S.LOW_TRAFFIC, "congested": S.CONGESTED},
        "has_comparison": os.path.exists("comparison.png"),
    }


def route(a, b):
    smart = S.read_json("live_state_smart.json") or {}
    advisories = S.read_json(ADVISORIES, []) or []
    blocked = {a["road"] for a in advisories if a.get("closed") and a.get("road")}
    blocked.update(reverse for road in tuple(blocked) if (reverse := net.reverse_of(road)))
    live = {e: r["tt"] for e, r in smart.get("roads", {}).items()}
    free = {e: d["length"] / max(d["speed"], 0.1) for e, d in net.edges.items()}
    best, t_best = net.best_route(a, b, live, blocked)
    if not best:
        return {"route": None, "closed_roads": sorted(blocked)}
    usual, _ = net.best_route(a, b, free, blocked)
    t_usual = sum(live.get(e, free[e]) for e in usual) if usual else None
    return {"route": best, "t_best": round(t_best, 1), "usual": usual,
            "t_usual": round(t_usual, 1) if t_usual else None}


def send_command(cmd):
    cmds = S.read_json(S.COMMANDS, []) or []
    cmd["id"] = max([c.get("id", 0) for c in cmds], default=0) + 1
    cmd["sent"] = time.time()
    S.write_json(S.COMMANDS, (cmds + [cmd])[-200:])


def update_demo(changes):
    c = dict(S.read_json(S.DEMO_FILE, {}) or {})
    c.update(changes)
    S.write_json(S.DEMO_FILE, c)
    return c


def update_advisories(change):
    if not isinstance(change, dict):
        raise ValueError("Expected an advisory object")
    advisories = S.read_json(ADVISORIES, []) or []
    action = change.get("action", "save")
    if action == "delete":
        try:
            advisory_id = int(change.get("id"))
        except (TypeError, ValueError):
            raise ValueError("Invalid advisory id")
        updated = [a for a in advisories if a.get("id") != advisory_id]
        if len(updated) == len(advisories):
            raise ValueError("Advisory not found")
        S.write_json(ADVISORIES, updated)
        return updated

    title = str(change.get("title", "")).strip()
    details = str(change.get("details", "")).strip()
    kind = change.get("type")
    road = str(change.get("road", "")).strip() or None
    closed = bool(change.get("closed"))
    if kind not in ADVISORY_TYPES:
        raise ValueError("Choose a valid advisory type")
    if not title or len(title) > 100:
        raise ValueError("Title must be 1 to 100 characters")
    if not details or len(details) > 500:
        raise ValueError("Details must be 1 to 500 characters")
    if road is not None and road not in net.edges:
        raise ValueError("Choose a road in the network")
    if closed and road is None:
        raise ValueError("Select a road to mark it closed")

    try:
        advisory_id = int(change["id"]) if change.get("id") is not None else None
    except (TypeError, ValueError):
        raise ValueError("Invalid advisory id")
    advisory = {"id": advisory_id, "type": kind, "title": title, "details": details,
                "road": road, "closed": closed, "updated": time.time()}
    if advisory_id is None:
        advisory["id"] = max((a.get("id", 0) for a in advisories), default=0) + 1
        advisories.insert(0, advisory)
    else:
        for index, existing in enumerate(advisories):
            if existing.get("id") == advisory_id:
                advisories[index] = advisory
                break
        else:
            raise ValueError("Advisory not found")
    S.write_json(ADVISORIES, advisories[:100])
    return advisories[:100]


# ---------------------------------------------------------------- web server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):          # keep the terminal quiet
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Control-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):                      # browser check before a POST from Netlify
        self.send_response(204)
        self.cors()
        self.end_headers()

    def send(self, body, ctype="application/json", code=200):
        if isinstance(body, (dict, list)) or body is None:
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self.send(f.read(), ctype)
        except OSError:
            if ctype.startswith("text/html"):
                self.send(f"<h2>Page file missing</h2><p>Could not find <b>{path}</b>.</p>"
                          "<p>Copy the <b>web</b> folder from the zip into the same folder as "
                          "dashboard_server.py, then restart it.</p>", "text/html; charset=utf-8", 404)
            else:
                self.send({"error": f"missing file {os.path.basename(path)}"}, code=404)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path.rstrip("/") or "/"
        if p == "/":
            self.file(os.path.join(WEB, "operator.html"), "text/html; charset=utf-8")
        elif p == "/public":
            self.file(os.path.join(WEB, "public.html"), "text/html; charset=utf-8")
        elif p in ("/common.css", "/common.js", "/config.js"):
            self.file(os.path.join(WEB, p[1:]), "text/css" if p.endswith(".css") else "text/javascript")
        elif p == "/api/net":
            self.send(net_json())
        elif p == "/api/state":
            self.send(state())
        elif p == "/api/timeline":
            self.send({"fixed": timeline("fixed"), "smart": timeline("smart")})
        elif p == "/api/forecast":
            self.send(S.read_json("forecast.json"))
        elif p == "/api/route":
            self.send(route(q.get("from"), q.get("to")))
        elif p == "/comparison.png":
            self.file("comparison.png", "image/png")
        elif p == "/camera-frame.jpg":
            self.file("camera_frame.jpg", "image/jpeg")
        elif p.startswith("/img/") and p[5:] in ("school.png", "office.png", "hospital.png"):
            self.file(p[5:], "image/png")
        else:
            self.send("<h2>Unknown address</h2><p>Open <a href='/'>the control room</a> "
                      "or <a href='/public'>the public page</a>.</p>", "text/html; charset=utf-8", 404)

    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0) or 0)
        if p == "/api/camera-frame":
            if n <= 0 or n > 5_000_000:
                return self.send({"error": "invalid frame size"}, code=400)
            jpeg = self.rfile.read(n)
            if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                return self.send({"error": "expected a JPEG frame"}, code=400)
            path = os.path.join(S.HERE, "camera_frame.jpg")
            tmp = f"{path}.{os.getpid()}.tmp"
            try:
                with open(tmp, "wb") as f:
                    f.write(jpeg)
                os.replace(tmp, path)
            except OSError:
                return self.send({"error": "could not store camera frame"}, code=500)
            return self.send({"ok": True})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self.send({"error": "bad json"}, code=400)
        if p in ("/api/command", "/api/demo", "/api/advisories") and S.CONTROL_KEY and self.headers.get("X-Control-Key") != S.CONTROL_KEY:
            return self.send({"error": "control locked"}, code=403)
        if p == "/api/command":
            send_command(body)
            self.send({"ok": True})
        elif p == "/api/demo":
            self.send(update_demo(body))
        elif p == "/api/advisories":
            try:
                self.send({"advisories": update_advisories(body)})
            except ValueError as e:
                self.send({"error": str(e)}, code=400)
        elif p == "/api/yolo":                 # camera counts from YOLO on another laptop
            body.setdefault("time", time.time())
            S.write_json(S.YOLO_FILE, body)
            self.send({"ok": True})
        else:
            self.send({"error": "not found"}, code=404)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "<laptop-IP>"


if __name__ == "__main__":
    missing = [f for f in ("operator.html", "public.html", "common.js", "common.css")
               if not os.path.exists(os.path.join(WEB, f))]
    if missing:
        print(f"WARNING: missing in {WEB}: {', '.join(missing)}")
        print("Copy the 'web' folder from the zip next to dashboard_server.py.\n")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ip = lan_ip()
    print("Dashboards running (press Ctrl+C to stop):")
    print(f"  Control room : http://localhost:{PORT}")
    print(f"  Public       : http://localhost:{PORT}/public")
    print(f"  Phone        : http://{ip}:{PORT}/public   (same Wi-Fi)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
