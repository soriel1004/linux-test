import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import queue
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import load_test as load
import sensor_lab as lab
import wire_test as wire

ROOT = Path(__file__).resolve().parent.parent


def safe_snapshot():
    return {'temperatures': [{'kind': 'cpu', 'celsius': 55, 'critical_celsius': 100},
                             {'kind': 'nvme', 'celsius': 45, 'critical_celsius': 85}],
            'memory': {'MemAvailable': 4 * 1024 ** 3}}


class GuardTests(unittest.TestCase):
    def test_missing_sensor_blocks_load(self):
        sample = safe_snapshot()
        sample['temperatures'] = sample['temperatures'][:1]
        self.assertEqual(load.guard(sample, 85, 70)['thermal']['missing'], ['nvme'])

    def test_temperature_limit_and_vendor_critical_ceiling(self):
        sample = safe_snapshot()
        self.assertIsNone(load.guard(sample, 85, 70))
        sample['temperatures'][1]['celsius'] = 70
        self.assertIn('thermal', load.guard(sample, 85, 70))
        sample['temperatures'][1].update(celsius=65, critical_celsius=68)
        sample['temperatures'][1]['celsius'] = 68
        self.assertEqual(load.guard(sample, 85, 70)['thermal']['hot'][0]['stop_limit_celsius'], 68)

    def test_memory_and_nvme_warning_stop(self):
        sample = safe_snapshot()
        sample['memory']['MemAvailable'] = 100 * 1024 ** 2
        self.assertIn('memory', load.guard(sample, 85, 70)['reason'])
        sample = safe_snapshot()
        sample['nvme_warnings'] = {'nvme0': 1}
        self.assertEqual(load.guard(sample, 85, 70)['reason'], 'nvme_critical_warning')

    def test_unsafe_preflight_never_launches_workloads(self):
        with patch.object(lab, 'interfaces', return_value=['eth0']), patch.object(lab, 'metrics', return_value={'temperatures': [], 'memory': {}}), patch.object(subprocess, 'Popen') as spawn:
            args = argparse.Namespace(interfaces=None, cpu_limit=85, nvme_limit=70)
            with self.assertRaisesRegex(ValueError, 'not started'):
                load.execute(args)
            spawn.assert_not_called()

    def test_disk_destination_must_be_nvme_not_tmpfs(self):
        bad = {'status': 'ok', 'stdout': json.dumps({'filesystems': [{'source': 'tmpfs', 'fstype': 'tmpfs', 'target': '/tmp'}]})}
        with patch.object(lab, 'command', return_value=bad):
            with self.assertRaisesRegex(ValueError, 'real NVMe'):
                load.disk_destination('/tmp')
        mount = {'status': 'ok', 'stdout': json.dumps({'filesystems': [{'source': '/dev/mapper/root', 'fstype': 'ext4'}]})}
        block = {'status': 'ok', 'stdout': json.dumps({'blockdevices': [{'name': 'root', 'children': [{'name': 'nvme0n1'}]}]})}
        with patch.object(lab, 'command', side_effect=[mount, block]):
            self.assertIn('block_devices', load.disk_destination('/var/tmp'))

    def test_disk_worker_writes_bounded_own_file_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.sensor-lab-test'
            path.touch()
            stop = threading.Event()
            timer = threading.Timer(0.25, stop.set)
            timer.start()
            try:
                total = load.disk_worker(path, 1, 10, stop)
            finally:
                timer.cancel()
            self.assertGreaterEqual(total, 1024 ** 2)
            self.assertLessEqual(path.stat().st_size, 1024 ** 2)
            self.assertFalse(path.is_symlink())

    def test_disk_worker_refuses_existing_data_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.sensor-lab-existing'
            path.write_text('preserve')
            with self.assertRaises(ValueError):
                load.disk_worker(path, 1, 1, threading.Event())
            self.assertEqual(path.read_text(), 'preserve')
            link = Path(tmp) / '.sensor-lab-link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                load.disk_worker(link, 1, 1, threading.Event())

    def test_stress_commands_verify_and_bound_memory(self):
        args = argparse.Namespace(cpu=True, workers=2, cpu_load=60, duration=10, memory_mb=512)
        commands = load.worker_commands(args)
        self.assertEqual(len(commands), 2)
        self.assertIn('--verify', commands[0])
        self.assertEqual(commands[1][commands[1].index('--vm-bytes') + 1], '512M')

    def test_stop_workers_only_terminates_own_session(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        try:
            load.stop_workers([process])
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_hot_temperature_after_start_stops_worker_and_saves_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            class FakeRecorder:
                def __init__(self, *a):
                    self.directory = root
                    self.log = lab.Log(root / 'telemetry.jsonl')
                    self.calls = 0
                def open(self): pass
                def close(self): (root / 'closed').touch()
                def sample(self):
                    self.calls += 1
                    snapshot = safe_snapshot()
                    if self.calls > 1:
                        snapshot['temperatures'][0]['celsius'] = 90
                    return snapshot, []
            args = argparse.Namespace(interfaces=['lo'], cpu_limit=85, nvme_limit=70,
                                      cpu=True, memory_mb=0, disk_dir=None, output=tmp)
            with patch.object(lab, 'metrics', return_value=safe_snapshot()), patch.object(lab, 'Recorder', FakeRecorder), patch.object(load.shutil, 'which', return_value='/test/stress-ng'), patch.object(load, 'worker_commands', return_value=[[sys.executable, '-c', 'import time; time.sleep(30)']]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(load.execute(args), 2)
            summary = json.loads((root / 'load-result.json').read_text())
            self.assertEqual(summary['stop']['reason'], 'temperature_limit_or_missing_sensor')
            self.assertIsNotNone(summary['worker_exit_codes_after_stop'][0])
            self.assertTrue((root / 'closed').exists())


class EvidenceTests(unittest.TestCase):
    def test_nic_reset_replacement_flap_and_error_deltas(self):
        old = {'present': True, 'ifindex': 1, 'carrier': 1, 'carrier_changes': 1,
               'counters': {'rx_bytes': 100, 'tx_bytes': 500, 'rx_errors': 1}}
        new = {'present': True, 'ifindex': 1, 'carrier': 1, 'carrier_changes': 3,
               'counters': {'rx_bytes': 300, 'tx_bytes': 10, 'rx_errors': 4}}
        events = lab.nic_events('a', new, old, 2)
        self.assertEqual(new['delta']['rx_errors'], 3)
        self.assertNotIn('tx', new['mbps'])
        self.assertTrue(any(e['event'] == 'counter_reset' for e in events))
        self.assertTrue(any(e['event'] == 'link_flaps' for e in events))
        replaced = dict(new, ifindex=2)
        replaced.pop('delta')
        lab.nic_events('a', replaced, old, 2)
        self.assertNotIn('delta', replaced)
        self.assertTrue(lab.nic_events('a', {'present': False}, old, 2))

    def test_temperature_source_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, name in enumerate(('coretemp', 'nvme', 'acpitz')):
                path = root / ('hwmon' + str(index))
                path.mkdir()
                (path / 'name').write_text(name)
                (path / 'temp1_input').write_text('60000')
                (path / 'temp1_crit').write_text('100000')
            sensors = lab.temperatures(root, root / 'absent')
            self.assertEqual([s['kind'] for s in sensors], ['cpu', 'nvme', 'other'])
            self.assertEqual(sensors[1]['celsius'], 60)

    def test_smart_baseline_not_mistaken_for_new_media_errors(self):
        result = {'status': 'ok', 'stdout': '{"critical_warning":"0x01","media_errors":"1,000","num_err_log_entries":2}'}
        baseline, events = lab.smart_observation(result)
        self.assertEqual(baseline['media_errors'], 1000)
        self.assertEqual(events[0]['event'], 'nvme_warning')
        self.assertTrue(events[0]['baseline'])
        _, unchanged = lab.smart_observation(result, baseline)
        self.assertEqual(unchanged, [])
        result['stdout'] = '{"critical_warning":0,"media_errors":1001,"num_err_log_entries":2}'
        _, changes = lab.smart_observation(result, baseline)
        self.assertEqual(changes[0]['delta'], 1)
        self.assertEqual(lab.smart_observation({'status': 'unavailable'}), (None, []))

    def test_rotating_logs_and_report_handle_current_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = lab.Log(root / 'telemetry.jsonl', max_bytes=1200, backups=2)
            for value in range(30):
                log.write('sample', data={'temperatures': [{'kind': 'cpu', 'celsius': value + 20}, {'kind': 'nvme', 'celsius': value + 10}]})
            log.write('event', event='nic_change', interface='<script>')
            self.assertLessEqual(len(list(root.glob('telemetry.jsonl*'))), 3)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lab.report(root), 0)
            report = json.loads((root / 'report.json').read_text())
            self.assertLess(report['samples_retained'], 30)
            self.assertEqual(report['peak_celsius']['cpu'], 49)
            self.assertEqual(report['event_counts']['nic_change'], 1)
            self.assertIn('&lt;script&gt;', (root / 'report.html').read_text())
            with (root / 'telemetry.jsonl').open('a') as stream:
                stream.write('incomplete\n')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lab.report(root), 1)

    def test_real_monitor_sigterm_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = [sys.executable, str(ROOT / 'sensor_lab.py'), 'monitor', '--interfaces', 'lo', '--duration', '0', '--interval', '0.2', '--output', tmp, '--no-follow']
            with (Path(tmp) / 'console').open('w') as out:
                process = subprocess.Popen(args, stdout=out, stderr=out)
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        files = list(Path(tmp).glob('monitor-*/telemetry.jsonl'))
                        if files and '"sample"' in files[0].read_text():
                            break
                        time.sleep(0.1)
                    self.assertIsNone(process.poll())
                    self.assertTrue(files)
                    process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=20), 0)
                    self.assertTrue((files[0].parent / 'inventory-end.json').exists())
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(lab.report(files[0].parent), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()


class WireTests(unittest.TestCase):
    def test_session_peer_direction_and_payload_crc(self):
        src, dst, session = b'a' * 6, b'b' * 6, b's' * 16
        packet = wire.frame(src, dst, session, 0, 123, 1400)
        self.assertEqual(wire.decode(packet, src, dst, session, 0), 123)
        self.assertIsNone(wire.decode(packet, dst, src, session, 0))
        self.assertIsNone(wire.decode(packet, src, dst, session, 1))
        altered = packet[:100] + bytes([packet[100] ^ 1]) + packet[101:]
        self.assertEqual(wire.decode(altered, src, dst, session, 0), 'corrupt')

    def test_bounded_sequence_window(self):
        window = wire.SequenceWindow(8)
        self.assertEqual([window.accept(n) for n in (0, 2, 1, 1, 100, 0)],
                         ['new', 'new', 'reordered', 'duplicate', 'new', 'too_old'])
        self.assertLessEqual(window.bits.bit_length(), 8)

    def test_low_rate_capture_drop_loss_and_stop_are_distinct(self):
        row = dict(sent=1000, received_unique=1000, tx_errors=0, duplicate=0,
                   reordered=0, too_old=0, corrupt=0, error=None, capture_socket_drops=0)
        args = argparse.Namespace(payload_size=1400, mbps=10)
        elapsed = 1000 * 1438 * 8 / 1e6 / 10
        def verdict(data, seconds=elapsed, reason='duration_completed'):
            return wire.assess([dict(data)], seconds, args, 0, reason)['verdict']
        self.assertEqual(verdict(row), 'NO_ANOMALY_AT_THIS_RATE')
        self.assertEqual(verdict(dict(row, received_unique=999)), 'ANOMALY_OBSERVED')
        self.assertEqual(verdict(dict(row, capture_socket_drops=1)), 'INCONCLUSIVE_OR_STOPPED')
        self.assertEqual(verdict(row, elapsed * 2), 'INCONCLUSIVE_RATE_TOO_LOW')
        self.assertEqual(verdict(row, reason='user_stopped'), 'INCONCLUSIVE_OR_STOPPED')

    def test_loopback_rejected_as_physical_port(self):
        with self.assertRaisesRegex(ValueError, 'physical'):
            wire.preflight(['lo', lab.interfaces()[0]], 1400)

    def test_mock_transport_detects_loss_and_ignores_outgoing_copies(self):
        packets = queue.Queue()
        class PacketSocket:
            def bind(self, *a): pass
            def settimeout(self, *a): pass
            def setsockopt(self, *a): pass
            def close(self): pass
            def getsockopt(self, *a): return struct.pack('II', 0, 0)
            def send(self, packet):
                seq = wire.HEADER.unpack(packet[14:14 + wire.HEADER.size])[-1]
                packets.put((packet, ('b', wire.PROTOCOL, socket.PACKET_OUTGOING)))
                if seq != 1:
                    packets.put((packet, ('b', wire.PROTOCOL, socket.PACKET_HOST)))
                return len(packet)
            def recvfrom(self, *a):
                try: return packets.get(timeout=0.05)
                except queue.Empty: raise socket.timeout()
        args = argparse.Namespace(mbps=0.1, payload_size=256, duration=0.2)
        channel = wire.Direction('a', 'b', b'a' * 6, b'b' * 6, b's' * 16, 0, args)
        with patch.object(socket, 'socket', side_effect=lambda *a: PacketSocket()):
            try:
                channel.open()
                channel.send(time.monotonic(), threading.Event())
                time.sleep(0.1)
            finally:
                channel.close()
        stats = channel.snapshot()
        self.assertGreater(stats['sent'], 2)
        self.assertEqual(stats['sent'] - stats['received_unique'], 1)
        self.assertEqual(stats['duplicate'], 0)

    def test_real_raw_loopback_transport_when_capability_available(self):
        try:
            stream = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(wire.PROTOCOL))
        except PermissionError:
            self.skipTest('CAP_NET_RAW unavailable; physical ports are not validated here')
        stream.close()
        args = argparse.Namespace(mbps=0.1, payload_size=256, duration=0.2)
        channel = wire.Direction('lo', 'lo', b'a' * 6, b'b' * 6, b's' * 16, 0, args)
        try:
            channel.open()
            channel.send(time.monotonic(), threading.Event())
            time.sleep(0.1)
        finally:
            channel.close()
        stats = channel.snapshot()
        self.assertGreater(stats['sent'], 0)
        self.assertEqual(stats['sent'], stats['received_unique'])
        self.assertEqual(stats['capture_socket_drops'], 0)


if __name__ == '__main__':
    unittest.main()
