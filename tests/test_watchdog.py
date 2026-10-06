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
