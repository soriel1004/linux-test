#!/usr/bin/env python3
"""Read-only Linux NIC diagnostics; active probes require explicit opt-in.

Python 3.8+, standard library only. No camera SDK or packet payload capture.
"""
import argparse
from collections import Counter, deque
import concurrent.futures
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

COUNTERS = (
    "rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors",
    "rx_dropped", "tx_dropped", "rx_crc_errors", "rx_frame_errors",
    "rx_fifo_errors", "rx_missed_errors", "rx_over_errors", "tx_carrier_errors",
    "collisions",
)
LINK_COUNTERS = ("carrier_changes", "carrier_up_count", "carrier_down_count")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class JsonLog:
    """Bounded JSONL log. I/O failures propagate rather than silently losing data."""
    def __init__(self, path, max_bytes=10 * 1024 * 1024, backups=9):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.lock = threading.Lock()

    def write(self, kind, **fields):
        record = dict(time=utc_now(), monotonic_seconds=time.monotonic(), kind=kind)
        record.update(fields)
        payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            size = self.path.stat().st_size if self.path.exists() else 0
            if size and size + len(payload) > self.max_bytes:
                oldest = Path(str(self.path) + "." + str(self.backups))
                if oldest.exists():
                    oldest.unlink()
                for n in range(self.backups - 1, 0, -1):
                    src = Path(str(self.path) + "." + str(n))
                    if src.exists():
                        src.replace(str(self.path) + "." + str(n + 1))
                self.path.replace(str(self.path) + ".1")
            with self.path.open("ab") as stream:
                stream.write(payload)


