#!/usr/bin/env python3
"""One privileged job; watches GUI lifetime and stop request, no arbitrary commands."""
import argparse
import json
import os
from pathlib import Path
import signal
import stat
import sys
import threading

import gui_common
import sensor_lab as lab
import load_test
import wire_test


def run(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('invalid job file')
    uid = int(os.environ.get('PKEXEC_UID', str(os.getuid())))
    if path.stat().st_uid != uid:
        raise ValueError('job must belong to the requesting user')
    job = gui_common.validate_job(json.loads(path.read_text()))
    old_map = lab.NVME_SENSOR_MAP
    lab.NVME_SENSOR_MAP = job['roles']
    output = path.parent / 'results'
    events_path = path.parent / 'gui-events.jsonl'
    fd = os.open(str(events_path), os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0))
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
        os.close(fd)
        raise ValueError('invalid event file owner/type')
    stream = os.fdopen(fd, 'w', buffering=1)
    lock, ended, requested = threading.Lock(), threading.Event(), threading.Event()
    def emit(kind, **fields):
        with lock:
            stream.write(json.dumps(dict(kind=kind, time=lab.now(), **fields), ensure_ascii=False) + '\n')
            stream.flush()
    original = lab.Recorder
    class DesktopRecorder(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if os.geteuid() == 0:
                os.chown(self.directory, uid, -1)
            emit('directory', path=str(self.directory))
    lab.Recorder = DesktopRecorder
    def watch():
        tick = 0
        try:
            while not ended.wait(0.5):
                parent_alive = gui_common.process_start(job['parent_pid']) == job['parent_start']
                if (path.parent / 'stop').exists() or not parent_alive:
                    requested.set()
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                if tick % 4 == 0:
                    sensors = lab.temperatures()
                    emit('metrics', temperatures=sensors, summary=lab.temperature_summary(sensors))
                tick += 1
        except (OSError, ValueError) as exc:
            requested.set()
            emit('watch_error', message=str(exc))
            os.kill(os.getpid(), signal.SIGTERM)
    previous = signal.signal(signal.SIGTERM, lambda *_: requested.set())
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    result = 0
    records = []
    try:
        for label, mode, args in gui_common.stages(job, output):
            if requested.is_set() or (path.parent / 'stop').exists():
                result = 2
                break
            emit('stage', label=label, mode=mode)
            try:
                code = {'monitor': lab.main, 'load': load_test.main, 'wire': wire_test.main}[mode](args)
            except (OSError, ValueError) as exc:
                code = 2
                emit('error', message=str(exc))
            records.append({'stage': label, 'exit_code': code})
            emit('stage_end', label=label, exit_code=code)
            if code or requested.is_set():
                result = code or 2
                break
        for directory in sorted(output.glob('*')) if output.exists() else []:
            if directory.is_dir() and (directory / 'telemetry.jsonl').exists():
                try:
                    report_code = lab.report(directory)
                    emit('report', path=str(directory / 'report.html'))
                    result = result or report_code
                except (OSError, ValueError) as exc:
                    emit('error', message='report: ' + str(exc))
                    result = result or 2
        if os.geteuid() == 0 and output.exists():
            for directory, dirs, files in os.walk(output, followlinks=False):
                os.chown(directory, uid, -1, follow_symlinks=False)
                for name in files:
                    os.chown(Path(directory) / name, uid, -1, follow_symlinks=False)
        emit('done', exit_code=result, user_stopped=requested.is_set(), stages=records)
    finally:
        ended.set()
        thread.join(timeout=3)
        signal.signal(signal.SIGTERM, previous)
        lab.Recorder = original
        lab.NVME_SENSOR_MAP = old_map
        stream.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True)
    args = parser.parse_args(argv)
    return run(args.job)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(2)
