#!/usr/bin/env python3
"""Sensor-free Ubuntu hardware evidence recorder. Python 3.8+, stdlib only."""
import argparse
from collections import Counter, deque
import concurrent.futures
import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

NET_COUNTERS = ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets', 'rx_errors',
                'tx_errors', 'rx_dropped', 'tx_dropped', 'rx_crc_errors',
                'rx_missed_errors', 'rx_fifo_errors', 'tx_carrier_errors')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def number(path):
    raw = read(path)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def command(args, timeout=4):
    if not shutil.which(args[0]):
        return {'status': 'unavailable', 'reason': args[0] + ' not installed'}
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors='replace',
                                timeout=timeout, env=dict(os.environ, LC_ALL='C'))
        return {'status': 'ok' if result.returncode == 0 else 'failed',
                'exit_code': result.returncode, 'stdout': result.stdout[:65536],
                'stderr': result.stderr[:8192]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'status': 'unavailable', 'reason': str(exc)}


def temperatures(root=Path('/sys/class/hwmon'), zones=Path('/sys/class/thermal')):
    result = []
    for hwmon in sorted(root.glob('hwmon*')):
        name = read(hwmon / 'name') or hwmon.name
        kind = 'cpu' if name in ('coretemp', 'k10temp', 'zenpower') else 'nvme' if name == 'nvme' else 'other'
        for path in sorted(hwmon.glob('temp*_input')):
            value = number(path)
            if value is None or not -20_000 <= value <= 150_000:
                continue
            prefix = path.name[:-6]
            critical = number(hwmon / (prefix + '_crit'))
            result.append({'id': str(path), 'name': name,
                           'label': read(hwmon / (prefix + '_label')) or prefix,
                           'kind': kind, 'celsius': value / 1000,
                           'critical_celsius': critical / 1000 if critical else None})
    for zone in sorted(zones.glob('thermal_zone*')):
        value, name = number(zone / 'temp'), read(zone / 'type') or zone.name
        if value is not None and -20_000 <= value <= 150_000:
            result.append({'id': str(zone / 'temp'), 'name': name, 'label': name,
                           'kind': 'cpu' if name in ('x86_pkg_temp', 'cpu_thermal', 'soc_thermal') else 'other',
                           'celsius': value / 1000, 'critical_celsius': None})
    return result


def thermal_status(sensors, cpu_limit=85, nvme_limit=70):
    missing = [kind for kind in ('cpu', 'nvme') if not any(s['kind'] == kind for s in sensors)]
    hot = []
    for sensor in sensors:
        if sensor['kind'] not in ('cpu', 'nvme'):
            continue
        limit = cpu_limit if sensor['kind'] == 'cpu' else nvme_limit
        if sensor.get('critical_celsius') is not None:
            limit = min(limit, sensor['critical_celsius'] - 5)
        if sensor['celsius'] >= limit:
            hot.append(dict(sensor, stop_limit_celsius=limit))
    return {'safe_to_load': not missing and not hot, 'missing': missing, 'hot': hot}


def nic(interface, root=Path('/sys/class/net')):
    base = root / interface
    if not base.exists():
        return {'present': False}
    value = {'present': True, 'operstate': read(base / 'operstate'),
             'duplex': read(base / 'duplex'), 'mac': read(base / 'address')}
    for name in ('carrier', 'carrier_changes', 'ifindex', 'mtu', 'speed'):
        value[name] = number(base / name)
    value['counters'] = {key: n for key in NET_COUNTERS
                         for n in [number(base / 'statistics' / key)] if n is not None}
    value['unreadable_counters'] = [k for k in NET_COUNTERS if k not in value['counters']]
    return value


def delta(current, previous):
    values, reset = {}, []
    for key, new in current.items():
        old = previous.get(key)
        if old is not None:
            if new >= old:
                values[key] = new - old
            else:
                reset.append(key)
    return values, reset


