#!/usr/bin/env python3
"""Passive service supervisor with bounded session retention. Python 3.8+."""
import argparse
import datetime as dt
import fcntl
import json
import math
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

DEFAULTS = {
    "interfaces": [], "interval_seconds": 1, "log_mb": 10, "backups": 9,
    "nic_detail_interval_seconds": 60, "max_sessions": 5, "max_total_mb": 512,
}
MARKER = ".lan-diag-session"


def load_config(path):
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("unknown service config fields; active probes are not supported by this service")
    config = dict(DEFAULTS, **raw)
    interfaces = config["interfaces"]
    if (not isinstance(interfaces, list) or len(interfaces) > 100 or
            any(not isinstance(i, str) or i.startswith("-") or
                not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", i) for i in interfaces) or
            len(set(interfaces)) != len(interfaces)):
        raise ValueError("interfaces must be a list of unique Linux interface names; [] auto-detects physical Ethernet")
    for name, low, high in (("interval_seconds", 0.2, 3600),
                            ("nic_detail_interval_seconds", 0, 86400)):
        value = config[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("invalid " + name)
    for name, low, high in (("log_mb", 1, 1024), ("backups", 1, 99),
                            ("max_sessions", 1, 100), ("max_total_mb", 10, 102400)):
        value = config[name]
        if type(value) is not int or not low <= value <= high:
            raise ValueError("invalid " + name)
    # Reserve space for the active session's inventory files as well as its ring.
    if config["max_total_mb"] < config["log_mb"] * (config["backups"] + 1) + 5:
        raise ValueError("max_total_mb must allow one complete log ring plus 5 MiB")
    return config


def detect_interfaces(root=Path("/sys/class/net")):
    result = []
    for path in sorted(root.iterdir()):
        try:
            if (path / "device").exists() and (path / "type").read_text().strip() == "1":
                result.append(path.name)
        except OSError:
            continue
    return result


def tree_bytes(path):
    total = 0
    for directory, _, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except FileNotFoundError:
                pass
    return total


def prune_sessions(output, config, active=None):
    """Delete only our marked session directories, never symlink targets/current."""
    sessions = []
    for path in Path(output).glob("session-*"):
        marker = path / MARKER
        if path.is_symlink() or not path.is_dir() or marker.is_symlink() or not marker.is_file():
            continue
        try:
            data = json.loads(marker.read_text())
            if data.get("owner") != "lan-diag" or data.get("version") != 1:
                continue
        except (OSError, ValueError, AttributeError):
            continue
        sessions.append((path, tree_bytes(path), path.stat().st_mtime))
    sessions.sort(key=lambda row: row[2])
    total = sum(size for _, size, _ in sessions)
    limit = config["max_total_mb"] * 1024 * 1024
    removed = []
    while len(sessions) > config["max_sessions"] or total > limit:
        index = next((i for i, row in enumerate(sessions) if row[0] != active), None)
        if index is None:
            print("WARNING: active session exceeds retention budget; it is preserved", flush=True)
            break
        path, size, _ = sessions.pop(index)
        shutil.rmtree(path)
        total -= size
        removed.append(str(path))
        print("Retention removed old session:", path, flush=True)
    return removed


def build_command(config, interfaces, output, script=None):
    if not interfaces:
        raise ValueError("cannot start without interfaces")
    return [sys.executable, "-u", str(script or Path(__file__).resolve().with_name("lan_diag.py")),
            "monitor", "--interfaces", *interfaces, "--duration", "0", "--output", str(output),
            "--interval", str(config["interval_seconds"]), "--log-mb", str(config["log_mb"]),
            "--backups", str(config["backups"]),
            "--nic-detail-interval", str(config["nic_detail_interval_seconds"])]


def recent_sample(output, since=0, max_age=30, now=None):
    """Read at most 1 MiB from each newest telemetry file, not entire logs."""
    now = time.time() if now is None else now
    candidates = sorted(Path(output).glob("session-*/run-*/telemetry.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates[:5]:
        if path.is_symlink() or path.stat().st_mtime < max(since, now - max_age):
            continue
        with path.open("rb") as stream:
            offset = max(0, path.stat().st_size - 1024 * 1024)
            stream.seek(offset)
            if offset:
                stream.readline()
            lines = stream.read().splitlines()
        for line in reversed(lines):
            try:
                record = json.loads(line)
                timestamp = dt.datetime.fromisoformat(record["time"]).timestamp()
                if (record["kind"] == "sample" and timestamp >= since and
                        -1 <= now - timestamp <= max_age):
                    return str(path)
            except (ValueError, KeyError, TypeError):
                continue
    return None


def supervise(config, output, stop, script=None, sysfs=Path("/sys/class/net")):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another service runner already owns this log directory")
        while not stop.is_set():
            interfaces = config["interfaces"] or detect_interfaces(sysfs)
            if interfaces and all((sysfs / i).exists() for i in interfaces):
                break
            print("Waiting for physical/configured Ethernet interfaces:", interfaces or "auto", flush=True)
            stop.wait(5)
        if stop.is_set():
            return 0
        session = Path(tempfile.mkdtemp(prefix="session-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=str(output)))
        os.chmod(session, 0o700)
        (session / MARKER).write_text(json.dumps({"owner": "lan-diag", "version": 1}))
        prune_sessions(output, config, active=session)
        argv = build_command(config, interfaces, session, script)
        print("Starting passive capture:", session, "interfaces:", ", ".join(interfaces), flush=True)
        process = subprocess.Popen(argv)
        last_prune = time.monotonic()
        try:
            while process.poll() is None and not stop.wait(0.5):
                if time.monotonic() - last_prune >= 60:
                    prune_sessions(output, config, active=session)
                    last_prune = time.monotonic()
            if stop.is_set() and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=150)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            code = process.wait()
            print("Capture stopped; exit code:", code, flush=True)
            # An unexpected clean child exit must also restart under systemd.
            return 0 if stop.is_set() else code if code > 0 else 1
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/lan-diag/config.json")
    parser.add_argument("--output", default="/var/log/lan-diag")
    parser.add_argument("--check", action="store_true", help="validate config only")
    parser.add_argument("--health", action="store_true", help="check for a recent sample; also check systemctl is-active")
    parser.add_argument("--since", type=float, default=0, help="oldest acceptable sample, Unix time")
    args = parser.parse_args(argv)
    if args.health:
        path = recent_sample(args.output, args.since)
        if path:
            print("Recent sample:", path)
            return 0
        print("No recent sample found", file=sys.stderr)
        return 1
    config = load_config(args.config)
    if args.check:
        print("Service config valid (passive mode)")
        return 0
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return supervise(config, args.output, stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(2)
