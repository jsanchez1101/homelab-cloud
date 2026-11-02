import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "watchdog"))
import lab_watchdog as w  # noqa: E402

LINKS = {"r1r2": "full", "r2r3": "full", "r1r3": "full"}
ROUTERS = {"r1": "up", "r2": "up", "r3": "up"}
SINCE = {"r1r2": "2026-10-06T00:56:10Z", "r2r3": "2026-10-06T00:56:40Z", "r1r3": None}


class ValidateTest(unittest.TestCase):
    def test_accepts_reports_with_and_without_since(self):
        self.assertIsNotNone(w.validate({"links": LINKS, "routers": ROUTERS}))
        report = w.validate({"links": LINKS, "routers": ROUTERS, "since": SINCE})
        self.assertEqual(report["since"], SINCE)

    def test_rejects_unexpected_shapes(self):
        bad = [
            {"links": LINKS},                                            # missing routers
            {"links": LINKS, "routers": ROUTERS, "extra": 1},            # unknown key
            {"links": {**LINKS, "r1r2": "maybe"}, "routers": ROUTERS},   # unknown state
            {"links": {"r1r2": "full"}, "routers": ROUTERS},             # missing links
            {"links": LINKS, "routers": ROUTERS, "since": {**SINCE, "r1r3": "tomorrow"}},
            {"links": LINKS, "routers": ROUTERS, "since": {**SINCE, "r1r3": "2999-01-01T00:00:00Z"}},
            "not a dict",
        ]
        for report in bad:
            with self.subTest(report=report):
                self.assertIsNone(w.validate(report))


class SteadySinceTest(unittest.TestCase):
    def published(self):
        return {"links": LINKS, "routers": ROUTERS, "since": SINCE, "hosts": {}}

    def test_ignores_collection_jitter(self):
        status = {**self.published(), "since": {**SINCE, "r1r2": "2026-10-06T00:56:12Z"}}
        self.assertEqual(w.steady_since(status, self.published())["since"]["r1r2"], SINCE["r1r2"])

    def test_keeps_real_resets(self):
        status = {**self.published(), "since": {**SINCE, "r2r3": "2026-10-06T01:20:00Z"}}
        self.assertEqual(w.steady_since(status, self.published())["since"]["r2r3"], "2026-10-06T01:20:00Z")

    def test_first_publish_passes_through(self):
        status = self.published()
        self.assertEqual(w.steady_since(status, None), status)


class ParseHostsTest(unittest.TestCase):
    def test_parses_pairs(self):
        self.assertEqual(w.parse_hosts("node1=100.64.0.1, node2=100.64.0.2"),
                         {"node1": "100.64.0.1", "node2": "100.64.0.2"})
        self.assertEqual(w.parse_hosts(""), {})

    def test_rejects_malformed_entries(self):
        with self.assertRaises(ValueError):
            w.parse_hosts("node1")


if __name__ == "__main__":
    unittest.main()


class EventsTest(unittest.TestCase):
    def status(self, links=None, routers=None, hosts=None, since=None):
        return {"links": links or dict(LINKS), "routers": routers or dict(ROUTERS),
                "since": since or {"r1r2": "2026-10-06T00:56:00Z", "r2r3": "2026-10-06T00:57:00Z", "r1r3": "2026-10-06T00:58:00Z"},
                "hosts": hosts or {"node1": "up", "node2": "up"}}

    def test_seeds_link_full_events_from_since(self):
        events = w.diff_events(None, self.status(), "2026-10-06T02:00:00Z")
        self.assertEqual([e["subject"] for e in events], ["r1r2", "r2r3", "r1r3"])
        self.assertTrue(all(e["type"] == "link_full" for e in events))

    def test_link_down_and_back(self):
        old = self.status()
        down = self.status(links={**LINKS, "r2r3": "down"}, since={**old["since"], "r2r3": None})
        self.assertEqual(w.diff_events(old, down, "2026-10-06T03:00:00Z"),
                         [{"at": "2026-10-06T03:00:00Z", "type": "link_down", "subject": "r2r3"}])
        back = self.status(since={**old["since"], "r2r3": "2026-10-06T03:05:10Z"})
        self.assertEqual(w.diff_events(down, back, "2026-10-06T03:06:00Z"),
                         [{"at": "2026-10-06T03:05:10Z", "type": "link_full", "subject": "r2r3"}])

    def test_reports_lost_hides_router_noise(self):
        unknown = {"r1": "unknown", "r2": "unknown", "r3": "unknown"}
        lost = self.status(links={k: "unknown" for k in LINKS}, routers=unknown, hosts={"node1": "down", "node2": "up"})
        types = [e["type"] for e in w.diff_events(self.status(), lost, "2026-10-06T04:00:00Z")]
        self.assertEqual(types, ["reports_lost", "host_down"])

    def test_history_is_newest_first_and_capped(self):
        events = [{"at": f"2026-10-06T0{i}:00:00Z", "type": "link_full", "subject": "r1r2"} for i in range(10)]
        history = w.add_events([], events)
        self.assertEqual(len(history), w.EVENTS_MAX)
        self.assertEqual(history[0]["at"], "2026-10-06T09:00:00Z")