def command(argv, timeout=5):
    if shutil.which(argv[0]) is None:
        return {"available": False, "error": "command not installed: " + argv[0]}
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                env=dict(os.environ, LC_ALL="C"), check=False)
        return {"available": True, "exit_code": result.returncode,
                "stdout": result.stdout[:65536], "stderr": result.stderr[:8192]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": True, "error": str(exc)}


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def read_nic(interface, root=Path("/sys/class/net")):
    base = root / interface
    if not base.exists():
        return {"present": False}
    result = {"present": True}
    for name in ("operstate", "carrier", "mtu", "speed", "duplex", "address",
                 "ifindex") + LINK_COUNTERS:
        value = read_text(base / name)
        if value is not None:
            result[name] = int(value) if re.fullmatch(r"-?\d+", value) else value
    result["counters"] = {}
    for name in COUNTERS:
        value = read_text(base / "statistics" / name)
        if value is not None and value.isdecimal():
            result["counters"][name] = int(value)
    result["unreadable_counters"] = [k for k in COUNTERS if k not in result["counters"]]
    return result


def counter_delta(current, previous):
    delta, reset = {}, []
    for key, value in current.items():
        old = previous.get(key)
        if old is None:
            continue
        if value < old:
            reset.append(key)
        else:
            delta[key] = value - old
    return delta, reset


def nic_changes(interface, current, previous, elapsed):
    events = []
    if previous is None:
        return events
    for key in ("present", "ifindex", "carrier", "operstate", "speed", "duplex", "mtu"):
        if current.get(key) != previous.get(key):
            events.append({"event": "nic_change", "interface": interface,
                           "field": key, "before": previous.get(key),
                           "after": current.get(key)})
    # A replacement interface must establish a new counter baseline.
    if (not current.get("present") or not previous.get("present") or
            current.get("ifindex") != previous.get("ifindex")):
        return events
    delta, reset = counter_delta(current.get("counters", {}), previous.get("counters", {}))
    current["delta"] = delta
    if elapsed > 0:
        for direction in ("rx", "tx"):
            if direction + "_bytes" in delta:
                current[direction + "_mbps"] = delta[direction + "_bytes"] * 8 / elapsed / 1e6
    if reset:
        events.append({"event": "counter_reset", "interface": interface, "counters": reset})
    errors = {k: v for k, v in delta.items() if v and k not in
              ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets")}
    if errors:
        events.append({"event": "nic_counter_increase", "interface": interface,
                       "delta": errors})
    link_now = {k: current[k] for k in LINK_COUNTERS if k in current}
    link_old = {k: previous[k] for k in LINK_COUNTERS if k in previous}
    link_delta, link_reset = counter_delta(link_now, link_old)
    if any(link_delta.values()) or link_reset:
        events.append({"event": "link_counter_change", "interface": interface,
                       "delta": link_delta, "reset": link_reset})
    return events


def load_config(path):
    if path is None:
        return []
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or set(data) - {"targets"}:
        raise ValueError("config must contain only a 'targets' list")
    targets = data.get("targets", [])
    if not isinstance(targets, list) or len(targets) > 32:
        raise ValueError("targets must be a list with at most 32 entries")
    names = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) - {
            "name", "interface", "ip", "tcp_port", "heartbeat_file", "max_age_seconds"
        }:
            raise ValueError("unknown target field or invalid target")
        name = target.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("each target needs a unique nonempty name")
        names.add(name)
        iface = target.get("interface", "")
        if not isinstance(iface, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", iface) or iface.startswith("-"):
            raise ValueError("target needs a valid Linux interface name")
        if "ip" in target:
            if not isinstance(target["ip"], str):
                raise ValueError("ip must be an IPv4 string")
            addr = ipaddress.ip_address(target["ip"])
            if addr.version != 4 or addr.is_multicast or addr.is_unspecified or int(addr) == 0xffffffff:
                raise ValueError("use a unicast IPv4 address for each target")
        if "tcp_port" in target:
            port = target["tcp_port"]
            if "ip" not in target or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("tcp_port requires ip and a port from 1 to 65535")
        if "heartbeat_file" in target:
            if not isinstance(target["heartbeat_file"], str) or not Path(target["heartbeat_file"]).is_absolute():
                raise ValueError("heartbeat_file must be an absolute path")
            age = target.get("max_age_seconds", 5)
            if type(age) not in (int, float) or not 0 < age <= 86400:
                raise ValueError("max_age_seconds must be between 0 and 86400")
        elif "max_age_seconds" in target:
            raise ValueError("max_age_seconds requires heartbeat_file")
        if "ip" not in target and "heartbeat_file" not in target:
            raise ValueError("target needs ip or heartbeat_file")
    return targets


def probe_target(target, active):
    result = {"name": target["name"], "interface": target["interface"]}
    if "heartbeat_file" in target:
        try:
            modified = Path(target["heartbeat_file"]).stat().st_mtime
            age = time.time() - modified
            result["heartbeat"] = {"status": "clock_error" if age < -1 else
                                    "stale" if age > target.get("max_age_seconds", 5) else "fresh",
                                    "age_seconds": round(age, 3)}
        except OSError as exc:
            result["heartbeat"] = {"status": "unavailable", "error": str(exc)}
    if active and "ip" in target:
        started = time.monotonic()
        reply = command(["ping", "-n", "-c", "1", "-W", "1", "-I",
                         target["interface"], target["ip"]], timeout=2)
        code = reply.get("exit_code")
        result["icmp"] = {"status": "reply" if code == 0 else "no_reply" if code == 1 else "unavailable",
                          "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                          "details": reply}
        if "tcp_port" in target:
            started = time.monotonic()
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
                    stream.settimeout(1)
                    # Never silently route through another NIC if binding is denied.
                    stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                      target["interface"].encode() + b"\0")
                    stream.connect((target["ip"], target["tcp_port"]))
                status, error = "connected", None
            except PermissionError as exc:
                status, error = "unavailable", str(exc)
            except OSError as exc:
                status, error = "connect_failed", str(exc)
            result["tcp"] = {"status": status, "error": error,
                             "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
    return result


def system_sample():
    memory = {}
    text = read_text("/proc/meminfo") or ""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            memory[key + "_kb"] = int(value.split()[0])
    temperatures = {}
    for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        raw = read_text(path)
        if raw is not None:
            try:
                temperatures[path.parent.name] = {
                    "type": read_text(path.parent / "type"), "celsius": int(raw) / 1000}
            except ValueError:
                pass
    softnet = {"processed": 0, "dropped": 0, "time_squeeze": 0}
    for line in (read_text("/proc/net/softnet_stat") or "").splitlines():
        try:
            values = [int(item, 16) for item in line.split()[:3]]
            for key, value in zip(("processed", "dropped", "time_squeeze"), values):
                softnet[key] += value
        except ValueError:
            continue
    cpu = {}
    for line in (read_text("/proc/stat") or "").splitlines():
        if re.match(r"cpu\d* ", line):
            fields = line.split()
            # /proc/stat guest times are already included in user/nice.
            values = [int(v) for v in fields[1:9]]
            cpu[fields[0]] = {"total_ticks": sum(values), "idle_ticks": sum(values[3:5])}
    return {"load_average": os.getloadavg(), "memory": memory,
            "temperatures": temperatures, "softnet": softnet, "cpu": cpu}


def inventory(interfaces):
    info = {"time": utc_now(), "platform": list(os.uname()),
            "os_release": read_text("/etc/os-release"),
            "tools": {k: shutil.which(k) for k in ("ip", "ping", "ethtool", "journalctl")},
            "nics": {i: read_nic(i) for i in interfaces}, "commands": {}}
    for argv in (["ip", "-j", "address"], ["ip", "-j", "route", "show", "table", "all"],
                 ["ip", "-j", "neigh"], ["journalctl", "-k", "-n", "200", "--no-pager", "-o", "short-iso"]):
        info["commands"][" ".join(argv)] = command(argv)
    for iface in interfaces:
        for opts in ([], ["-i"], ["-S"], ["-k"], ["--show-eee"]):
            argv = ["ethtool"] + opts + [iface]
            info["commands"][" ".join(argv)] = command(argv)
        base = Path("/sys/class/net") / iface / "device"
        info["nics"][iface]["device_path"] = str(base.resolve()) if base.exists() else None
        driver = base / "driver"
        info["nics"][iface]["driver"] = driver.resolve().name if driver.exists() else None
    return info


class Follower:
    def __init__(self, argv, log):
        self.argv, self.log = argv, log
        self.process = None
        self.thread = None
        self.error = None

    def start(self):
        if shutil.which(self.argv[0]) is None:
            self.log.write("collector_unavailable", command=self.argv, reason="not installed")
            return
        self.process = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        encoding="utf-8", errors="replace", bufsize=1,
                                        env=dict(os.environ, LC_ALL="C"))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while True:
                line = self.process.stdout.readline(8192)
                if not line:
                    break
                self.log.write("system_log", source=self.argv[0], message=line.rstrip())
            self.log.write("collector_exit", command=self.argv, exit_code=self.process.wait())
        except OSError as exc:
            self.error = exc
        finally:
            self.process.stdout.close()

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.thread.join(timeout=4)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def monitor(args):
    targets = load_config(args.config)
    interfaces = args.interfaces or sorted(p.name for p in Path("/sys/class/net").iterdir() if p.name != "lo")
    interfaces = list(dict.fromkeys(interfaces))
    for interface in interfaces:
        if interface.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
            raise ValueError("invalid interface: " + interface)
    if not interfaces:
        raise ValueError("no interfaces found; specify --interfaces")
    for target in targets:
        if target["interface"] not in interfaces:
            raise ValueError("target interface is not monitored: " + target["interface"])
    missing = [iface for iface in interfaces if not Path("/sys/class/net", iface).exists()]
    if missing:
        raise ValueError("interfaces not present: " + ", ".join(missing))
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=dt.datetime.now(dt.timezone.utc).strftime("run-%Y%m%dT%H%M%SZ-"), dir=str(output)))
    os.chmod(run_dir, 0o700)
    log = JsonLog(run_dir / "telemetry.jsonl", args.log_mb * 1024 * 1024, args.backups)
    stop = threading.Event()
    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
    followers = []
    print("Log directory:", run_dir, flush=True)
    print("Mode:", "active probes enabled" if args.active_probes else "passive (no ICMP/TCP probes)", flush=True)
    try:
        initial_inventory = inventory(interfaces)
        write_json(run_dir / "inventory-start.json", initial_inventory)
        for tool, location in initial_inventory["tools"].items():
            if location is None:
                print("WARNING: missing tool:", tool, "(related diagnostics unavailable)", flush=True)
        write_json(run_dir / "run.json", {"start": utc_now(), "interfaces": interfaces,
                   "targets": targets, "active_probes": args.active_probes,
                   "interval": args.interval, "duration": args.duration,
                   "log_mb": args.log_mb, "backups": args.backups,
                   "nic_detail_interval": args.nic_detail_interval})
        log.write("start", interfaces=interfaces, active_probes=args.active_probes)
        if not args.no_follow:
            followers = [Follower(["ip", "-ts", "monitor", "link", "address", "route"], log),
                         Follower(["journalctl", "-k", "-f", "-n", "0", "--no-pager", "-o", "short-iso"], log)]
            for follower in followers:
                follower.start()
        previous, probe_states, previous_time, previous_system = {}, {}, None, None
        began = time.monotonic()
        next_detail = began
        detail_jobs = {}
        unsupported_details = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as detail_pool:
                while not stop.is_set():
                    now = time.monotonic()
                    if args.duration and now - began >= args.duration:
                        break
                    if any(f.error for f in followers):
                        raise OSError("background log collection failed; check disk space")
                    current = {iface: read_nic(iface) for iface in interfaces}
                    elapsed = now - previous_time if previous_time is not None else 0
                    if previous_time is not None and elapsed > args.interval * 2:
                        log.write("event", event="sampling_delay", elapsed_seconds=elapsed)
                    for iface in interfaces:
                        for event in nic_changes(iface, current[iface], previous.get(iface), elapsed):
                            log.write("event", **event)
                    jobs = [pool.submit(probe_target, t, args.active_probes) for t in targets]
                    probes = [job.result() for job in jobs]
                    for probe in probes:
                        for kind in ("icmp", "tcp", "heartbeat"):
                            if kind not in probe:
                                continue
                            key = (probe["name"], kind)
                            state = probe[kind]["status"]
                            old = probe_states.get(key)
                            if old != state:
                                log.write("event", event="probe_state", name=probe["name"],
                                          interface=probe["interface"], probe=kind, before=old, after=state)
                            probe_states[key] = state
                    system = system_sample()
                    if previous_system is not None:
                        soft_delta, soft_reset = counter_delta(system["softnet"], previous_system["softnet"])
                        system["softnet_delta"] = soft_delta
                        if soft_delta.get("dropped", 0) or soft_delta.get("time_squeeze", 0) or soft_reset:
                            log.write("event", event="softnet_pressure", delta=soft_delta, reset=soft_reset)
                        cpu_usage = {}
                        for cpu, ticks in system["cpu"].items():
                            old = previous_system["cpu"].get(cpu, {})
                            total = ticks["total_ticks"] - old.get("total_ticks", ticks["total_ticks"])
                            idle = ticks["idle_ticks"] - old.get("idle_ticks", ticks["idle_ticks"])
                            if total > 0 and 0 <= idle <= total:
                                cpu_usage[cpu] = round(100 * (1 - idle / total), 2)
                        system["cpu_busy_percent"] = cpu_usage
                    log.write("sample", elapsed_seconds=elapsed, nics=current, probes=probes,
                              system=system, disk_free_bytes=shutil.disk_usage(run_dir).free)
                    for iface, job in list(detail_jobs.items()):
                        if job.done():
                            result = job.result()
                            log.write("nic_detail", interface=iface, result=result)
                            if result.get("exit_code") != 0:
                                unsupported_details.add(iface)
                            del detail_jobs[iface]
                    if args.nic_detail_interval and now >= next_detail:
                        for iface in interfaces:
                            if iface not in detail_jobs and iface not in unsupported_details:
                                detail_jobs[iface] = detail_pool.submit(command, ["ethtool", "-S", iface], 3)
                        next_detail = now + args.nic_detail_interval
                    previous, previous_time = current, now
                    previous_system = system
                    delay = args.interval - (time.monotonic() - now)
                    if args.duration:
                        delay = min(delay, args.duration - (time.monotonic() - began))
                    stop.wait(max(0, delay))
                for iface, job in detail_jobs.items():
                    log.write("nic_detail", interface=iface, result=job.result())
        log.write("stop", reason="signal" if stop.is_set() else "duration")
    finally:
        for follower in followers:
            follower.stop()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    write_json(run_dir / "inventory-end.json", inventory(interfaces))
    print("Capture finished:", run_dir, flush=True)
    return 0