def nic_events(interface, current, previous, seconds):
    if previous is None:
        return []
    events = []
    for field in ('present', 'ifindex', 'carrier', 'speed', 'duplex', 'operstate'):
        if current.get(field) != previous.get(field):
            events.append({'event': 'nic_change', 'interface': interface, 'field': field,
                           'before': previous.get(field), 'after': current.get(field)})
    if not current['present'] or not previous['present'] or current.get('ifindex') != previous.get('ifindex'):
        return events
    diff, resets = delta(current['counters'], previous['counters'])
    current['delta'] = diff
    current['mbps'] = {direction: diff[direction + '_bytes'] * 8 / seconds / 1e6
                       for direction in ('rx', 'tx') if seconds > 0 and direction + '_bytes' in diff}
    errors = {key: value for key, value in diff.items() if value and key not in
              ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets')}
    if errors:
        events.append({'event': 'nic_errors', 'interface': interface, 'delta': errors})
    if resets:
        events.append({'event': 'counter_reset', 'interface': interface, 'keys': resets})
    changes, old = current.get('carrier_changes'), previous.get('carrier_changes')
    if changes is not None and old is not None and changes != old:
        events.append({'event': 'link_flaps', 'interface': interface, 'before': old, 'after': changes})
    return events


def metrics(interfaces):
    memory = {}
    for line in (read('/proc/meminfo') or '').splitlines():
        key, _, rest = line.partition(':')
        if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
            memory[key] = int(rest.split()[0]) * 1024
    cpu = {}
    for line in (read('/proc/stat') or '').splitlines():
        fields = line.split()
        if fields and re.fullmatch(r'cpu\d*', fields[0]):
            ticks = [int(v) for v in fields[1:9]]
            cpu[fields[0]] = {'total': sum(ticks), 'idle': sum(ticks[3:5])}
    throttle = {}
    for path in Path('/sys/devices/system/cpu').glob('cpu[0-9]*/thermal_throttle/*_throttle_count'):
        value = number(path)
        if value is not None:
            throttle[str(path)] = value
    aer = {}
    for path in Path('/sys/bus/pci/devices').glob('*/aer_*'):
        text = read(path)
        if text:
            for line in text.splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1].isdecimal():
                    aer[str(path) + '/' + fields[0]] = int(fields[1])
    disks = {}
    for line in (read('/proc/diskstats') or '').splitlines():
        fields = line.split()
        if len(fields) >= 14 and re.fullmatch(r'nvme\d+n\d+', fields[2]):
            values = [int(v) for v in fields[3:14]]
            disks[fields[2]] = dict(zip(('reads', 'reads_merged', 'sectors_read', 'read_ms', 'writes',
                                       'writes_merged', 'sectors_written', 'write_ms', 'inflight', 'io_ms', 'weighted_io_ms'), values))
    softnet = {'dropped': 0, 'time_squeeze': 0}
    for line in (read('/proc/net/softnet_stat') or '').splitlines():
        fields = line.split()
        if len(fields) >= 3:
            softnet['dropped'] += int(fields[1], 16)
            softnet['time_squeeze'] += int(fields[2], 16)
    return {'nics': {i: nic(i) for i in interfaces}, 'temperatures': temperatures(),
            'memory': memory, 'cpu_ticks': cpu, 'throttle': throttle, 'aer': aer,
            'disks': disks, 'softnet': softnet, 'load_average': os.getloadavg(),
            'pressure': {kind: read('/proc/pressure/' + kind) for kind in ('cpu', 'memory', 'io')}}


class Log:
    def __init__(self, path, max_bytes=10 * 1024 * 1024, backups=19):
        self.path, self.max_bytes, self.backups = Path(path), max_bytes, backups
        self.lock = threading.Lock()

    def write(self, kind, **fields):
        data = (json.dumps(dict(time=now(), monotonic=time.monotonic(), kind=kind, **fields),
                           ensure_ascii=False) + '\n').encode()
        with self.lock:
            if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
                for index in range(self.backups, 0, -1):
                    path = Path(str(self.path) + '.' + str(index))
                    if index == self.backups:
                        path.unlink(missing_ok=True)
                    else:
                        source = Path(str(self.path) + '.' + str(index))
                        if source.exists():
                            source.replace(str(self.path) + '.' + str(index + 1))
                self.path.replace(str(self.path) + '.1')
            with self.path.open('ab') as stream:
                stream.write(data)


