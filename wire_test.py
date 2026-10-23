#!/usr/bin/env python3
"""Cable-only bidirectional Ethernet test; no sensor, IP or route changes."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import threading
import time
import uuid
import zlib

import sensor_lab as lab
from load_test import guard

PROTOCOL = 0x88B5
HEADER = struct.Struct('!8s16sBQ')
MAGIC = b'SENSLAB1'


class SequenceWindow:
    def __init__(self, size=4096):
        self.size, self.limit, self.highest, self.bits = size, (1 << size) - 1, -1, 0

    def accept(self, sequence):
        if sequence > self.highest:
            shift = sequence - self.highest
            self.bits = 1 if shift >= self.size else ((self.bits << shift) | 1) & self.limit
            self.highest = sequence
            return 'new'
        distance = self.highest - sequence
        if distance >= self.size:
            return 'too_old'
        bit = 1 << distance
        if self.bits & bit:
            return 'duplicate'
        self.bits |= bit
        return 'reordered'


def frame(source, destination, session, direction, sequence, payload_size):
    body = HEADER.pack(MAGIC, session, direction, sequence)
    body += b'\xa5' * (payload_size - HEADER.size - 4)
    return destination + source + struct.pack('!H', PROTOCOL) + body + struct.pack('!I', zlib.crc32(body))


def decode(data, source, destination, session, direction):
    if len(data) < 14 + HEADER.size + 4 or data[:12] != destination + source or data[12:14] != struct.pack('!H', PROTOCOL):
        return None
    payload = data[14:]
    magic, token, channel, sequence = HEADER.unpack(payload[:HEADER.size])
    if (magic, token, channel) != (MAGIC, session, direction):
        return None
    if zlib.crc32(payload[:-4]) != struct.unpack('!I', payload[-4:])[0]:
        return 'corrupt'
    return sequence


def preflight(names, payload_size):
    if len(set(names)) != 2:
        raise ValueError('two different Ethernet ports are required')
    names = lab.interfaces(names)
    result = {name: lab.nic(name) for name in names}
    for name, value in result.items():
        path = Path('/sys/class/net') / name
        if not (path / 'device').exists() or lab.read(path / 'type') != '1' or (path / 'master').exists():
            raise ValueError(name + ': use standalone physical Ethernet, not a bridge/bond/loopback')
        if value.get('carrier') != 1 or value.get('speed', 0) is None or value.get('speed', 0) <= 0 or value.get('duplex') != 'full':
            raise ValueError(name + ': connect a direct known-good cable and verify link/speed/full duplex')
        if payload_size > (value.get('mtu') or 0):
            raise ValueError('payload exceeds current MTU')
    if len({v['mac'] for v in result.values()}) != 2:
        raise ValueError('ports need distinct MAC addresses')
    for family in ('-4', '-6'):
        route = lab.command(['ip', '-j', family, 'route', 'show', 'table', 'all'])
        if route['status'] != 'ok':
            raise ValueError('iproute2 is required to protect management interfaces')
        for item in json.loads(route['stdout']):
            devices = [item.get('dev')] + [hop.get('dev') for hop in item.get('nexthops', [])]
            if item.get('dst') in ('default', '0.0.0.0/0', '::/0') and set(devices) & set(names):
                raise ValueError('a default-route/management port cannot be used for this cable test')
    return result


class Direction:
    def __init__(self, source, destination, src_mac, dst_mac, session, number, args):
        self.source, self.destination = source, destination
        self.src_mac, self.dst_mac, self.session, self.number = src_mac, dst_mac, session, number
        self.args, self.tx, self.rx = args, None, None
        self.done, self.lock = threading.Event(), threading.Lock()
        self.receiver, self.error, self.capture_drops = None, None, None
        self.stats = {key: 0 for key in ('sent', 'received_unique', 'tx_errors', 'duplicate', 'reordered', 'too_old', 'corrupt')}

    def open(self):
        self.tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(PROTOCOL))
        self.tx.bind((self.source, 0))
        self.tx.settimeout(0.2)
        self.rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(PROTOCOL))
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.rx.bind((self.destination, 0))
        self.rx.settimeout(0.2)
        self.receiver = threading.Thread(target=self.receive, daemon=True)
        self.receiver.start()

    def receive(self):
        window = SequenceWindow()
        try:
            while not self.done.is_set():
                try:
                    packet, address = self.rx.recvfrom(65535)
                except socket.timeout:
                    continue
                if address[2] == socket.PACKET_OUTGOING:
                    continue
                sequence = decode(packet, self.src_mac, self.dst_mac, self.session, self.number)
                if sequence is None:
                    continue
                with self.lock:
                    state = 'corrupt' if sequence == 'corrupt' else window.accept(sequence)
                    if state in ('new', 'reordered'):
                        self.stats['received_unique'] += 1
                    if state != 'new':
                        self.stats[state] += 1
        except OSError as exc:
            self.error = str(exc)

    def send(self, started, stop):
        pps = self.args.mbps * 1e6 / ((self.args.payload_size + 38) * 8)
        sequence = 0
        try:
            while not stop.is_set() and time.monotonic() - started < self.args.duration:
                due = int((time.monotonic() - started) * pps) - sequence
                if due <= 0:
                    stop.wait(0.001)
                    continue
                for _ in range(min(due, 128)):
                    if stop.is_set() or time.monotonic() - started >= self.args.duration:
                        return
                    packet = frame(self.src_mac, self.dst_mac, self.session, self.number, sequence, self.args.payload_size)
                    sequence += 1
                    try:
                        if self.tx.send(packet) != len(packet):
                            raise OSError('partial send')
                        with self.lock:
                            self.stats['sent'] += 1
                    except OSError:
                        with self.lock:
                            self.stats['tx_errors'] += 1
        except Exception as exc:
            self.error = str(exc)

    def snapshot(self):
        with self.lock:
            return dict(self.stats, source=self.source, destination=self.destination,
                        error=self.error, capture_socket_drops=self.capture_drops)

    def close(self):
        self.done.set()
        if self.receiver:
            self.receiver.join(timeout=1)
        if self.rx:
            try:
                self.capture_drops = struct.unpack('II', self.rx.getsockopt(263, 6, 12)[:8])[1]
            except OSError as exc:
                self.error = 'capture statistics unavailable: ' + str(exc)
        for stream in (self.tx, self.rx):
            if stream:
                stream.close()


def assess(rows, elapsed, args, events, reason):
    for row in rows:
        row['unreceived'] = max(0, row['sent'] - row['received_unique'])
        row['offered_wire_mbps'] = row['sent'] * (args.payload_size + 38) * 8 / max(elapsed, 0.001) / 1e6
    if reason != 'duration_completed' or any(r['error'] or not r['sent'] or r['too_old'] or r['capture_socket_drops'] is None or r['capture_socket_drops'] for r in rows):
        verdict = 'INCONCLUSIVE_OR_STOPPED'
    elif events or any(any(r[key] for key in ('unreceived', 'tx_errors', 'duplicate', 'reordered', 'corrupt')) for r in rows):
        verdict = 'ANOMALY_OBSERVED'
    elif any(r['offered_wire_mbps'] < args.mbps * 0.8 for r in rows):
        verdict = 'INCONCLUSIVE_RATE_TOO_LOW'
    else:
        verdict = 'NO_ANOMALY_AT_THIS_RATE'
    return {'verdict': verdict, 'stop_reason': reason, 'elapsed_seconds': elapsed,
            'directions': rows, 'event_count': events,
            'scope': 'Raw Ethernet test at observed negotiated speed and achieved rate. Not 10G, camera, SDK or game certification.'}


def execute(args):
    initial = preflight(args.interfaces, args.payload_size)
    limit = min(v['speed'] for v in initial.values())
    print(json.dumps({'ports': initial, 'requested_mbps_each_direction': args.mbps,
                      'thermal': lab.thermal_status(lab.temperatures()), 'will_send': args.run}, indent=2))
    if args.mbps > limit * 0.8:
        raise ValueError('test rate exceeds 80% of lowest negotiated link speed')
    if not args.run:
        print('Preflight only. Confirm isolated equipment and a direct cable, then add --run.')
        return 0
    blocked = guard(lab.metrics(args.interfaces), lab.CPU_LIMIT, lab.NVME_LIMIT)
    if blocked:
        raise ValueError('test not started: ' + json.dumps(blocked))
    stop, user_stop = threading.Event(), threading.Event()
    def interrupted(*_):
        user_stop.set()
        stop.set()
    handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    recorder = lab.Recorder(args.interfaces, args.output, 'wire')
    channels, senders, reason, count, elapsed = [], [], 'setup_incomplete', 0, 0
    try:
        recorder.open()
        snapshot, _ = recorder.sample()
        if guard(snapshot, lab.CPU_LIMIT, lab.NVME_LIMIT):
            raise ValueError('temperature guard blocked test after inventory')
        session = uuid.uuid4().bytes
        for n in (0, 1):
            source, destination = args.interfaces[n], args.interfaces[1 - n]
            channel = Direction(source, destination, bytes.fromhex(initial[source]['mac'].replace(':', '')),
                                bytes.fromhex(initial[destination]['mac'].replace(':', '')), session, n, args)
            channels.append(channel)
            channel.open()
        started = time.monotonic()
        for channel in channels:
            sender = threading.Thread(target=channel.send, args=(started, stop), daemon=True)
            senders.append(sender)
            sender.start()
        reason = 'duration_completed'
        while not stop.is_set():
            snapshot, events = recorder.sample()
            count += len(events)
            recorder.log.write('wire_sample', directions=[c.snapshot() for c in channels])
            elapsed = time.monotonic() - started
            blocked = guard(snapshot, lab.CPU_LIMIT, lab.NVME_LIMIT)
            if blocked:
                recorder.log.write('event', event='load_guard', details=blocked)
                reason = 'temperature_or_memory_guard'
                break
            if any(c.error for c in channels):
                reason = 'test_receiver_error'
                break
            if elapsed >= args.duration:
                break
            stop.wait(min(1, args.duration - elapsed))
        elapsed = time.monotonic() - started
        if user_stop.is_set():
            reason = 'user_stopped'
    finally:
        stop.set()
        for sender in senders:
            sender.join(timeout=2)
        time.sleep(0.3)
        for channel in channels:
            channel.close()
        result = assess([c.snapshot() for c in channels], elapsed, args, count, reason)
        recorder.log.write('event', event='wire_result', **result)
        lab.write_json(recorder.directory / 'wire-result.json', result)
        recorder.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    print(json.dumps(result, indent=2))
    return 0 if result['verdict'] == 'NO_ANOMALY_AT_THIS_RATE' else 2 if result['verdict'].startswith('INCONCLUSIVE') else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interfaces', nargs=2, required=True)
    parser.add_argument('--duration', type=int, default=60)
    parser.add_argument('--mbps', type=float, default=50, help='estimated wire Mb/s in EACH direction')
    parser.add_argument('--payload-size', type=int, default=1400)
    parser.add_argument('--output', default='./results')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    if not 5 <= args.duration <= 3600 or not 0.1 <= args.mbps <= 9000 or not 128 <= args.payload_size <= 1500:
        parser.error('invalid duration, rate or payload size')
    return execute(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(2)
