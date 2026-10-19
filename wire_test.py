#!/usr/bin/env python3
"""Camera-free, bidirectional Ethernet cable test between two Linux NICs.

Uses private test Ethernet frames (0x88b5), never IP loopback routing.
No IP, route, MTU, offload, EEE or link-state settings are changed.
Run only on isolated intake equipment with a direct patch cable.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid
import zlib

import lan_diag as diag

ETHERTYPE = 0x88B5  # IEEE local experimental EtherType
MAGIC = b"LANDIAG1"
HEADER = struct.Struct("!8s16sBQ")


class SequenceWindow:
    """Bounded duplicate/reordering detection; old frames stay explicitly unknown."""
    def __init__(self, width=4096):
        self.width, self.highest, self.bits = width, -1, 0

    def accept(self, sequence):
        if sequence > self.highest:
            shift = sequence - self.highest
            self.bits = 1 if shift >= self.width else ((self.bits << shift) | 1) & ((1 << self.width) - 1)
            self.highest = sequence
            return "new"
        distance = self.highest - sequence
        if distance >= self.width:
            return "too_old"
        bit = 1 << distance
        if self.bits & bit:
            return "duplicate"
        self.bits |= bit
        return "reordered"


def make_frame(source, destination, session, direction, sequence, payload_size):
    body = HEADER.pack(MAGIC, session, direction, sequence)
    body += b"\xa5" * (payload_size - HEADER.size - 4)
    body += struct.pack("!I", zlib.crc32(body))
    return destination + source + struct.pack("!H", ETHERTYPE) + body


def parse_frame(frame, source, destination, session, direction):
    if len(frame) < 14 + HEADER.size + 4 or frame[:6] != destination or frame[6:12] != source:
        return None
    if frame[12:14] != struct.pack("!H", ETHERTYPE):
        return None
    body = frame[14:]
    magic, identifier, number, sequence = HEADER.unpack(body[:HEADER.size])
    if (magic, identifier, number) != (MAGIC, session, direction):
        return None
    if zlib.crc32(body[:-4]) != struct.unpack("!I", body[-4:])[0]:
        return {"corrupt": True}
    return {"sequence": sequence}


def preflight(interfaces, payload_size, root=Path("/sys/class/net")):
    if len(set(interfaces)) != 2:
        raise ValueError("choose two different interfaces")
    result = {}
    for iface in interfaces:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", iface) or iface.startswith("-"):
            raise ValueError("invalid interface name")
        base = root / iface
        if not (base / "device").exists():
            raise ValueError(iface + ": requires a physical Ethernet NIC, not loopback/veth")
        if (base / "master").exists():
            raise ValueError(iface + ": bridge/bond member is not supported; use a standalone NIC")
        if diag.read_text(base / "type") != "1":
            raise ValueError(iface + ": not an Ethernet interface")
        nic = diag.read_nic(iface, root)
        flags = int(diag.read_text(base / "flags") or "0", 16)
        if not flags & 1:
            raise ValueError(iface + ": interface is DOWN; enable it manually on the isolated test bench")
        if nic.get("carrier") != 1:
            raise ValueError(iface + ": no link; connect the two ports with a known-good cable")
        if nic.get("speed", 0) < 1000 or nic.get("duplex") != "full":
            raise ValueError(iface + ": expected at least 1000 Mb/s full duplex; investigate cable/link negotiation first")
        if payload_size > nic.get("mtu", 0):
            raise ValueError(iface + ": payload exceeds current MTU")
        result[iface] = nic
    if result[interfaces[0]].get("address") == result[interfaces[1]].get("address"):
        raise ValueError("interfaces must have distinct MAC addresses")
    # Protect likely management interfaces. IP routes are never changed.
    for family in ("-4", "-6"):
        routes = diag.command(["ip", "-j", family, "route", "show", "table", "all"])
        if routes.get("exit_code") != 0:
            raise ValueError("iproute2 is required to check management/default routes")
        for route in json.loads(routes["stdout"]):
            devices = [route.get("dev")] + [hop.get("dev") for hop in route.get("nexthops", [])]
            if route.get("dst") in ("default", "0.0.0.0/0", "::/0") and set(devices) & set(interfaces):
                raise ValueError("test ports include a default-route interface; use isolated non-management ports")
    return result


class Direction:
    def __init__(self, source, destination, source_mac, destination_mac, session, number, args):
        self.source, self.destination = source, destination
        self.source_mac, self.destination_mac = source_mac, destination_mac
        self.session, self.number, self.args = session, number, args
        self.stats = {key: 0 for key in ("tx_frames", "tx_errors", "rx_unique", "duplicates", "reordered", "too_old", "corrupt")}
        self.lock = threading.Lock()
        self.stop_receive = threading.Event()
        self.error = None
        self.receiver = None
        self.tx, self.rx = None, None
        self.receive_buffer_bytes = None
        self.capture_socket_drops = None
        self.capture_stats_error = None
        self.last_tx_error = None

    def open(self):
        self.tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
        self.tx.bind((self.source, 0))
        self.tx.settimeout(0.2)
        self.rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.receive_buffer_bytes = self.rx.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        self.rx.bind((self.destination, 0))
        self.rx.settimeout(0.2)
        self.receiver = threading.Thread(target=self.receive, daemon=True)
        self.receiver.start()

    def receive(self):
        window = SequenceWindow()
        try:
            while not self.stop_receive.is_set():
                try:
                    frame, address = self.rx.recvfrom(65535)
                except socket.timeout:
                    continue
                if address[2] == socket.PACKET_OUTGOING:
                    continue
                record = parse_frame(frame, self.source_mac, self.destination_mac, self.session, self.number)
                if record is None:
                    continue
                with self.lock:
                    if record.get("corrupt"):
                        self.stats["corrupt"] += 1
                    else:
                        state = window.accept(record["sequence"])
                        if state in ("new", "reordered"):
                            self.stats["rx_unique"] += 1
                        if state != "new":
                            key = "duplicates" if state == "duplicate" else state
                            self.stats[key] += 1
        except OSError as exc:
            self.error = str(exc)

    def send(self, start, stop):
        # Requested rate includes Ethernet FCS, preamble and inter-frame gap.
        wire_bytes = self.args.payload_size + 14 + 24
        packets_per_second = self.args.mbps * 1e6 / (wire_bytes * 8)
        sequence = 0
        attempted = 0
        try:
            while not stop.is_set():
                now = time.monotonic()
                if now >= start + self.args.duration:
                    return
                due = int((now - start) * packets_per_second) - attempted
                if due <= 0:
                    stop.wait(0.001)
                    continue
                # Bound catch-up bursts; never build an unbounded work queue.
                for _ in range(min(due, 128)):
                    if stop.is_set() or time.monotonic() >= start + self.args.duration:
                        return
                    frame = make_frame(self.source_mac, self.destination_mac, self.session,
                                       self.number, sequence, self.args.payload_size)
                    sequence += 1
                    attempted += 1
                    try:
                        count = self.tx.send(frame)
                        if count != len(frame):
                            raise OSError("partial Ethernet frame send")
                        with self.lock:
                            self.stats["tx_frames"] += 1
                    except OSError as exc:
                        with self.lock:
                            self.stats["tx_errors"] += 1
                            self.last_tx_error = str(exc)
        except Exception as exc:
            self.error = str(exc)

    def snapshot(self):
        with self.lock:
            return dict(self.stats, source=self.source, destination=self.destination,
                        receiver_error=self.error, receive_buffer_bytes=self.receive_buffer_bytes,
                        capture_socket_drops=self.capture_socket_drops,
                        capture_stats_error=self.capture_stats_error,
                        last_tx_error=self.last_tx_error)

    def close(self):
        self.stop_receive.set()
        if self.receiver is not None:
            self.receiver.join(timeout=1)
        if self.rx is not None:
            try:
                # SOL_PACKET/PACKET_STATISTICS: distinguish test receiver overflow
                # from packet loss elsewhere. Reading resets these socket stats.
                raw = self.rx.getsockopt(263, 6, 12)
                _, self.capture_socket_drops = struct.unpack("II", raw[:8])
            except OSError as exc:
                self.capture_stats_error = str(exc)
        for stream in (self.tx, self.rx):
            if stream is not None:
                stream.close()


def assess(directions, elapsed, requested_mbps, payload_size, nic_events, interrupted):
    rows = []
    for direction in directions:
        row = dict(direction)
        row["unreceived_frames"] = max(0, row["tx_frames"] - row["rx_unique"])
        row["offered_wire_mbps"] = row["tx_frames"] * (payload_size + 38) * 8 / max(elapsed, 0.001) / 1e6
        row["loss_percent"] = (100 * row["unreceived_frames"] / row["tx_frames"]) if row["tx_frames"] else None
        rows.append(row)
    if interrupted or any(r["receiver_error"] or not r["tx_frames"] or r["too_old"] for r in rows):
        verdict = "INCONCLUSIVE"
    elif any(r.get("capture_socket_drops") or r.get("capture_stats_error") for r in rows):
        verdict = "INCONCLUSIVE_CAPTURE_OVERRUN_OR_UNAVAILABLE_STATS"
    elif any(r["unreceived_frames"] or r["tx_errors"] or r["corrupt"] or r["duplicates"] or r["reordered"] for r in rows) or nic_events:
        verdict = "ANOMALY_OBSERVED"
    elif any(r["offered_wire_mbps"] < requested_mbps * 0.8 for r in rows):
        verdict = "INCONCLUSIVE_RATE_TOO_LOW"
    else:
        verdict = "PASS_FOR_THIS_WIRE_TEST_ONLY"
    return {"verdict": verdict, "directions": rows, "elapsed_seconds": elapsed,
            "nic_event_count": nic_events,
            "scope": "Physical port-pair Ethernet test only. Not camera, GigE Vision, SDK, PoE or field-environment certification."}


def run(args):
    initial = preflight(args.interfaces, args.payload_size)
    print(json.dumps({"interfaces": initial, "requested_wire_mbps_per_direction": args.mbps,
                      "duration_seconds": args.duration, "will_send": args.run}, indent=2), flush=True)
    if not args.run:
        print("Preflight only. Confirm a direct cable on isolated equipment, then add --run.")
        return 0
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="wire-", dir=str(output)))
    os.chmod(directory, 0o700)
    print("Log directory:", directory, flush=True)
    log = diag.JsonLog(directory / "telemetry.jsonl")
    session = uuid.uuid4().bytes
    directions = []
    stop = threading.Event()
    senders = []
    old_handlers = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    nic_event_count = 0
    started = None
    elapsed = 0
    try:
        diag.write_json(directory / "inventory-start.json", diag.inventory(args.interfaces))
        diag.write_json(directory / "run.json", vars(args))
        for number in (0, 1):
            source, destination = args.interfaces[number], args.interfaces[1 - number]
            source_mac = bytes.fromhex(initial[source]["address"].replace(":", ""))
            dest_mac = bytes.fromhex(initial[destination]["address"].replace(":", ""))
            direction = Direction(source, destination, source_mac, dest_mac, session, number, args)
            directions.append(direction)
            direction.open()
        # Re-baseline after inventory; no test frames have been sent yet.
        previous = {iface: diag.read_nic(iface) for iface in args.interfaces}
        started = last = time.monotonic()
        for direction in directions:
            worker = threading.Thread(target=direction.send, args=(started, stop), daemon=True)
            senders.append(worker)
            worker.start()
        while True:
            now = time.monotonic()
            current = {iface: diag.read_nic(iface) for iface in args.interfaces}
            for iface in args.interfaces:
                events = diag.nic_changes(iface, current[iface], previous[iface], now - last)
                for event in events:
                    log.write("event", **event)
                nic_event_count += len(events)
            log.write("sample", nics=current, directions=[d.snapshot() for d in directions],
                      system=diag.system_sample())
            previous, last = current, now
            if stop.is_set() or now >= started + args.duration or any(d.error for d in directions):
                break
            stop.wait(min(1, max(0, started + args.duration - time.monotonic())))
        elapsed = time.monotonic() - started
    finally:
        interrupted = stop.is_set()
        stop.set()
        for worker in senders:
            worker.join(timeout=2)
        # Drain queued receive frames before comparing sent and received counts.
        time.sleep(0.5)
        for direction in directions:
            direction.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    final_nics = {iface: diag.read_nic(iface) for iface in args.interfaces}
    for iface in args.interfaces:
        events = diag.nic_changes(iface, final_nics[iface], previous[iface], time.monotonic() - last)
        for event in events:
            log.write("event", **event)
        nic_event_count += len(events)
    summary = assess([d.snapshot() for d in directions], elapsed, args.mbps,
                     args.payload_size, nic_event_count, interrupted)
    diag.write_json(directory / "summary.json", summary)
    diag.write_json(directory / "inventory-end.json", diag.inventory(args.interfaces))
    log.write("wire_result", **summary)
    print(json.dumps(summary, indent=2), flush=True)
    print("Results:", directory, flush=True)
    if summary["verdict"] == "PASS_FOR_THIS_WIRE_TEST_ONLY":
        return 0
    return 2 if summary["verdict"].startswith("INCONCLUSIVE") else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interfaces", nargs=2, required=True, metavar=("PORT_A", "PORT_B"))
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--mbps", type=float, default=10, help="wire Mb/s in EACH direction, default 10")
    parser.add_argument("--payload-size", type=int, default=1400, help="Ethernet payload bytes, default 1400")
    parser.add_argument("--output", default="./lan-diag-logs")
    parser.add_argument("--run", action="store_true", help="actually send frames; otherwise preflight only")
    args = parser.parse_args(argv)
    if (not math.isfinite(args.duration) or not 1 <= args.duration <= 604800 or
            not math.isfinite(args.mbps) or not 0.1 <= args.mbps <= 900 or
            not 64 <= args.payload_size <= 1500):
        parser.error("duration must be 1..604800 s, mbps 0.1..900, payload 64..1500")
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PermissionError:
        print("ERROR: raw Ethernet frames require root/CAP_NET_RAW; use sudo on the isolated test unit.", file=sys.stderr)
        sys.exit(2)
    except (OSError, ValueError) as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(2)
