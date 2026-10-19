import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import service_runner as service

ROOT = Path(__file__).resolve().parent.parent


class ServiceTests(unittest.TestCase):
    def test_service_config_blocks_active_modes_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for data in ({"active_probes": True}, {"interval_seconds": True},
                         {"interfaces": ["eth0", "eth0"]}, {"interfaces": "eth0"},
                         {"max_total_mb": 50}, {"log_mb": 0}, {"interval_seconds": float("nan")}):
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        service.load_config(path)
            path.write_text("{}")
            config = service.load_config(path)
            command = service.build_command(config, ["eth0"], Path(tmp))
            self.assertNotIn("--active-probes", command)
            self.assertNotIn("wire_test.py", " ".join(command))
            self.assertEqual(command[command.index("--duration") + 1], "0")

    def test_auto_detection_only_selects_physical_ethernet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, kind, physical in (("eth0", "1", True), ("docker0", "1", False),
                                          ("lo", "772", False), ("wwan0", "512", True)):
                path = root / name
                path.mkdir()
                (path / "type").write_text(kind)
                if physical:
                    (path / "device").mkdir()
            self.assertEqual(service.detect_interfaces(root), ["eth0"])

    def test_retention_preserves_active_unknown_and_symlink_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "logs"
            output.mkdir()
            for n in range(3):
                path = output / ("session-" + str(n))
                path.mkdir()
                (path / service.MARKER).write_text(json.dumps({"owner": "lan-diag", "version": 1}))
                (path / "data").write_bytes(b"a" * 100)
                os.utime(path, (n + 1, n + 1))
            unknown = output / "session-user-files"
            unknown.mkdir()
            external = Path(tmp) / "external"
            external.mkdir()
            (external / service.MARKER).write_text(json.dumps({"owner": "lan-diag", "version": 1}))
            (output / "session-symlink").symlink_to(external, target_is_directory=True)
            config = dict(service.DEFAULTS, max_sessions=1)
            active = output / "session-0"  # active must survive even if it is oldest
            with contextlib.redirect_stdout(io.StringIO()):
                deleted = service.prune_sessions(output, config, active)
            self.assertEqual(len(deleted), 2)
            self.assertTrue(active.exists())
            self.assertTrue(unknown.exists())
            self.assertTrue(external.exists())

    def test_retention_enforces_bytes_as_well_as_session_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n in range(2):
                path = root / ("session-" + str(n))
                path.mkdir()
                (path / service.MARKER).write_text(json.dumps({"owner": "lan-diag", "version": 1}))
                (path / "large").write_bytes(b"x" * 700000)
                os.utime(path, (n + 1, n + 1))
            with contextlib.redirect_stdout(io.StringIO()):
                deleted = service.prune_sessions(root, dict(service.DEFAULTS, max_total_mb=1), root / "session-1")
            self.assertEqual(deleted, [str(root / "session-0")])

    def test_health_rejects_old_or_non_sample_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "session-a" / "run-a" / "telemetry.jsonl"
            path.parent.mkdir(parents=True)
            now = time.time()
            def write(kind, timestamp):
                path.write_text(json.dumps({"kind": kind, "time": dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()}) + "\n")
                os.utime(path, (now, now))
            write("sample", now - 100)
            self.assertIsNone(service.recent_sample(root, now=now))
            write("event", now)
            self.assertIsNone(service.recent_sample(root, now=now))
            write("sample", now)
            self.assertEqual(service.recent_sample(root, since=now - 1, now=now), str(path))
            self.assertIsNone(service.recent_sample(root, since=now + 1, now=now))

    def test_real_supervisor_launch_sample_duplicate_lock_and_sigterm(self):
        interfaces = sorted(p.name for p in Path("/sys/class/net").iterdir() if p.name != "lo")
        if not interfaces:
            self.skipTest("no non-loopback interface")
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            output = Path(tmp) / "logs"
            config.write_text(json.dumps(dict(service.DEFAULTS, interfaces=interfaces[:1], interval_seconds=0.2)))
            argv = [sys.executable, str(ROOT / "service_runner.py"), "--config", str(config), "--output", str(output)]
            with (Path(tmp) / "stdout").open("w+") as stream:
                process = subprocess.Popen(argv, stdout=stream, stderr=stream)
                try:
                    deadline = time.monotonic() + 15
                    sample = None
                    while time.monotonic() < deadline:
                        sample = service.recent_sample(output)
                        if sample or process.poll() is not None:
                            break
                        time.sleep(0.05)
                    self.assertIsNotNone(sample, "supervisor did not produce a real sample")
                    duplicate = subprocess.run(argv, capture_output=True, text=True, timeout=5)
                    self.assertEqual(duplicate.returncode, 2)
                    self.assertIn("already owns", duplicate.stderr)
                    process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=15), 0)
                    self.assertTrue(list(output.glob("session-*/run-*/inventory-end.json")))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()


class InstallerTests(unittest.TestCase):
    def stage(self, destination):
        return subprocess.run(["bash", str(ROOT / "install.sh"), "--source-dir", str(ROOT),
                               "--destdir", str(destination)], capture_output=True, text=True, timeout=30)

    def test_staged_install_reinstall_and_config_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "root"
            first = self.stage(dest)
            self.assertEqual(first.returncode, 0, first.stderr)
            current = dest / "opt/lan-diag/current"
            self.assertTrue((current / "service_runner.py").is_file())
            release = current.resolve()
            config = dest / "etc/lan-diag/config.json"
            data = json.loads(config.read_text())
            data["interval_seconds"] = 2
            config.write_text(json.dumps(data))
            unit = dest / "etc/systemd/system/lan-diag.service"
            unit.write_text(unit.read_text() + "\n# existing local edit\n")
            second = self.stage(dest)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(config.read_text())["interval_seconds"], 2)
            self.assertEqual(current.resolve(), release)
            backups = list((dest / "opt/lan-diag/backups").glob("unit-*/lan-diag.service"))
            self.assertEqual(len(backups), 1)
            self.assertIn("existing local edit", backups[0].read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual((dest / "var/log/lan-diag").stat().st_mode & 0o777, 0o700)

    def test_invalid_existing_config_does_not_switch_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            self.assertEqual(self.stage(dest).returncode, 0)
            current = dest / "opt/lan-diag/current"
            old = os.readlink(current)
            config = dest / "etc/lan-diag/config.json"
            config.write_text('{"active_probes": true}')
            failed = self.stage(dest)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(os.readlink(current), old)
            self.assertEqual(config.read_text(), '{"active_probes": true}')

    def test_staging_parent_symlink_cannot_escape_to_external_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            external = Path(tmp) / "outside"
            root.mkdir()
            external.mkdir()
            (root / "etc").symlink_to(external, target_is_directory=True)
            result = self.stage(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink installation path", result.stderr)
            self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
