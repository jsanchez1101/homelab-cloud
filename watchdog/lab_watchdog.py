#!/usr/bin/env python3
"""Live-room watchdog for cloud-node1.

Receives OSPF reports from node1 over Tailscale, checks the home machines from
outside the house, and publishes a small public status file to Cloud Storage.

Writes only when the status changes, plus a heartbeat every 5 minutes, so the
bucket sees roughly 9k writes a month instead of 43k.

Standard library only. Runs as an unprivileged systemd DynamicUser.

Configuration comes from the environment (see lab-watchdog.env.example):
    LAB_BUCKET     Cloud Storage bucket that holds the public status file
    LAB_HOSTS      home machines to ping, as name=tailscale-ip pairs
    LAB_REPORTER   which of LAB_HOSTS may send OSPF reports
"""
import json
import os
import sys
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OBJECT = "status.json"
LISTEN_PORT = 8787
CHECK_EVERY = 60          # seconds between checks
HEARTBEAT = 300           # publish at least this often, even with no change
REPORT_MAX_AGE = 180      # node1 reports older than this count as missing
SINCE_TOLERANCE = 120     # "up since" times that move less than this are treated as unchanged

LINKS = ("r1r2", "r2r3", "r1r3")
ROUTERS = ("r1", "r2", "r3")
LINK_STATES = {"full", "down"}
ROUTER_STATES = {"up", "down"}



def parse_hosts(value):
    """"node1=100.64.0.1,node2=100.64.0.2" -> {"node1": "100.64.0.1", "node2": "100.64.0.2"}"""
    hosts = {}
    for pair in filter(None, (part.strip() for part in value.split(","))):
        name, _, ip = pair.partition("=")
        if not name or not ip:
            raise ValueError(f"bad LAB_HOSTS entry: {pair!r}")
        hosts[name.strip()] = ip.strip()
    return hosts


BUCKET = os.environ.get("LAB_BUCKET", "")
HOSTS = parse_hosts(os.environ.get("LAB_HOSTS", ""))
REPORTER_IP = HOSTS.get(os.environ.get("LAB_REPORTER", ""), "")

lock = threading.Lock()
last_report = {"at": 0.0, "links": None, "routers": None, "since": None}
NO_SINCE = {k: None for k in LINKS}

# A short history of real changes, newest first, published with the status so the
# site can show a lab log. It only changes when the lab does, so it adds no writes.
EVENTS_MAX = 8
EVENTS_FILE = os.path.join(os.environ.get("STATE_DIRECTORY", ""), "events.json") if os.environ.get("STATE_DIRECTORY") else ""


def log(msg):
    print(msg, flush=True)


def parse_time(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def validate_since(since):
    """Optional per-link "up since" times: an ISO UTC timestamp or null, never in the future."""
    if since is None:
        return dict(NO_SINCE)
    if not isinstance(since, dict) or set(since) != set(LINKS):
        return None
    now = datetime.now(timezone.utc)
    for value in since.values():
        if value is None:
            continue
        stamp = parse_time(value)
        if stamp is None or (stamp - now).total_seconds() > 300:
            return None
    return dict(since)


def validate(report):
    """Accept only the exact shape we publish. Anything else is rejected."""
    if not isinstance(report, dict) or not {"links", "routers"} <= set(report) <= {"links", "routers", "since"}:
        return None
    links, routers = report["links"], report["routers"]
    if not isinstance(links, dict) or set(links) != set(LINKS):
        return None
    if not isinstance(routers, dict) or set(routers) != set(ROUTERS):
        return None
    if not all(v in LINK_STATES for v in links.values()):
        return None
    if not all(v in ROUTER_STATES for v in routers.values()):
        return None
    since = validate_since(report.get("since"))
    if since is None:
        return None
    return {"links": dict(links), "routers": dict(routers), "since": since}


class ReportHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/report" or self.client_address[0] != REPORTER_IP:
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2048:
            self.send_error(400)
            return
        try:
            report = validate(json.loads(self.rfile.read(length)))
        except ValueError:
            report = None
        if report is None:
            self.send_error(422)
            return
        with lock:
            last_report.update(at=time.time(), **report)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass  # keep the journal quiet; publishes are logged instead


def host_up(ip):
    result = subprocess.run(["ping", "-c", "1", "-W", "3", ip],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def tailscale_ip():
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", "tailscale0"],
                         capture_output=True, text=True)
    fields = out.stdout.split()
    return fields[3].split("/")[0] if len(fields) > 3 else ""


def current_status():
    hosts = {name: ("up" if host_up(ip) else "down") for name, ip in HOSTS.items()}
    with lock:
        fresh = time.time() - last_report["at"] < REPORT_MAX_AGE
        links = last_report["links"] if fresh else None
        routers = last_report["routers"] if fresh else None
        since = last_report["since"] if fresh else None
    if links is None:
        # No recent report from node1, so the router state is unknown.
        links = {k: "unknown" for k in LINKS}
        routers = {k: "unknown" for k in ROUTERS}
        since = dict(NO_SINCE)
    return {"links": links, "routers": routers, "since": since, "hosts": hosts}


def steady_since(status, published):
    """Keep the published "up since" time when a new reading only differs by collection jitter.

    node1 derives each time from "now minus uptime", so it wobbles by a second or two
    between runs. Without this, every run would look like a change and force a write.
    """
    if not published:
        return status
    since = dict(status["since"])
    for link, value in since.items():
        old = published["since"].get(link)
        new_t, old_t = parse_time(value), parse_time(old)
        if new_t and old_t and abs((new_t - old_t).total_seconds()) <= SINCE_TOLERANCE:
            since[link] = old
    return {**status, "since": since}


def access_token():
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)["access_token"]