class PauseTest(unittest.TestCase):
    def pause_from(self, content, now):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
        old = w.PAUSE_FILE
        w.PAUSE_FILE = f.name
        try:
            return w.current_pause(now=now)
        finally:
            w.PAUSE_FILE = old
            os.unlink(f.name)

    def test_active_hold_reports_earliest_start_and_generic_reason(self):
        p = self.pause_from({"holders": {"chat35": 1_800_000_000, "bench": 1_800_000_600}}, now=1_800_003_600)
        self.assertEqual(p, {"since": "2027-01-15T08:00:00Z", "reason": w.PAUSE_REASON})

    def test_long_holds_remain_paused_and_invalid_holds_are_ignored(self):
        self.assertIsNotNone(self.pause_from({"holders": {"old": 1_800_000_000}}, now=1_800_000_000 + 7 * 86400))
        for holders in ([1], {"bad": True}, {"bad": float("inf")}, {"bad": -1}, {"future": 1_900_000_000}):
            self.assertIsNone(self.pause_from({"holders": holders}, now=1_800_000_000))
        self.assertIsNone(self.pause_from({"holders": {}}, now=1_800_000_000))
        self.assertIsNone(self.pause_from("not json", now=1_800_000_000))
        self.assertIsNone(self.pause_from(["a", "list"], now=1_800_000_000))

    def test_no_pause_file_configured(self):
        old = w.PAUSE_FILE
        w.PAUSE_FILE = ""
        try:
            self.assertIsNone(w.current_pause())
        finally:
            w.PAUSE_FILE = old

    def test_paused_and_resumed_events(self):
        base = EventsTest.status(EventsTest())
        paused = {**base, "paused": {"since": "2026-11-02T02:50:00Z", "reason": w.PAUSE_REASON}}
        self.assertIn({"at": "2026-11-02T02:50:00Z", "type": "lab_paused", "subject": None},
                      w.diff_events({**base, "paused": None}, paused, "2026-11-02T02:51:00Z"))
        self.assertIn({"at": "2026-11-02T09:00:00Z", "type": "lab_resumed", "subject": None},
                      w.diff_events(paused, {**base, "paused": None}, "2026-11-02T09:00:00Z"))

    def test_statuses_published_before_this_change_still_compare(self):
        base = EventsTest.status(EventsTest())          # no "paused" key at all
        self.assertEqual(w.diff_events(base, {**base, "paused": None}, "2026-11-02T00:00:00Z"), [])


class HomeModeTests(unittest.TestCase):
    def test_listen_setting_overrides_tailscale_address(self):
        old = w.LISTEN
        try:
            w.LISTEN = "127.0.0.1:8788"
            self.assertEqual(w.listen_address(), ("127.0.0.1", 8788))
        finally:
            w.LISTEN = old

    def test_key_file_token_signs_a_jwt_and_caches_it(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest("cryptography not installed")
        import json, tempfile, io, base64
        from unittest import mock
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"client_email": "sa@example.iam.gserviceaccount.com", "private_key": pem,
                       "token_uri": "https://oauth2.example/token"}, f)
        sent = []

        def fake_urlopen(req, timeout=0):
            sent.append(req)
            return io.BytesIO(json.dumps({"access_token": "tok", "expires_in": 3600}).encode())

        w._token.update(value=None, expires=0.0)
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(w.key_file_token(f.name, now=1000), "tok")
            self.assertEqual(w.key_file_token(f.name, now=1100), "tok")   # cached, no second request
        self.assertEqual(len(sent), 1)
        assertion = dict(x.split("=", 1) for x in sent[0].data.decode().split("&"))["assertion"]
        claims = json.loads(base64.urlsafe_b64decode(assertion.split(".")[1] + "=="))
        self.assertEqual(claims["iss"], "sa@example.iam.gserviceaccount.com")
        self.assertEqual(claims["scope"], "https://www.googleapis.com/auth/devstorage.read_write")
        os.unlink(f.name)
