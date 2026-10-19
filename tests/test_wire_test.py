import argparse
import json
from pathlib import Path
import queue
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import wire_test as wire


class WireTests(unittest.TestCase):
    def test_frame_validates_peer_session_direction_and_crc(self):
        src, dst = bytes.fromhex("020000000001"), bytes.fromhex("020000000002")
        session = bytes(range(16))
        frame = wire.make_frame(src, dst, session, 0, 123, 1400)
        self.assertEqual(len(frame), 1414)
        self.assertEqual(wire.parse_frame(frame, src, dst, session, 0), {"sequence": 123})
        self.assertIsNone(wire.parse_frame(frame, dst, src, session, 0))
        self.assertIsNone(wire.parse_frame(frame, src, dst, b"x" * 16, 0))
        self.assertIsNone(wire.parse_frame(frame, src, dst, session, 1))
        broken = frame[:100] + bytes([frame[100] ^ 1]) + frame[101:]
        self.assertEqual(wire.parse_frame(broken, src, dst, session, 0), {"corrupt": True})
        self.assertIsNone(wire.parse_frame(b"short", src, dst, session, 0))

    def test_reorder_duplicates_and_bounded_window(self):
        window = wire.SequenceWindow(width=8)
        self.assertEqual(window.accept(0), "new")
        self.assertEqual(window.accept(2), "new")
        self.assertEqual(window.accept(1), "reordered")
        self.assertEqual(window.accept(1), "duplicate")
        self.assertEqual(window.accept(10000), "new")
        self.assertEqual(window.accept(0), "too_old")
        self.assertLessEqual(window.bits.bit_length(), 8)

    def test_verdict_separates_loss_from_insufficient_load_and_capture_overrun(self):
        row = {"tx_frames": 1000, "tx_errors": 0, "rx_unique": 1000,
               "duplicates": 0, "reordered": 0, "too_old": 0, "corrupt": 0,
               "receiver_error": None, "capture_socket_drops": 0, "capture_stats_error": None}
        duration = 1000 * 1438 * 8 / 1e6 / 10
        def verdict(data, **kwargs):
            return wire.assess([data], kwargs.get("elapsed", duration), 10, 1400,
                               kwargs.get("nic_events", 0), kwargs.get("interrupted", False))["verdict"]
        self.assertEqual(verdict(row), "PASS_FOR_THIS_WIRE_TEST_ONLY")
        self.assertEqual(verdict(dict(row, rx_unique=999)), "ANOMALY_OBSERVED")
        self.assertEqual(verdict(row, nic_events=1), "ANOMALY_OBSERVED")
        self.assertEqual(verdict(row, elapsed=duration * 2), "INCONCLUSIVE_RATE_TOO_LOW")
        self.assertEqual(verdict(row, interrupted=True), "INCONCLUSIVE")
        self.assertEqual(verdict(dict(row, rx_unique=0, capture_socket_drops=10)),
                         "INCONCLUSIVE_CAPTURE_OVERRUN_OR_UNAVAILABLE_STATS")

    def test_preflight_rejects_loopback_and_duplicate_interfaces(self):
        for interfaces in (["lo", "lo"], ["lo", "eth0"]):
            with self.assertRaises(ValueError):
                wire.preflight(interfaces, 1400)

    def test_preflight_checks_link_and_management_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number, iface in enumerate(("nic1", "nic2")):
                base = root / iface
                (base / "device").mkdir(parents=True)
                for key, value in {"type": "1", "flags": "0x1003", "carrier": "1", "speed": "1000",
                                   "duplex": "full", "mtu": "1500", "address": "02:00:00:00:00:0" + str(number)}.items():
                    (base / key).write_text(value)
            with patch.object(wire.diag, "command", return_value={"exit_code": 0, "stdout": "[]"}):
                result = wire.preflight(["nic1", "nic2"], 1400, root)
                self.assertEqual(result["nic1"]["speed"], 1000)
                (root / "nic1" / "speed").write_text("100")
                with self.assertRaisesRegex(ValueError, "1000"):
                    wire.preflight(["nic1", "nic2"], 1400, root)
                (root / "nic1" / "speed").write_text("1000")
            routes = {"exit_code": 0, "stdout": json.dumps([{"dst": "default", "dev": "nic1"}])}
            with patch.object(wire.diag, "command", return_value=routes):
                with self.assertRaisesRegex(ValueError, "default-route"):
                    wire.preflight(["nic1", "nic2"], 1400, root)

    def test_send_receive_pipeline_detects_deliberate_loss_and_ignores_tx_copy(self):
        # A synthetic packet transport verifies the complete pipeline without
        # claiming that any real NIC/cable traffic has been tested.
        packets = queue.Queue()
        class PacketSocket:
            def bind(self, address):
                pass
            def settimeout(self, timeout):
                pass
            def setsockopt(self, *args):
                pass
            def getsockopt(self, level, option, *args):
                return struct.pack("II", 0, 0) if level == 263 else 4096
            def close(self):
                pass
            def send(self, frame):
                sequence = wire.HEADER.unpack(frame[14:14 + wire.HEADER.size])[-1]
                packets.put((frame, ("b", wire.ETHERTYPE, socket.PACKET_OUTGOING)))
                if sequence != 1:
                    packets.put((frame, ("b", wire.ETHERTYPE, socket.PACKET_HOST)))
                return len(frame)
            def recvfrom(self, size):
                try:
                    return packets.get(timeout=0.05)
                except queue.Empty:
                    raise socket.timeout()
        args = argparse.Namespace(mbps=0.1, payload_size=256, duration=0.25)
        direction = wire.Direction("a", "b", b"a" * 6, b"b" * 6, b"s" * 16, 0, args)
        stop = threading.Event()
        with patch.object(wire.socket, "socket", side_effect=lambda *args: PacketSocket()):
            try:
                direction.open()
                direction.send(time.monotonic(), stop)
                limit = time.monotonic() + 2
                while not packets.empty() and time.monotonic() < limit:
                    time.sleep(0.01)
            finally:
                direction.close()
        stats = direction.snapshot()
        self.assertGreater(stats["tx_frames"], 2)
        self.assertEqual(stats["rx_unique"], stats["tx_frames"] - 1)
        self.assertEqual(stats["duplicates"], 0)
        self.assertIsNone(stats["receiver_error"])
        result = wire.assess([stats], 0.25, 0.1, 256, 0, False)
        self.assertEqual(result["verdict"], "ANOMALY_OBSERVED")
        self.assertEqual(result["directions"][0]["unreceived_frames"], 1)

    def test_raw_ethernet_loopback_transport_when_permitted(self):
        # Checks AF_PACKET code and pacing locally. This is NOT a cable/NIC test.
        try:
            check = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(wire.ETHERTYPE))
        except PermissionError:
            self.skipTest("CAP_NET_RAW unavailable")
        check.close()
        args = argparse.Namespace(mbps=0.1, payload_size=256, duration=0.3)
        direction = wire.Direction("lo", "lo", bytes.fromhex("020000000001"),
                                   bytes.fromhex("020000000002"), b"a" * 16, 0, args)
        stop = threading.Event()
        try:
            direction.open()
            sender = threading.Thread(target=direction.send, args=(time.monotonic(), stop))
            sender.start()
            sender.join(timeout=2)
            time.sleep(0.1)
        finally:
            stop.set()
            direction.close()
        stats = direction.snapshot()
        self.assertIsNone(stats["receiver_error"])
        self.assertGreater(stats["tx_frames"], 0)
        self.assertEqual(stats["rx_unique"], stats["tx_frames"])
        self.assertEqual(stats["duplicates"], 0)
        self.assertEqual(stats["corrupt"], 0)
        self.assertEqual(stats["capture_socket_drops"], 0)


if __name__ == "__main__":
    unittest.main()
