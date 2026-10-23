import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import gui_common
import gui_worker
import sensor_lab as lab

ROOT = Path(__file__).resolve().parent.parent


def job_data(mode='auto'):
    return {'mode': mode, 'duration': 300, 'disk_dir': '/var/tmp', 'roles': {},
            'parent_pid': os.getpid(), 'parent_start': gui_common.process_start(os.getpid())}


class TemperatureTests(unittest.TestCase):
    def test_100_and_90_are_actual_defaults_without_earlier_margin(self):
        sensors = [{'kind': 'cpu', 'celsius': 99.9, 'critical_celsius': 100},
                   {'kind': 'nvme', 'celsius': 89.9, 'critical_celsius': 95}]
        self.assertTrue(lab.thermal_status(sensors)['safe_to_load'])
        sensors[0]['celsius'] = 100
        self.assertEqual(lab.thermal_status(sensors)['hot'][0]['stop_limit_celsius'], 100)
        sensors[0]['celsius'] = 50
        sensors[1]['celsius'] = 90
        self.assertFalse(lab.thermal_status(sensors)['safe_to_load'])
        sensors[1].update(celsius=82, critical_celsius=82)
        self.assertEqual(lab.thermal_status(sensors)['hot'][0]['stop_limit_celsius'], 82)

    def test_sensor_number_is_not_a_controller_definition(self):
        self.assertEqual(lab.sensor_role('Composite'), 'composite')
        self.assertEqual(lab.sensor_role('Controller Temperature'), 'controller')
        self.assertEqual(lab.sensor_role('NAND Temperature'), 'nand')
        self.assertEqual(lab.sensor_role('Sensor 1'), 'unspecified')
        self.assertEqual(lab.sensor_role('Sensor 2'), 'unspecified')
        self.assertEqual(lab.model_profile('GIGABYTE GP-G440E250G'), 'GIGABYTE G440E250G')
        self.assertIn('EM280256GMN58-PE33TAS', lab.model_profile('EM280256GMN58-PE33TAS'))

    def test_per_device_roles_and_explicit_mapping_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'hwmon0'
            device = path / 'device'
            device.mkdir(parents=True)
            (device / 'model').write_text('G440E250G')
            (device / 'serial').write_text('TEST-DEVICE')
            (path / 'name').write_text('nvme')
            for n, label, temp in ((1, 'Composite', '70000'), (2, 'Sensor 1', '85000')):
                (path / ('temp%d_input' % n)).write_text(temp)
                (path / ('temp%d_label' % n)).write_text(label)
            raw = lab.temperatures(root, root / 'absent')
            self.assertEqual([s['role'] for s in raw], ['composite', 'unspecified'])
            key = raw[1]['sensor_key']
            with patch.object(lab, 'NVME_SENSOR_MAP', {key: {'role': 'controller', 'evidence': 'Test manufacturer documentation'}}):
                mapped = lab.temperatures(root, root / 'absent')
            self.assertEqual(mapped[1]['role_source'], 'user_mapping')
            self.assertEqual(lab.temperature_summary(mapped)['nvme_controller'], 85)
            self.assertEqual(lab.temperature_summary(mapped)['nvme_composite'], 70)
            self.assertEqual(lab.temperature_summary(mapped)['nvme'], 85)
            self.assertIsNone(lab.temperature_summary(raw)['nvme_controller'])


