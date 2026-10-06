#!/usr/bin/env python3
"""Live-room collector for node1.

Runs `show ip ospf neighbor` on R1-R3 (read-only, same command the NetOps agent
uses), turns the output into link and router states, and sends them to the
watchdog on cloud-node1 over Tailscale. Router credentials stay on node1:
only the summary below leaves the house.

    {"links": {"r1r2": "full", ...}, "routers": {"r1": "up", ...},
     "since": {"r1r2": "2026-10-06T00:56:10Z", ...}}

"since" is when each adjacency last came up (now minus OSPF's Up Time), so the
site can count uptime without the file changing every minute.

Usage: LAB_WATCHDOG_URL=http://<cloud-node tailscale ip>:8787/report \
           python ospf_reporter.py /path/to/devices.yaml
Needs netmiko and PyYAML, which the NetOps agent's environment already has.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

ROUTER_IDS = {"R1": "1.1.1.1", "R2": "10.50.0.2", "R3": "3.3.3.3"}
LINKS = {"r1r2": ("R1", "R2"), "r2r3": ("R2", "R3"), "r1r3": ("R1", "R3")}
UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def uptime_seconds(text):
    """FRR prints Up Time like 27m32s, 1h02m03s, 3d04h05m or 2w1d03h."""
    parts = re.findall(r"(\d+(?:\.\d+)?)([wdhms])", text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    return int(sum(float(n) * UNITS[u] for n, u in parts))


def full_neighbors(device):
    """Return {router ID: uptime seconds} for neighbors in Full state, or None if unreachable."""
    from netmiko import ConnectHandler  # imported here so the parsing logic can be tested without it

    try:
        conn = ConnectHandler(device_type=device.get("device_type", "vyos"),
                              host=device["host"], username=device["username"],
                              password=device["password"], conn_timeout=10)
        try:
            output = conn.send_command("show ip ospf neighbor", read_timeout=20)
        finally:
            conn.disconnect()
    except Exception:
        return None
    # FRR format: "Neighbor ID  Pri  State  Up Time  Dead Time  Address  Interface ..."
    seen = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] in ROUTER_IDS.values() and parts[2].startswith("Full"):
            seen[parts[0]] = uptime_seconds(parts[3])
    return seen


def build_report(views):
    routers = {name.lower(): ("up" if views[name] is not None else "down") for name in ROUTER_IDS}
    now = datetime.now(timezone.utc)
    links, since = {}, {}
    for link, (a, b) in LINKS.items():
        # Ask both ends. A link counts as full only if every end we could reach agrees.
        answers = []
        if views[a] is not None:
            answers.append(ROUTER_IDS[b] in views[a])
        if views[b] is not None:
            answers.append(ROUTER_IDS[a] in views[b])
        links[link] = "full" if answers and all(answers) else "down"
        # Up since the more recent of the two ends' view, so a reset on either side counts.
        uptimes = [views[x][ROUTER_IDS[y]] for x, y in ((a, b), (b, a))
                   if views[x] is not None and ROUTER_IDS[y] in views[x]]
        since[link] = None
        if links[link] == "full" and uptimes and None not in uptimes:
            since[link] = (now - timedelta(seconds=min(uptimes))).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"links": links, "routers": routers, "since": since}


def main():
    import yaml

    watchdog = os.environ.get("LAB_WATCHDOG_URL")
    if len(sys.argv) != 2 or not watchdog:
        sys.exit("Usage: LAB_WATCHDOG_URL=http://host:8787/report python ospf_reporter.py devices.yaml")
    with open(sys.argv[1]) as f:
        devices = yaml.safe_load(f)["devices"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = pool.map(lambda name: (name, full_neighbors(devices[name])), ROUTER_IDS)
        views = dict(results)
    report = build_report(views)
    req = urllib.request.Request(watchdog, data=json.dumps(report).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()
    print(json.dumps(report))


if __name__ == "__main__":
    main()
