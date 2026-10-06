import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))
import ospf_reporter as r  # noqa: E402

R1_OUTPUT = """Neighbor ID     Pri State           Up Time         Dead Time Address         Interface                        RXmtL RqstL DBsmL
10.50.0.2         1 Full/DR         27m32s            37.325s 10.0.12.2       eth1:10.0.12.1                       0     0     0
3.3.3.3           1 Full/DR         27m28s            32.049s 10.0.13.2       eth2:10.0.13.1                       0     0     0"""


class UptimeTest(unittest.TestCase):
    def test_frr_formats(self):
        cases = {"27m32s": 1652, "1h02m03s": 3723, "3d04h05m": 273900, "2w1d03h": 1306800, "32.049s": 32}
        for text, seconds in cases.items():
            with self.subTest(text=text):
                self.assertEqual(r.uptime_seconds(text), seconds)

    def test_rejects_junk(self):
        for text in ("", "bogus", "1h2x", "12"):
            with self.subTest(text=text):
                self.assertIsNone(r.uptime_seconds(text))


class BuildReportTest(unittest.TestCase):
    def test_healthy_triangle(self):
        views = {"R1": {"10.50.0.2": 1652, "3.3.3.3": 1648},
                 "R2": {"1.1.1.1": 1650, "3.3.3.3": 1640},
                 "R3": {"1.1.1.1": 1648, "10.50.0.2": 1641}}
        report = r.build_report(views)
        self.assertEqual(set(report["links"].values()), {"full"})
        self.assertEqual(set(report["routers"].values()), {"up"})
        self.assertTrue(all(report["since"].values()))

    def test_uses_the_more_recent_end_for_since(self):
        views = {"R1": {"10.50.0.2": 3600, "3.3.3.3": 3600},
                 "R2": {"1.1.1.1": 600, "3.3.3.3": 3600},
                 "R3": {"1.1.1.1": 3600, "10.50.0.2": 3600}}
        before = datetime.now(timezone.utc)
        since = datetime.strptime(r.build_report(views)["since"]["r1r2"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertAlmostEqual((before - since).total_seconds(), 600, delta=5)

    def test_unreachable_router_and_disagreement(self):
        views = {"R1": {"10.50.0.2": 100, "3.3.3.3": 100}, "R2": {"1.1.1.1": 100}, "R3": None}
        report = r.build_report(views)
        self.assertEqual(report["routers"]["r3"], "down")
        self.assertEqual(report["links"]["r1r2"], "full")
        # R2 can't see R3, so the link is down even though R3 itself can't be asked.
        self.assertEqual(report["links"]["r2r3"], "down")
        self.assertIsNone(report["since"]["r2r3"])


class ParseOutputTest(unittest.TestCase):
    def test_reads_full_neighbors_and_uptimes(self):
        class FakeConn:
            def __init__(self, **kwargs): pass
            def send_command(self, command, read_timeout): return R1_OUTPUT
            def disconnect(self): pass

        import types
        sys.modules["netmiko"] = types.SimpleNamespace(ConnectHandler=FakeConn)
        try:
            seen = r.full_neighbors({"host": "x", "username": "u", "password": "p"})
        finally:
            del sys.modules["netmiko"]
        self.assertEqual(seen, {"10.50.0.2": 1652, "3.3.3.3": 1648})


if __name__ == "__main__":
    unittest.main()