class DesktopWorkflowTests(unittest.TestCase):
    def test_auto_sequence_excludes_unattended_lan_and_keeps_bounded_disk(self):
        job = gui_common.validate_job(job_data())
        steps = gui_common.stages(job, '/tmp/output with spaces')
        self.assertEqual([kind for _, kind, _ in steps], ['monitor', 'load', 'load', 'load'])
        self.assertEqual(steps[0][2][steps[0][2].index('--duration') + 1], '60')
        self.assertIn('--disk-dir', steps[-1][2])
        self.assertIn('8', steps[-1][2])
        self.assertNotIn('wire', [kind for _, kind, _ in steps])

    def test_invalid_jobs_and_unconfirmed_mapping_rejected(self):
        for data in (dict(job_data(), mode='shell'), dict(job_data(), duration=True),
                     dict(job_data(), disk_dir='relative'),
                     dict(job_data(), roles={'ssd': {'role': 'controller', 'evidence': ''}}),
                     dict(job_data('wire'), interfaces=['lo', 'lo'])):
            with self.assertRaises(ValueError):
                gui_common.validate_job(data)

    def test_worker_stops_auto_sequence_on_first_failed_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'job.json'
            path.write_text(json.dumps(job_data()))
            (path.parent / 'gui-events.jsonl').touch()
            with patch.object(lab, 'main', return_value=0) as baseline, patch.object(gui_worker.load_test, 'main', return_value=2) as load:
                self.assertEqual(gui_worker.run(path), 2)
            baseline.assert_called_once()
            load.assert_called_once()
            events = [json.loads(line) for line in (path.parent / 'gui-events.jsonl').read_text().splitlines()]
            self.assertEqual(len([e for e in events if e['kind'] == 'stage']), 2)
            self.assertEqual(events[-1]['kind'], 'done')

    def test_real_worker_stop_file_generates_report_and_leaves_no_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'job.json'
            path.write_text(json.dumps(job_data('monitor')))
            (root / 'gui-events.jsonl').touch()
            with (root / 'console').open('w') as console:
                process = subprocess.Popen([sys.executable, str(ROOT / 'gui_worker.py'), '--job', str(path)],
                                           stdout=console, stderr=console, env=dict(os.environ, PKEXEC_UID=str(os.getuid())))
                try:
                    deadline = time.monotonic() + 20
                    files = []
                    while time.monotonic() < deadline:
                        files = list(root.glob('results/monitor-*/telemetry.jsonl'))
                        if files and '"sample"' in files[0].read_text():
                            break
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    self.assertTrue(files)
                    (root / 'stop').write_text('stop')
                    self.assertEqual(process.wait(timeout=20), 2)
                    self.assertTrue((files[0].parent / 'report.html').exists())
                    events = [json.loads(line) for line in (root / 'gui-events.jsonl').read_text().splitlines()]
                    self.assertTrue(events[-1]['user_stopped'])
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()

    def test_package_installer_skips_all_or_installs_only_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / 'calls'
            # Execute the real install function with APT/dpkg test doubles; no root or host changes.
            script = '''source "$1"
dpkg-query() { if [[ "$*" == *missing* ]]; then return 1; else printf 'install ok installed'; fi; }
apt-get() { printf '%s\\n' "$*" >> "$TEST_APT_CALLS"; }
install_missing_packages present-a present-b
'''
            env = dict(os.environ, TEST_APT_CALLS=str(calls))
            first = subprocess.run(['bash', '-c', script, 'test', str(ROOT / 'setup.sh')], env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertFalse(calls.exists())
            second = subprocess.run(['bash', '-c', script.replace('present-a present-b', 'present-a missing-b'), 'test', str(ROOT / 'setup.sh')], env=env, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(calls.read_text().splitlines(), ['update', 'install -y --no-install-recommends missing-b'])


@unittest.skipUnless(os.environ.get('DISPLAY'), 'GUI test requires a display/Xvfb')
class TkSmokeTests(unittest.TestCase):
    def test_window_displays_composite_and_keeps_unmapped_controller_unknown(self):
        try:
            import tkinter as tk
            import desktop_app
        except ImportError:
            self.skipTest('system Python tkinter required')
        with tempfile.TemporaryDirectory() as tmp:
            root = tk.Tk()
            try:
                app = desktop_app.App(root, tmp, auto=False)
                root.update()
                sensors = [dict(kind='nvme', celsius=70, role='composite', role_source='driver_label', label='Composite', name='nvme', model='G440E250G', sensor_key='a'),
                           dict(kind='nvme', celsius=85, role='unspecified', role_source='unconfirmed', label='Sensor 1', name='nvme', model='G440E250G', sensor_key='b')]
                app.handle({'kind': 'metrics', 'temperatures': sensors, 'summary': lab.temperature_summary(sensors)})
                root.update()
                self.assertIn('70.0', app.cards['nvme_composite'].get())
                self.assertIn('미확인', app.cards['nvme_controller'].get())
                self.assertEqual(len(app.tree.get_children()), 2)
                app.minutes.set('10')
                self.assertIn('31분', app.description.get())
                self.assertIn('4.7 GiB', app.description.get())
            finally:
                root.destroy()

    def test_final_worker_event_is_drained_before_ui_marks_completion(self):
        import tkinter as tk
        import desktop_app
        with tempfile.TemporaryDirectory() as tmp:
            root = tk.Tk()
            try:
                app = desktop_app.App(root, tmp, auto=False)
                app.session = Path(tmp)
                path = app.session / 'gui-events.jsonl'
                path.touch()
                app.console = io.StringIO()
                class FinishingProcess:
                    returncode = 0
                    def poll(self):
                        path.write_text(json.dumps({'kind': 'done', 'exit_code': 0}) + '\n')
                        return 0
                app.process = FinishingProcess()
                app.poll()
                self.assertTrue(app.finished)
                self.assertIn('관찰 완료', app.status.get())
                self.assertTrue((app.session / 'index.html').exists())
            finally:
                root.destroy()


if __name__ == '__main__':
    unittest.main()
