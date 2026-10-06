# homelab-cloud

The pipeline behind the live lab display on [jason-ss.dev](https://jason-ss.dev). Three VyOS routers in my home EVE-NG lab report their OSPF state through a Google Cloud VM, and the homepage shows it: `3/3 ADJACENCIES · UP 2H 14M`, or `2/3` with the broken link in red when something goes down.

```
R1–R3 (VyOS, EVE-NG)
   │  show ip ospf neighbor (read-only SSH)
   ▼
node1 collector ─────── every 60 s
   │  Tailscale only
   ▼
cloud-node1 watchdog ── GCP e2-micro; also pings the home machines itself
   │  writes on change, plus a heartbeat every 5 min
   ▼
gs://<bucket>/status.json  (public, max-age=30)
   │
   ▼
jason-ss.dev /api/lab-status → the laptop on the homepage
```

| Path | Runs on | What it does |
|---|---|---|
| `gcp-hybrid-setup.sh` | laptop | Builds the GCP project, budget alert, VPC, firewall and VM |
| `watchdog/` | cloud VM | Receives OSPF reports, pings the home machines, publishes `status.json` |
| `collector/` | node1 (home) | Reads OSPF neighbors from the routers and reports to the watchdog |
| `tests/` | anywhere | Unit tests for validation, uptime parsing and change detection |

## Design decisions

**Router credentials never leave the house.** The collector runs at home, next to the existing NetOps agent, and reuses its device file. The cloud only ever receives link and router up/down states plus when each adjacency came up.

**The cloud is an outside observer.** If node1 or the home internet goes down, reports stop arriving and the watchdog notices on its own. A script at home pushing straight to the site could never report that the house had gone quiet.

**Publish what the page needs, nothing else.** The public file holds link states, router states, host up/down and timestamps. No IP addresses, no hostnames beyond `node1`/`node2`, no command output. Both the watchdog and the website validate the exact shape and drop anything else.

**About 2 cents a month.** Writing the file every minute would mean ~43k Cloud Storage writes a month. The watchdog writes only when the status changes, plus a heartbeat every 5 minutes (~9k writes). Uptime is published as "up since" timestamps rather than a running counter, so the file doesn't change every minute; the browser counts up. Collection jitter of a second or two is smoothed out so it doesn't count as a change.

## Security

- **Least privilege, verified.** The VM runs as a dedicated service account that can write one object, `status.json`, in one bucket, enforced with an IAM condition on the object name. Writing any other object returns 403.
- **No public SSH.** The VPC only allows SSH from Google's IAP range. Day-to-day access is over Tailscale. The default network and its open rules were deleted.
- **Reports only from node1, only over Tailscale.** The watchdog listens on its Tailscale address only, accepts reports only from the configured reporter, and GCP's firewall also blocks the port from the internet.
- **Sandboxed service.** The watchdog runs as a systemd `DynamicUser` with a read-only filesystem and a 64 MB memory cap. Its only privilege is `CAP_NET_RAW`, because Ubuntu 24.04 disables unprivileged ping.
- **Untrusted input is validated twice.** The watchdog rejects reports with unknown keys, states or future timestamps; the website's API route re-validates the public file before the page sees it.

## How it runs

**Cloud.** `gcp-hybrid-setup.sh` built the project: a $5 budget alert, a custom VPC with IAP-only SSH, and an e2-micro running Ubuntu 24.04 that joined my tailnet. I then added the status bucket and the restricted service account by hand.

**Watchdog.** It runs on the VM as a systemd service (`watchdog/lab-watchdog.service`). Its settings live in `/etc/lab-watchdog.env` (example in `watchdog/lab-watchdog.env.example`):

| Variable | Meaning |
|---|---|
| `LAB_BUCKET` | Bucket that holds `status.json` |
| `LAB_HOSTS` | Home machines to ping, e.g. `node1=100.64.0.1,node2=100.64.0.2` |
| `LAB_REPORTER` | Which of `LAB_HOSTS` may send OSPF reports |

Each publish is logged, so `journalctl -u lab-watchdog -f` shows every change and heartbeat as it happens.

**Collector.** It lives on node1 next to my NetOps agent, reusing that agent's Python environment and device file, and runs every minute from cron with `LAB_WATCHDOG_URL` pointing at the VM's Tailscale address. I used cron instead of the systemd timer in `collector/` because node1's sudo needs a password.

**Updating.** I edit here, run the tests, then copy the changed file to the VM or node1 and restart the service.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Standard library only; netmiko and PyYAML are not needed to run them.

## Lessons

- **Plan the address space before connecting networks.** The first cloud subnet was `10.50.0.0/24`, and the VM got `10.50.0.2`, which is R2's router ID in the lab. It didn't break anything yet only because the cloud never routes into the lab. The setup script now defaults to `10.60.0.0/24`.
- **Check the default caching.** Cloud Storage serves public objects with `max-age=3600`, which would have shown an hour-old status. The watchdog sets `max-age=30` on every upload.
- **A restart shouldn't publish a lie.** Restarting the watchdog briefly published "unknown" for every router because it had forgotten the last report. It now waits for node1's first report before publishing.
- **Real output beats memory.** The uptime parser was written against actual VyOS output (`27m32s`, `1h02m03s`, `3d04h05m`), not assumptions about the format.

## Limitations

- One collector and one watchdog: no redundancy. If the VM dies, the site shows "offline" after 12 minutes rather than failing over.
- Router state comes from SSH reachability plus OSPF's own view. A router that is up but unreachable over management shows as down.
- The topology (R1–R3 router IDs and links) is hard-coded in the collector.