def interfaces(names=None):
    result = names or [p.name for p in sorted(Path('/sys/class/net').iterdir())
                       if (p / 'device').exists() and read(p / 'type') == '1']
    if not result:
        result = [p.name for p in sorted(Path('/sys/class/net').iterdir()) if p.name != 'lo']
    for name in result:
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', name) or name.startswith('-'):
            raise ValueError('invalid interface name: ' + name)
        if not Path('/sys/class/net', name).exists():
            raise ValueError('interface is missing: ' + name)
    return list(dict.fromkeys(result))


def inventory(names):
    args = [['lscpu'], ['lsblk', '-J', '-o', 'NAME,MODEL,SIZE,TYPE,MOUNTPOINT'],
            ['lspci', '-nnk'], ['lsusb', '-t'], ['ip', '-j', 'address'],
            ['ip', '-j', 'route', 'show', 'table', 'all'],
            ['journalctl', '-k', '-n', '200', '--no-pager', '-o', 'short-iso']]
    for name in names:
        args += [['ethtool', name], ['ethtool', '-i', name], ['ethtool', '-S', name],
                 ['ethtool', '--show-eee', name]]
    for path in Path('/sys/class/nvme').glob('nvme[0-9]*'):
        if re.fullmatch(r'nvme\d+', path.name):
            args.append(['nvme', 'smart-log', '/dev/' + path.name, '-o', 'json'])
    return {'time': now(), 'os_release': read('/etc/os-release'), 'platform': list(os.uname()),
            'snapshot': metrics(names), 'commands': {' '.join(a): command(a) for a in args},
            'nic_devices': {n: str((Path('/sys/class/net') / n / 'device').resolve()) for n in names}}


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def smart_observation(result, previous=None):
    if result.get('status') != 'ok':
        return None, []
    try:
        raw = json.loads(result['stdout'])
        state = {}
        for key in ('critical_warning', 'media_errors', 'num_err_log_entries'):
            value = raw.get(key)
            if value is not None:
                text = str(value).replace(',', '').strip()
                state[key] = int(text, 16) if text.lower().startswith('0x') else int(text)
    except (ValueError, TypeError, AttributeError, KeyError):
        return None, []
    events = []
    if state.get('critical_warning', 0) and state.get('critical_warning') != (previous or {}).get('critical_warning'):
        events.append({'event': 'nvme_warning', 'value': state['critical_warning'], 'baseline': previous is None})
    if previous:
        for key in ('media_errors', 'num_err_log_entries'):
            if key in state and key in previous and state[key] > previous[key]:
                events.append({'event': 'nvme_error_increase', 'counter': key, 'delta': state[key] - previous[key]})
    return state, events