def publish(status):
    # Multipart upload so the object carries its own short cache lifetime.
    # The bucket default is max-age=3600, which would show stale status for an hour.
    meta = {"name": OBJECT, "contentType": "application/json",
            "cacheControl": "public, max-age=30"}
    boundary = "labstatus"
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n"
            f"{json.dumps(status, separators=(',', ':'))}\r\n--{boundary}--\r\n").encode()
    req = urllib.request.Request(
        f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o?uploadType=multipart",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {access_token()}",
                 "Content-Type": f"multipart/related; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def serve():
    # tailscale0 may not have its address yet right after boot; keep trying.
    while True:
        ip = tailscale_ip()
        if ip:
            try:
                server = ThreadingHTTPServer((ip, LISTEN_PORT), ReportHandler)
                log(f"listening on {ip}:{LISTEN_PORT}")
                server.serve_forever()
            except OSError as err:
                log(f"bind failed: {err}")
        time.sleep(10)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def diff_events(old, new, at):
    """Events for what changed between two published statuses (oldest first)."""
    if old is None:
        # First publish with no saved history: the "since" times say when each link came up.
        seeded = [{"at": t, "type": "link_full", "subject": link}
                  for link, t in new["since"].items() if t and new["links"][link] == "full"]
        return sorted(seeded, key=lambda e: e["at"])
    events = []
    old_unknown = all(v == "unknown" for v in old["routers"].values())
    new_unknown = all(v == "unknown" for v in new["routers"].values())
    if new_unknown and not old_unknown:
        events.append({"at": at, "type": "reports_lost", "subject": None})
    elif old_unknown and not new_unknown:
        events.append({"at": at, "type": "reports_back", "subject": None})
    for name, state in new["hosts"].items():
        if old["hosts"].get(name) != state:
            events.append({"at": at, "type": f"host_{state}", "subject": name})
    if not new_unknown:
        for router, state in new["routers"].items():
            if old["routers"].get(router) != state and state in ("up", "down"):
                events.append({"at": at, "type": f"router_{state}", "subject": router})
        for link, state in new["links"].items():
            if old["links"].get(link) != state and state in ("full", "down"):
                event_type = "link_full" if state == "full" else "link_down"
                events.append({"at": new["since"].get(link) or at if state == "full" else at,
                               "type": event_type, "subject": link})
    return events


def add_events(history, events):
    """Newest first, capped."""
    return sorted(history + events, key=lambda e: e["at"], reverse=True)[:EVENTS_MAX]


def load_events():
    if not EVENTS_FILE:
        return None
    try:
        with open(EVENTS_FILE) as f:
            events = json.load(f)
        return events if isinstance(events, list) else None
    except (OSError, ValueError):
        return None


def save_events(events):
    if not EVENTS_FILE:
        return
    tmp = EVENTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(events, f)
    os.replace(tmp, EVENTS_FILE)


def main():
    if not BUCKET or not HOSTS or not REPORTER_IP:
        sys.exit("Set LAB_BUCKET, LAB_HOSTS and LAB_REPORTER (see lab-watchdog.env.example).")
    threading.Thread(target=serve, daemon=True).start()
    published, published_at = None, 0.0
    started = time.time()
    saved = load_events()
    history = saved or []
    seed_needed = saved is None
    while True:
        # After a restart, give node1 time to report before publishing, so a
        # restart doesn't briefly tell the site the routers are unknown.
        with lock:
            have_report = last_report["at"] > started
        if not have_report and time.time() - started < REPORT_MAX_AGE:
            time.sleep(5)
            continue
        try:
            status = steady_since(current_status(), published)
            due = time.time() - published_at >= HEARTBEAT
            if status != published or due:
                at = now_iso()
                if published is not None or seed_needed:
                    new_events = diff_events(published if published is not None else None, status, at)
                    if new_events:
                        history = add_events(history, new_events)
                        save_events(history)
                    seed_needed = False
                stamped = {"checkedAt": at, **status, "events": history}
                publish(stamped)
                log(("change " if status != published else "heartbeat ") + json.dumps(status))
                published, published_at = status, time.time()
        except Exception as err:  # keep running; the next loop retries
            log(f"check failed: {err}")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
