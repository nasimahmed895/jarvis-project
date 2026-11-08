"""P4.10 — internet connectivity monitor."""

from __future__ import annotations

import socket
import threading
import time
import unittest

from actions import net_monitor


def _start_tcp_server() -> tuple[str, int, socket.socket]:
    """Spin up a loopback TCP listener on an ephemeral port; return
    (host, port, socket) — caller closes."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    host, port = s.getsockname()

    def accept_loop():
        try:
            while True:
                conn, _ = s.accept()
                conn.close()
        except OSError:
            pass

    threading.Thread(target=accept_loop, daemon=True).start()
    return host, port, s


class NetMonitorTests(unittest.TestCase):
    def test_reachable_host_reports_online(self):
        host, port, srv = _start_tcp_server()
        try:
            report = net_monitor.check(((host, port),), timeout_s=1.0)
            self.assertTrue(report["online"])
            self.assertEqual(len(report["probes"]), 1)
            self.assertTrue(report["probes"][0]["ok"])
            self.assertIsNotNone(report["probes"][0]["latency_ms"])
            self.assertIsNotNone(report["avg_ms"])
        finally:
            srv.close()

    def test_unreachable_host_reports_offline(self):
        # Reserved documentation range (RFC 5737) — no real listener.
        report = net_monitor.check(
            (("192.0.2.1", 1), ),
            timeout_s=0.3,
        )
        self.assertFalse(report["online"])
        self.assertFalse(report["probes"][0]["ok"])
        self.assertIsNotNone(report["probes"][0]["error"])
        self.assertIsNone(report["avg_ms"])

    def test_mixed_probes_still_online(self):
        host, port, srv = _start_tcp_server()
        try:
            report = net_monitor.check((
                (host, port),
                ("192.0.2.1", 1),
            ), timeout_s=0.3)
            self.assertTrue(report["online"])
            oks = [r["ok"] for r in report["probes"]]
            self.assertEqual(sorted(oks), [False, True])
        finally:
            srv.close()

    def test_render_includes_status_and_rows(self):
        host, port, srv = _start_tcp_server()
        try:
            text = net_monitor.render(net_monitor.check(((host, port),), 1.0))
            self.assertIn("ONLINE", text)
            self.assertIn(host, text)
        finally:
            srv.close()

    def test_tool_entry_with_custom_hosts(self):
        host, port, srv = _start_tcp_server()
        try:
            out = net_monitor._tool_entry({
                "timeout_s": 1.0,
                "hosts": [{"host": host, "port": port}],
            })
            self.assertIn("ONLINE", out)
        finally:
            srv.close()

    def test_tool_entry_malformed_hosts(self):
        r = net_monitor._tool_entry({"hosts": [{"bad": "value"}]})
        self.assertIn("hosts", r)


if __name__ == "__main__":
    unittest.main()