class Recorder:
    def __init__(self, names, output, label='monitor', follow=True):
        self.names = interfaces(names)
        Path(output).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix=label + '-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ-'), dir=str(Path(output).resolve())))
        os.chmod(self.directory, 0o700)
        self.log = Log(self.directory / 'telemetry.jsonl')
        self.previous, self.previous_time = None, None
        self.follow, self.followers, self.threads = follow, [], []
        self.background_error = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.jobs, self.next_detail = {}, 0
        self.smart, self.pending_events = {}, []

    def open(self):
        print('Results:', self.directory, flush=True)
        initial = inventory(self.names)
        write_json(self.directory / 'inventory-start.json', initial)
        for key, result in initial['commands'].items():
            self._smart(key, result)
        self.log.write('start', interfaces=self.names)
        if self.follow:
            for argv in (['journalctl', '-k', '-f', '-n', '0', '--no-pager', '-o', 'short-iso'],
                         ['ip', '-ts', 'monitor', 'link', 'address', 'route']):
                if not shutil.which(argv[0]):
                    self.log.write('unavailable', source=argv[0])
                    continue
                process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, errors='replace', env=dict(os.environ, LC_ALL='C'))
                self.followers.append(process)
                thread = threading.Thread(target=self._follow, args=(process, argv[0]), daemon=True)
                self.threads.append(thread)
                thread.start()

    def _smart(self, key, result):
        if key.startswith('nvme smart-log '):
            state, events = smart_observation(result, self.smart.get(key))
            if state is not None:
                self.smart[key] = state
            self.pending_events += [dict(e, source=key) for e in events]

    def _follow(self, process, source):
        try:
            while True:
                line = process.stdout.readline(8192)
                if not line:
                    break
                self.log.write('system_log', source=source, message=line.rstrip())
            self.log.write('collector_exit', source=source, exit_code=process.wait())
        except OSError as exc:
            self.background_error = str(exc)
        finally:
            process.stdout.close()

    def sample(self):
        if self.background_error:
            raise OSError('background log write failed: ' + self.background_error)
        current, stamp = metrics(self.names), time.monotonic()
        events, self.pending_events = self.pending_events, []
        current['nvme_warnings'] = {key: state['critical_warning'] for key, state in self.smart.items()
                                    if state.get('critical_warning')}
        if self.previous is not None:
            seconds = stamp - self.previous_time
            current['interval_seconds'] = seconds
            for name in self.names:
                events += nic_events(name, current['nics'][name], self.previous['nics'][name], seconds)
            for key in ('throttle', 'aer', 'softnet'):
                changes, resets = delta(current[key], self.previous[key])
                changes = {k: v for k, v in changes.items() if v}
                if changes or resets:
                    events.append({'event': key + '_change', 'delta': changes, 'reset': resets})
            busy = {}
            for core, values in current['cpu_ticks'].items():
                change, _ = delta(values, self.previous['cpu_ticks'].get(core, {}))
                if change.get('total', 0) and 'idle' in change and 0 <= change['idle'] <= change['total']:
                    busy[core] = round(100 * (1 - change['idle'] / change['total']), 2)
            current['cpu_busy_percent'] = busy
            current['disk_delta'] = {name: delta({k: v for k, v in values.items() if k != 'inflight'}, self.previous['disks'].get(name, {}))[0]
                                     for name, values in current['disks'].items()}
            current['disk_rates'] = {}
            for name, diff in current['disk_delta'].items():
                current['disk_rates'][name] = {
                    'read_mibps': diff.get('sectors_read', 0) * 512 / seconds / 1024 ** 2,
                    'write_mibps': diff.get('sectors_written', 0) * 512 / seconds / 1024 ** 2,
                    'read_await_ms': diff.get('read_ms', 0) / diff['reads'] if diff.get('reads') else None,
                    'write_await_ms': diff.get('write_ms', 0) / diff['writes'] if diff.get('writes') else None,
                    'io_busy_percent': diff.get('io_ms', 0) / seconds / 10}
        for event in events:
            self.log.write('event', **event)
        self.log.write('sample', data=current)
        self.previous, self.previous_time = current, stamp
        for key, future in list(self.jobs.items()):
            if future.done():
                result = future.result()
                self.log.write('detail', command=key, result=result)
                self._smart(key, result)
                del self.jobs[key]
        if stamp >= self.next_detail and not self.jobs:
            args = [['ethtool', '-S', name] for name in self.names]
            args += [['nvme', 'smart-log', '/dev/' + p.name, '-o', 'json']
                     for p in Path('/sys/class/nvme').glob('nvme*') if re.fullmatch(r'nvme\d+', p.name)]
            self.jobs = {' '.join(a): self.pool.submit(command, a) for a in args}
            self.next_detail = stamp + 30
        return current, events

    def close(self):
        for process in self.followers:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for thread in self.threads:
            thread.join(timeout=3)
        self.pool.shutdown(wait=True)
        for key, future in self.jobs.items():
            self.log.write('detail', command=key, result=future.result())
        self.log.write('stop')
        write_json(self.directory / 'inventory-end.json', inventory(self.names))