def report(directory):
    root = Path(directory)
    files = list(root.glob("telemetry.jsonl*"))
    if not files:
        raise ValueError("no telemetry logs in " + str(root))
    events, samples, malformed = deque(maxlen=2000), 0, 0
    event_counts = Counter()
    first, last = None, None
    def rotation_number(path):
        suffix = path.name[len("telemetry.jsonl"):]
        return int(suffix[1:]) if suffix.startswith(".") and suffix[1:].isdigit() else 0
    for path in sorted(files, key=rotation_number, reverse=True):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    timestamp = record["time"]
                    first = min(first, timestamp) if first else timestamp
                    last = max(last, timestamp) if last else timestamp
                    if record["kind"] == "event":
                        events.append(record)
                        event_counts[record.get("event", "unknown")] += 1
                    if record["kind"] == "sample":
                        samples += 1
                except (ValueError, KeyError, TypeError):
                    malformed += 1
    ordered_events = sorted(events, key=lambda item: item.get("monotonic_seconds", 0))
    print(json.dumps({"directory": str(root), "retained_log_files": len(files),
                      "first_retained_time": first, "last_retained_time": last,
                      "samples": samples, "malformed_lines": malformed,
                      "event_count": sum(event_counts.values()), "event_counts": dict(event_counts),
                      "events_truncated": sum(event_counts.values()) > len(events),
                      "events": ordered_events,
                      "note": "Retained window only. No events does not prove camera or frame health. Review system_log, nic_detail and inventories too."},
                     ensure_ascii=False, indent=2))
    return 0 if samples and not malformed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    inv = sub.add_parser("inventory", help="print interface names and state")
    inv.set_defaults(action="inventory")
    run = sub.add_parser("monitor", help="collect bounded logs; no network setting changes")
    run.add_argument("--interfaces", nargs="+", help="default: all current non-loopback interfaces")
    run.add_argument("--config", help="JSON target configuration")
    run.add_argument("--output", default="./lan-diag-logs")
    run.add_argument("--interval", type=float, default=1.0)
    run.add_argument("--duration", type=float, default=0, help="seconds; 0 means until Ctrl+C/SIGTERM")
    run.add_argument("--active-probes", action="store_true", help="enable bound ICMP and explicitly configured TCP connections")
    run.add_argument("--no-follow", action="store_true", help="disable live ip/journal collectors")
    run.add_argument("--log-mb", type=int, default=10, help="per log file, MiB")
    run.add_argument("--backups", type=int, default=9)
    run.add_argument("--nic-detail-interval", type=float, default=60, help="ethtool -S interval; 0 disables periodic collection")
    summary = sub.add_parser("report", help="summarize retained events; does not classify camera health")
    summary.add_argument("directory")
    args = parser.parse_args(argv)
    if args.action == "inventory":
        print(json.dumps({p.name: read_nic(p.name) for p in sorted(Path("/sys/class/net").iterdir())}, indent=2))
        return 0
    if args.action == "report":
        return report(args.directory)
    if (not 0.2 <= args.interval <= 3600 or not 0 <= args.duration <= 31536000 or
            not 1 <= args.log_mb <= 1024 or not 1 <= args.backups <= 99 or
            not 0 <= args.nic_detail_interval <= 86400):
        parser.error("invalid interval, duration, log size, backups or NIC detail interval")
    return monitor(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(2)
