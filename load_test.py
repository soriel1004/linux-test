#!/usr/bin/env python3
"""Opt-in CPU/RAM/file-I/O load, stopped by CPU/NVMe temperature guards."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

import sensor_lab as lab


def guard(snapshot, cpu_limit, nvme_limit):
    thermal = lab.thermal_status(snapshot['temperatures'], cpu_limit, nvme_limit)
    if not thermal['safe_to_load']:
        return {'reason': 'temperature_limit_or_missing_sensor', 'thermal': thermal}
    if snapshot.get('nvme_warnings'):
        return {'reason': 'nvme_critical_warning', 'warnings': snapshot['nvme_warnings']}
    available = snapshot['memory'].get('MemAvailable')
    if available is None or available < 256 * 1024 * 1024:
        return {'reason': 'low_or_unknown_available_memory', 'available_bytes': available}
    return None


def disk_destination(directory):
    mount = lab.command(['findmnt', '-J', '-T', str(directory), '-o', 'SOURCE,FSTYPE,TARGET'])
    if mount.get('status') != 'ok':
        raise ValueError('findmnt is required to identify the disk test destination')
    try:
        filesystem = json.loads(mount['stdout'])['filesystems'][0]
        source = filesystem['source'].split('[')[0]
        if filesystem['fstype'] in ('tmpfs', 'ramfs', 'overlay') or not source.startswith('/dev/'):
            raise ValueError('disk test must use a real NVMe filesystem, not RAM/overlay/network storage')
        block = lab.command(['lsblk', '-s', '-J', '-o', 'NAME,TYPE', source])
        if block.get('status') != 'ok':
            raise ValueError('cannot identify the backing NVMe device')
        def has_nvme(items):
            return any(item.get('name', '').startswith('nvme') or has_nvme(item.get('children', [])) for item in items)
        if not has_nvme(json.loads(block['stdout'])['blockdevices']):
            raise ValueError('selected filesystem is not backed by a visible NVMe device')
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError('cannot verify disk destination: ' + str(exc))
    return {'mount': mount, 'block_devices': block}


def disk_worker(path, size_mb, rate_mib, stop):
    """Overwrite only the one exclusively created regular test file."""
    flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(path), flags)
    total = 0
    try:
        info = os.fstat(fd)
        if not Path(path).name.startswith('.sensor-lab-') or not stat.S_ISREG(info.st_mode) or info.st_size:
            raise ValueError('disk worker requires a new empty .sensor-lab-* regular test file')
        block = os.urandom(1024 * 1024)
        capacity = size_mb * len(block)
        start = time.monotonic()
        while not stop.is_set():
            if total and total % capacity == 0:
                os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(block)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError('zero-byte file write')
                view = view[written:]
            total += len(block)
            if total % (16 * len(block)) == 0:
                os.fsync(fd)
                if hasattr(os, 'posix_fadvise'):
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            stop.wait(max(0, total / (rate_mib * 1024 * 1024) - (time.monotonic() - start)))
        os.fsync(fd)
    finally:
        os.close(fd)
    return total


def worker_commands(args, disk_file=None):
    commands = []
    if args.cpu:
        commands.append(['stress-ng', '--cpu', str(args.workers), '--cpu-load', str(args.cpu_load),
                         '--verify', '--timeout', str(args.duration + 10) + 's', '--metrics-brief'])
    if args.memory_mb:
        commands.append(['stress-ng', '--vm', '1', '--vm-bytes', str(args.memory_mb) + 'M',
                         '--vm-keep', '--verify', '--timeout', str(args.duration + 10) + 's', '--metrics-brief'])
    if disk_file:
        commands.append([sys.executable, '-u', str(Path(__file__).resolve()), '_disk',
                         '--file', str(disk_file), '--size-mb', str(args.disk_size_mb),
                         '--rate-mib', str(args.disk_mib)])
    return commands


def stop_workers(processes):
    for process in processes:
        # The session may still contain stress-ng children after its leader exits.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                print('WARNING: worker still blocked in kernel I/O; PID:', process.pid, flush=True)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def execute(args):
    names = lab.interfaces(args.interfaces)
    initial = lab.metrics(names)
    blocked = guard(initial, args.cpu_limit, args.nvme_limit)
    if blocked:
        raise ValueError('load not started: ' + json.dumps(blocked, ensure_ascii=False))
    if (args.cpu or args.memory_mb) and not shutil.which('stress-ng'):
        raise ValueError('stress-ng missing; run setup.sh')
    if args.memory_mb > initial['memory']['MemAvailable'] / (4 * 1024 * 1024):
        raise ValueError('memory test exceeds 25% of currently available RAM; lower --memory-mb')
    disk_file = None
    disk_info = None
    if args.disk_dir:
        directory = Path(args.disk_dir).resolve(strict=True)
        if not directory.is_dir():
            raise ValueError('--disk-dir must be an existing directory, never a device')
        disk_info = disk_destination(directory)
        required = max(2 * 1024 ** 3, args.disk_size_mb * 2 * 1024 ** 2)
        if shutil.disk_usage(directory).free < required:
            raise ValueError('not enough free disk space; at least 2 GiB and twice file size are required')
    stop = threading.Event()
    handlers = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    recorder = lab.Recorder(names, args.output, 'load')
    processes, reason, elapsed = [], {'reason': 'setup_incomplete'}, 0
    out = None
    try:
        recorder.open()
        lab.write_json(recorder.directory / 'test-settings.json', vars(args))
        before, _ = recorder.sample()
        blocked = guard(before, args.cpu_limit, args.nvme_limit)
        if blocked:
            reason = blocked
        elif stop.is_set():
            reason = {'reason': 'user_stopped_before_load'}
        else:
            if args.disk_dir:
                fd, path = tempfile.mkstemp(prefix='.sensor-lab-', dir=str(directory))
                os.close(fd)
                disk_file = Path(path)
                recorder.log.write('disk_test_file', path=str(disk_file),
                                   destination=disk_info)
            commands = worker_commands(args, disk_file)
            out = (recorder.directory / 'workloads.log').open('w')
            for argv in commands:
                recorder.log.write('workload_start', command=argv)
                processes.append(subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, start_new_session=True))
            started = time.monotonic()
            reason = {'reason': 'duration_completed'}
            while not stop.is_set():
                elapsed = time.monotonic() - started
                snapshot, events = recorder.sample()
                blocked = guard(snapshot, args.cpu_limit, args.nvme_limit)
                if blocked:
                    reason = blocked
                    break
                if any((e['event'] == 'aer_change' and e['delta']) or e['event'] == 'nvme_error_increase' for e in events):
                    reason = {'reason': 'pcie_or_nvme_error_counter_changed', 'events': events}
                    break
                if any(p.poll() is not None for p in processes):
                    reason = {'reason': 'workload_exited_early', 'exit_codes': [p.poll() for p in processes]}
                    break
                if disk_file and shutil.disk_usage(disk_file.parent).free < 1024 ** 3:
                    reason = {'reason': 'disk_free_below_1GiB'}
                    break
                if elapsed >= args.duration:
                    break
                stop.wait(min(1, args.duration - elapsed))
            if stop.is_set():
                reason = {'reason': 'user_stopped'}
            elapsed = time.monotonic() - started
    finally:
        stop_workers(processes)
        if out:
            out.close()
        if disk_file and all(p.poll() is not None for p in processes):
            disk_file.unlink(missing_ok=True)
        summary = {'result': 'COMPLETED_OBSERVATION' if reason['reason'] == 'duration_completed' else 'STOPPED_OR_INCONCLUSIVE',
                   'elapsed_seconds': elapsed, 'stop': reason,
                   'worker_exit_codes_after_stop': [p.poll() for p in processes],
                   'leftover_disk_file': str(disk_file) if disk_file and disk_file.exists() else None,
                   'scope': 'No sensors, SDK or game tested. Temperature stop is a test limit, not a product-failure verdict.'}
        recorder.log.write('event', event='load_result', **summary)
        lab.write_json(recorder.directory / 'load-result.json', summary)
        recorder.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['result'] == 'COMPLETED_OBSERVATION' else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == '_disk':
        worker = argparse.ArgumentParser()
        worker.add_argument('--file', required=True)
        worker.add_argument('--size-mb', type=int, required=True)
        worker.add_argument('--rate-mib', type=float, required=True)
        args = worker.parse_args(argv[1:])
        if not 1 <= args.size_mb <= 1024 or not 0.1 <= args.rate_mib <= 100:
            worker.error('invalid disk worker limits')
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        print(json.dumps({'bytes_written': disk_worker(args.file, args.size_mb, args.rate_mib, stop)}))
        return 0
    parser.add_argument('--cpu', action='store_true')
    parser.add_argument('--workers', type=int, default=min(2, os.cpu_count() or 1))
    parser.add_argument('--cpu-load', type=int, default=60)
    parser.add_argument('--memory-mb', type=int, default=0)
    parser.add_argument('--disk-dir', help='opt-in file write test on this existing directory; no raw device writes')
    parser.add_argument('--disk-size-mb', type=int, default=256)
    parser.add_argument('--disk-mib', type=float, default=8, help='write MiB/s; default 8')
    parser.add_argument('--duration', type=int, default=600)
    parser.add_argument('--cpu-limit', type=float, default=85)
    parser.add_argument('--nvme-limit', type=float, default=70)
    parser.add_argument('--interfaces', nargs='+')
    parser.add_argument('--output', default='./results')
    args = parser.parse_args(argv)
    if not (args.cpu or args.memory_mb or args.disk_dir):
        parser.error('select --cpu, --memory-mb and/or --disk-dir')
    if (not 10 <= args.duration <= 3600 or not 1 <= args.workers <= min(32, os.cpu_count() or 1) or
            not 10 <= args.cpu_load <= 90 or not 0 <= args.memory_mb <= 2048 or
            not 64 <= args.disk_size_mb <= 1024 or not 0.1 <= args.disk_mib <= 100 or
            not 50 <= args.cpu_limit <= 95 or not 40 <= args.nvme_limit <= 80):
        parser.error('invalid load/duration/temperature limits; see --help')
    return execute(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(2)