def report(directory):
    directory = Path(directory)
    files = list(directory.glob('telemetry.jsonl*'))
    if not files:
        raise ValueError('no telemetry files in selected run')
    def order(path):
        suffix = path.name[len('telemetry.jsonl'):]
        return int(suffix[1:]) if suffix.startswith('.') else 0
    counts, events, points, malformed, samples = Counter(), deque(maxlen=1000), [], 0, 0
    kernel_clues = deque(maxlen=100)
    clue_pattern = re.compile(r'nvme.*(timeout|reset|abort|error)|NETDEV WATCHDOG|AER:|PCIe Bus Error|thermal.*(critical|thrott)|Out of memory|oom-kill|link.*(down|lost)|USB disconnect|reset.*USB', re.I)
    disk_await_peak, min_memory, top_core_peak = None, None, None
    nic_totals = {}
    peaks = {'cpu': None, 'nvme': None}
    first, last = None, None
    stride = 1
    for path in sorted(files, key=order, reverse=True):
        with path.open() as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    first = first or record['time']
                    last = record['time']
                    if record['kind'] == 'event':
                        counts[record['event']] += 1
                        events.append(record)
                    if record['kind'] == 'system_log' and clue_pattern.search(record.get('message', '')):
                        kernel_clues.append(record)
                    if record['kind'] == 'sample':
                        data = record['data']
                        temps = {kind: max((s['celsius'] for s in data['temperatures'] if s['kind'] == kind), default=None)
                                 for kind in peaks}
                        for kind, value in temps.items():
                            if value is not None:
                                peaks[kind] = max(peaks[kind], value) if peaks[kind] is not None else value
                        for disk in data.get('disk_rates', {}).values():
                            for key in ('read_await_ms', 'write_await_ms'):
                                value = disk.get(key)
                                if value is not None:
                                    disk_await_peak = max(disk_await_peak, value) if disk_await_peak is not None else value
                        available = data.get('memory', {}).get('MemAvailable')
                        if available is not None:
                            min_memory = min(min_memory, available) if min_memory is not None else available
                        for core, value in data.get('cpu_busy_percent', {}).items():
                            if core != 'cpu':
                                top_core_peak = max(top_core_peak, value) if top_core_peak is not None else value
                        for name, nic_data in data.get('nics', {}).items():
                            counters = nic_totals.setdefault(name, Counter())
                            counters.update({k: v for k, v in nic_data.get('delta', {}).items()
                                             if k not in ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets')})
                        if samples % stride == 0:
                            points.append({'time': record['time'], 'monotonic': record['monotonic'], **temps,
                                           'cpu_busy': data.get('cpu_busy_percent', {}).get('cpu')})
                        samples += 1
                        if len(points) > 2000:
                            points = points[::2]
                            stride *= 2
                except (ValueError, KeyError, TypeError, AttributeError):
                    malformed += 1
    summary = {'samples_retained': samples, 'malformed_lines': malformed, 'first_retained_time': first,
               'last_retained_time': last, 'peak_celsius': peaks, 'event_counts': dict(counts),
               'latest_events': list(events), 'temperature_points': points,
               'peak_disk_interval_average_await_ms': disk_await_peak,
               'min_available_memory_mib': min_memory / 1024 ** 2 if min_memory is not None else None,
               'peak_single_core_busy_percent': top_core_peak,
               'nic_counter_increments': {k: dict(v) for k, v in nic_totals.items()},
               'kernel_keyword_matches': list(kernel_clues),
               'scope': 'Retained logs only. Correlation is not proof of cause; sensor/SDK/game behavior is not tested.'}
    write_json(directory / 'report.json', summary)
    rows = ''.join('<tr><td>' + html.escape(e['time']) + '</td><td><pre>' + html.escape(json.dumps(e, ensure_ascii=False)) + '</pre></td></tr>' for e in events)
    chart = []
    start_mono = points[0]['monotonic'] if points else 0
    span = max(0.001, points[-1]['monotonic'] - start_mono) if points else 1
    for kind, color in (('cpu', '#dc2626'), ('nvme', '#2563eb')):
        segments, current = [], []
        for index, point in enumerate(points):
            if point[kind] is None:
                if current:
                    segments.append(current)
                    current = []
                continue
            current.append('%0.1f,%0.1f' % (40 + (point['monotonic'] - start_mono) * 900 / span, 260 - point[kind] * 2))
        if current:
            segments.append(current)
        chart += ['<polyline fill="none" stroke="' + color + '" points="' + ' '.join(segment) + '"/>' for segment in segments]
    for event in events:
        if event['event'] in ('nic_change', 'link_flaps', 'nvme_warning', 'nvme_error_increase', 'aer_change'):
            x = 40 + (event.get('monotonic', start_mono) - start_mono) * 900 / span
            if 40 <= x <= 940:
                chart.append('<line x1="%0.1f" x2="%0.1f" y1="20" y2="260" stroke="#888" stroke-dasharray="3"/>' % (x, x))
    body = ('<!doctype html><html lang="ko"><meta charset="utf-8"><title>센서 장비 진단</title>'
            '<style>body{font:16px sans-serif;max-width:1100px;margin:30px auto;padding:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere}td{vertical-align:top;border-bottom:1px solid #ddd;padding:8px}svg{width:100%;background:#fafafa}</style>'
            '<h1>무센서 장비 진단 기록</h1><p>관측 시각은 UTC. CPU 빨강 / NVMe 파랑. 회색 세로선: NIC·NVMe·PCIe 이벤트. 선이 없는 구간은 온도 수집 불가.</p>'
            '<p>온도와 오류가 함께 나타나도 원인 확정은 아닙니다. 보존된 구간만 분석하며 실제 카메라·게임 정상 여부를 판정하지 않습니다.</p>'
            '<pre>' + html.escape(json.dumps({k: v for k, v in summary.items() if k not in ('latest_events', 'temperature_points', 'kernel_keyword_matches')}, ensure_ascii=False, indent=2)) + '</pre>'
            '<svg viewBox="0 0 1000 300" role="img" aria-label="CPU와 NVMe 온도 추이">'
            '<text x="0" y="25">120°C</text><text x="0" y="265">0°C</text>' + ''.join(chart) + '</svg>'
            '<p>왼쪽: ' + html.escape(first or '') + ' / 오른쪽: ' + html.escape(last or '') + '</p>'
            '<h2>커널 키워드 일치 (최대 100개, 원인 판정 아님)</h2><pre>' + html.escape(json.dumps(list(kernel_clues), ensure_ascii=False, indent=2)) + '</pre>'
            '<h2>최근 이벤트 (최대 1000개)</h2><table>' + rows + '</table></html>')
    (directory / 'report.html').write_text(body)
    print('Report:', directory / 'report.html')
    return 0 if samples and not malformed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='action', required=True)
    subs.add_parser('inventory')
    monitor = subs.add_parser('monitor')
    monitor.add_argument('--interfaces', nargs='+')
    monitor.add_argument('--duration', type=float, default=1800)
    monitor.add_argument('--interval', type=float, default=2)
    monitor.add_argument('--output', default='./results')
    monitor.add_argument('--no-follow', action='store_true')
    summary = subs.add_parser('report')
    summary.add_argument('directory')
    args = parser.parse_args(argv)
    if args.action == 'inventory':
        print(json.dumps(inventory(interfaces()), ensure_ascii=False, indent=2))
        return 0
    if args.action == 'report':
        return report(args.directory)
    if not 0 <= args.duration <= 604800 or not 0.2 <= args.interval <= 3600:
        parser.error('duration 0..604800 seconds (0: until signal); interval 0.2..3600')
    stop = threading.Event()
    old = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    recorder = Recorder(args.interfaces, args.output, follow=not args.no_follow)
    try:
        recorder.open()
        start = time.monotonic()
        while not stop.is_set() and (not args.duration or time.monotonic() - start < args.duration):
            recorder.sample()
            wait = args.interval if not args.duration else min(args.interval, max(0, args.duration - (time.monotonic() - start)))
            stop.wait(wait)
    finally:
        recorder.close()
        for sig, handler in old.items():
            signal.signal(sig, handler)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(2)
