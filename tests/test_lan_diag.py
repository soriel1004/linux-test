import contextlib
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import lan_diag as diag


class DiagnosticsTests(unittest.TestCase):
    def test_error_deltas_and_counter_reset(self):
        before = {"present": True, "ifindex": 2, "carrier": 1,
                  "carrier_changes": 2, "counters": {"rx_bytes": 1000, "rx_errors": 4, "tx_bytes": 999}}
        after = {"present": True, "ifindex": 2, "carrier": 0,
                 "carrier_changes": 4, "counters": {"rx_bytes": 2000, "rx_errors": 6, "tx_bytes": 10}}
        events = diag.nic_changes("eth0", after, before, 2)
        self.assertAlmostEqual(after["rx_mbps"], 0.004)
        self.assertNotIn("tx_bytes", after["delta"])
        self.assertIn({"event": "counter_reset", "interface": "eth0", "counters": ["tx_bytes"]}, events)
        self.assertTrue(any(e.get("field") == "carrier" and e["after"] == 0 for e in events))
        self.assertTrue(any(e.get("event") == "nic_counter_increase" and e["delta"]["rx_errors"] == 2 for e in events))
        self.assertTrue(any(e.get("event") == "link_counter_change" and e["delta"]["carrier_changes"] == 2 for e in events))

    def test_interface_replacement_does_not_mix_counters(self):
        before = {"present": True, "ifindex": 2, "counters": {"rx_errors": 1}}
        after = {"present": True, "ifindex": 3, "counters": {"rx_errors": 100}}
        events = diag.nic_changes("eth0", after, before, 1)
        self.assertNotIn("delta", after)
        self.assertEqual(events[0]["field"], "ifindex")

    def test_disappearing_and_returning_interface(self):
        previous = {"present": True, "carrier": 1, "counters": {}}
        events = diag.nic_changes("eth0", {"present": False}, previous, 1)
        self.assertTrue(any(e.get("field") == "present" and not e["after"] for e in events))
        current = {"present": True, "carrier": 1, "counters": {"rx_bytes": 99}}
        events = diag.nic_changes("eth0", current, {"present": False}, 1)
        self.assertTrue(any(e.get("field") == "present" and e["after"] for e in events))
        self.assertNotIn("delta", current)

    def test_missing_and_unreadable_sysfs_not_reported_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(diag.read_nic("eth0", root), {"present": False})
            (root / "eth0" / "statistics").mkdir(parents=True)
            (root / "eth0" / "carrier").write_text("1\n")
            (root / "eth0" / "statistics" / "rx_errors").write_text("7\n")
            nic = diag.read_nic("eth0", root)
            self.assertEqual(nic["carrier"], 1)
            self.assertEqual(nic["counters"]["rx_errors"], 7)
            self.assertNotIn("tx_errors", nic["counters"])
            self.assertIn("tx_errors", nic["unreadable_counters"])

    def test_log_rotation_keeps_valid_bounded_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telemetry.jsonl"
            log = diag.JsonLog(path, max_bytes=450, backups=2)
            for i in range(30):
                log.write("sample", index=i)
            files = list(Path(tmp).glob("telemetry.jsonl*"))
            self.assertEqual(len(files), 3)
            indices = []
            for file in files:
                self.assertLessEqual(file.stat().st_size, 450)
                indices.extend(json.loads(line)["index"] for line in file.read_text().splitlines())
            self.assertIn(29, indices)
            self.assertNotIn(0, indices)

    def test_invalid_config_fails_before_any_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.json"
            invalid = [
                {"name": "a", "interface": "eth0;reboot", "ip": "192.168.0.1"},
                {"name": "a", "interface": "eth0", "ip": "camera.local"},
                {"name": "a", "interface": "eth0", "ip": "239.1.2.3"},
                {"name": "a", "interface": "eth0", "ip": 123},
                {"name": "a", "interface": "eth0", "ip": "192.168.0.1", "tcp_port": 0},
                {"name": "a", "interface": "eth0", "heartbeat_file": "relative.txt"},
            ]
            for target in invalid:
                with self.subTest(target=target):
                    path.write_text(json.dumps({"targets": [target]}))
                    with self.assertRaises(ValueError):
                        diag.load_config(path)
            path.write_text(json.dumps({"targets": [{"name": "a", "interface": "eth0", "ip": "192.168.0.1"}]}))
            self.assertEqual(diag.load_config(path)[0]["name"], "a")

    def test_passive_mode_never_probes_network(self):
        with patch.object(diag, "command") as command, patch.object(diag.socket, "socket") as sock:
            result = diag.probe_target({"name": "camera", "interface": "eth0", "ip": "192.168.0.1", "tcp_port": 80}, False)
            command.assert_not_called()
            sock.assert_not_called()
            self.assertNotIn("icmp", result)

    def test_no_reply_and_missing_ping_are_distinct(self):
        target = {"name": "camera", "interface": "eth0", "ip": "192.168.0.1"}
        for reply, expected in [({"exit_code": 0}, "reply"), ({"exit_code": 1}, "no_reply"),
                                ({"exit_code": 2}, "unavailable"), ({"available": False}, "unavailable")]:
            with patch.object(diag, "command", return_value=reply) as cmd:
                result = diag.probe_target(target, True)
                self.assertEqual(result["icmp"]["status"], expected)
                self.assertIn("-I", cmd.call_args[0][0])

    def test_tcp_permission_error_does_not_fallback_to_unbound_connect(self):
        target = {"name": "camera", "interface": "eth0", "ip": "192.168.0.1", "tcp_port": 1234}
        with patch.object(diag, "command", return_value={"exit_code": 0}), patch.object(diag.socket, "socket") as factory:
            stream = factory.return_value.__enter__.return_value
            stream.setsockopt.side_effect = PermissionError("denied")
            result = diag.probe_target(target, True)
            stream.connect.assert_not_called()
            self.assertEqual(result["tcp"]["status"], "unavailable")

    def test_heartbeat_fresh_stale_missing_and_clock_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last-frame"
            target = {"name": "camera", "interface": "eth0", "heartbeat_file": str(path), "max_age_seconds": 5}
            self.assertEqual(diag.probe_target(target, False)["heartbeat"]["status"], "unavailable")
            path.touch()
            modified = path.stat().st_mtime
            for now, state in ((modified + 1, "fresh"), (modified + 10, "stale"), (modified - 10, "clock_error")):
                with patch.object(diag.time, "time", return_value=now):
                    self.assertEqual(diag.probe_target(target, False)["heartbeat"]["status"], state)

    def test_report_orders_events_and_flags_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telemetry.jsonl"
            rows = [
                {"time": "2026-01-01T00:00:00Z", "kind": "sample"},
                {"time": "2026-01-01T00:00:02Z", "kind": "event", "monotonic_seconds": 2, "event": "b"},
                {"time": "2026-01-01T00:00:01Z", "kind": "event", "monotonic_seconds": 1, "event": "a"},
            ]
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(diag.report(tmp), 0)
            report = json.loads(output.getvalue())
            self.assertEqual([r["event"] for r in report["events"]], ["a", "b"])
            self.assertEqual(report["samples"], 1)
            with path.open("a") as stream:
                stream.write("incomplete\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(diag.report(tmp), 1)

    def test_loopback_tcp_connect_when_binding_is_permitted(self):
        # Exercises an actual local connection, never a production camera.
        with socket.socket() as check:
            try:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"lo\0")
            except PermissionError:
                self.skipTest("SO_BINDTODEVICE permission unavailable")
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            server.settimeout(4)
            target = {"name": "local", "interface": "lo", "ip": "127.0.0.1", "tcp_port": server.getsockname()[1]}
            accepted = []
            def accept():
                conn, _ = server.accept()
                conn.close()
                accepted.append(True)
            worker = threading.Thread(target=accept)
            worker.start()
            try:
                with patch.object(diag, "command", return_value={"exit_code": 0}):
                    result = diag.probe_target(target, True)
                self.assertEqual(result["tcp"]["status"], "connected")
            finally:
                worker.join(timeout=5)
            self.assertEqual(accepted, [True])


if __name__ == "__main__":
    unittest.main()
