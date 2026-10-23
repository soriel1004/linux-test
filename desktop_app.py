#!/usr/bin/env python3
"""Ubuntu desktop UI. Privileged measurement runs in a separate, stoppable worker."""
import argparse
import datetime as dt
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

import gui_common
import sensor_lab as lab

ROLE_NAMES = {'composite': '종합 / 일반 (Composite)', 'controller': '컨트롤러',
              'nand': 'NAND / 미디어', 'unspecified': '역할 미확인'}


class App:
    def __init__(self, root, output=None, auto=True):
        self.root = root
        self.output = Path(output or Path.home() / 'SensorLab' / 'results').resolve()
        self.settings = self.output.parent / 'settings' / 'temperature-map.json'
        self.roles, settings_error = {}, None
        try:
            if self.settings.exists():
                parsed = json.loads(self.settings.read_text())
                if not isinstance(parsed, dict):
                    raise ValueError('센서 역할 설정은 JSON 객체여야 합니다.')
                self.roles = parsed
        except (OSError, ValueError) as exc:
            settings_error = str(exc)
        self.process, self.session, self.console = None, None, None
        self.offset, self.partial, self.reports, self.rows = 0, '', [], {}
        self.closing, self.finished = False, False
        root.title('센서 장비 검사 — CPU · NVMe · LAN')
        root.geometry('1100x820')
        root.minsize(900, 680)
        root.protocol('WM_DELETE_WINDOW', self.close)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='센서 장비 자동 검사', font=('', 20, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='중단 설정: CPU 100°C / NVMe 모든 센서 90°C · 장치가 보고한 더 낮은 critical 온도 우선').pack(anchor='w', pady=(4, 8))
        self.cards = {}
        cards = ttk.Frame(frame)
        cards.pack(fill='x')
        for label, key in (('CPU', 'cpu'), ('NVMe 종합 / 일반', 'nvme_composite'),
                           ('NVMe 컨트롤러', 'nvme_controller'), ('NVMe 미확인 센서', 'nvme_unspecified')):
            box = ttk.LabelFrame(cards, text=label, padding=10)
            box.pack(side='left', fill='x', expand=True, padx=(0, 8))
            var = tk.StringVar(value='수집 대기')
            ttk.Label(box, textvariable=var, font=('', 18, 'bold')).pack()
            self.cards[key] = var
        ttk.Label(frame, text='센서 번호만으로 컨트롤러를 추정하지 않습니다. 표에서 원본 이름·장치·사용자 지정 여부를 확인하세요.').pack(anchor='w', pady=(8, 4))
        columns = ('device', 'role', 'label', 'temp', 'critical', 'source')
        self.tree = ttk.Treeview(frame, columns=columns, show='headings', height=7)
        for key, label, width in zip(columns, ('장치 / 모델', '온도 구분', '원본 센서명', '현재 °C', 'critical °C', '분류 근거'), (220, 170, 130, 80, 95, 110)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, stretch=True)
        self.tree.pack(fill='x')
        ttk.Button(frame, text='선택 NVMe 센서 역할 지정…', command=self.map_sensor).pack(anchor='w', pady=5)
        options = ttk.Frame(frame)
        options.pack(fill='x', pady=6)
        ttk.Label(options, text='부하 단계별 시간(분)').pack(side='left')
        self.minutes = tk.StringVar(value='5')
        ttk.Spinbox(options, from_=1, to=60, textvariable=self.minutes, width=5).pack(side='left', padx=6)
        ttk.Label(options, text='NVMe 시험 디렉터리').pack(side='left', padx=(15, 4))
        self.disk = tk.StringVar(value='/var/tmp')
        ttk.Entry(options, textvariable=self.disk, width=25).pack(side='left', fill='x', expand=True)
        ttk.Button(options, text='찾기', command=self.browse_disk).pack(side='left', padx=4)
        self.start_buttons = []
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=3)
        for label, mode in (('자동 검사 시작', 'auto'), ('기록만', 'monitor'), ('CPU만', 'cpu'), ('RAM만', 'memory'), ('NVMe만', 'disk')):
            button = ttk.Button(buttons, text=label, command=lambda m=mode: self.start(m))
            button.pack(side='left', padx=(0, 5))
            self.start_buttons.append(button)
        self.stop_button = ttk.Button(buttons, text='검사 중지', command=self.stop, state='disabled')
        self.stop_button.pack(side='right')
        self.description = tk.StringVar()
        ttk.Label(frame, textvariable=self.description).pack(anchor='w', pady=5)
        self.minutes.trace_add('write', lambda *_: self.update_description())
        self.update_description()
        network = ttk.LabelFrame(frame, text='직결 케이블 LAN 시험', padding=6)
        network.pack(fill='x', pady=4)
        try:
            names = lab.interfaces()
        except ValueError:
            names = []
        self.port_a = tk.StringVar(value=names[0] if names else '')
        self.port_b = tk.StringVar(value=names[1] if len(names) > 1 else '')
        ttk.Combobox(network, textvariable=self.port_a, values=names, width=15, state='readonly').pack(side='left')
        ttk.Label(network, text=' ↔ ').pack(side='left')
        ttk.Combobox(network, textvariable=self.port_b, values=names, width=15, state='readonly').pack(side='left')
        ttk.Label(network, text='  각 방향 Mb/s ').pack(side='left')
        self.mbps = tk.StringVar(value='50')
        ttk.Entry(network, textvariable=self.mbps, width=7).pack(side='left')
        wire_button = ttk.Button(network, text='LAN 시험 시작', command=lambda: self.start('wire'))
        wire_button.pack(side='left', padx=10)
        self.start_buttons.append(wire_button)
        self.status = tk.StringVar(value='준비 — 자동 검사 시작 대기')
        ttk.Label(frame, textvariable=self.status, wraplength=1000, font=('', 11, 'bold')).pack(anchor='w', pady=7)
        self.progress = ttk.Progressbar(frame, mode='indeterminate')
        self.progress.pack(fill='x')
        self.log = tk.Text(frame, height=6, wrap='word', state='disabled')
        self.log.pack(fill='both', expand=True, pady=6)
        footer = ttk.Frame(frame)
        footer.pack(fill='x')
        ttk.Button(footer, text='결과 보고서 열기', command=self.open_report).pack(side='left')
        ttk.Button(footer, text='결과 폴더 열기', command=self.open_folder).pack(side='left', padx=5)
        self.add_log('온도가 없거나 이상이 감지되면 부하를 중단합니다. 검사 완료는 제품 합격 판정이 아닙니다.')
        if settings_error:
            self.add_log('센서 설정 읽기 실패: ' + settings_error)
        root.after(400, self.poll)
        if auto:
            root.after(800, lambda: self.start('auto'))

    def add_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        if int(self.log.index('end-1c').split('.')[0]) > 400:
            self.log.delete('1.0', '100.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def browse_disk(self):
        path = filedialog.askdirectory(parent=self.root, initialdir=self.disk.get(), title='NVMe 위의 기존 디렉터리')
        if path:
            self.disk.set(path)

    def update_description(self):
        try:
            minutes = int(self.minutes.get())
            if not 1 <= minutes <= 60:
                raise ValueError()
            self.description.set('자동 순서: 기준 기록 1분 → CPU → RAM 512 MiB → NVMe 파일 I/O (약 %d분, 스냅샷 시간 별도).\nNVMe 요청 쓰기 약 %.1f GiB / 전용 파일 최대 256 MiB. LAN은 케이블 확인 후 별도 실행.' % (1 + minutes * 3, minutes * 60 * 8 / 1024))
        except ValueError:
            self.description.set('단계 시간을 1~60분으로 입력하세요. NVMe는 전용 파일만 사용하며 LAN은 별도 실행합니다.')

    def start(self, mode):
        if self.process is not None:
            return
        try:
            data = gui_common.validate_job({'mode': mode, 'duration': int(self.minutes.get()) * 60,
                                            'disk_dir': self.disk.get(), 'roles': self.roles,
                                            'interfaces': [self.port_a.get(), self.port_b.get()],
                                            'mbps': float(self.mbps.get()) if mode == 'wire' else 50, 'parent_pid': os.getpid(),
                                            'parent_start': gui_common.process_start(os.getpid())})
        except (ValueError, TypeError) as exc:
            messagebox.showerror('설정 확인', str(exc), parent=self.root)
            return
        if mode == 'wire' and not messagebox.askyesno('케이블 연결 확인', '생산망과 분리된 장비에서 선택한 두 포트를 정상 케이블로 직접 연결했습니까?\n관리 접속 포트는 시험에 사용하지 마세요.', parent=self.root):
            return
        if os.geteuid() != 0 and not shutil.which('pkexec'):
            messagebox.showerror('설치 필요', '실행 아이콘 또는 start.sh로 필요한 패키지를 먼저 설치하세요.', parent=self.root)
            return
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            self.session = Path(tempfile.mkdtemp(prefix='session-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ-'), dir=str(self.output)))
            os.chmod(self.session, 0o700)
            (self.session / 'gui-events.jsonl').touch(mode=0o600)
            lab.write_json(self.session / 'job.json', data)
            self.console = (self.session / 'console.log').open('w')
            args = ['/usr/bin/python3', str(Path(__file__).resolve().with_name('gui_worker.py')), '--job', str(self.session / 'job.json')]
            if os.geteuid() != 0:
                args.insert(0, 'pkexec')
            self.process = subprocess.Popen(args, stdout=self.console, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            if self.console:
                self.console.close()
            messagebox.showerror('시작 실패', str(exc), parent=self.root)
            return
        self.offset, self.partial, self.reports, self.finished = 0, '', [], False
        self.status.set('관리자 승인 대기 / 검사 준비 중…')
        self.add_log('결과 폴더: ' + str(self.session))
        for button in self.start_buttons:
            button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.progress.start(10)

    def stop(self):
        if self.process is not None:
            try:
                (self.session / 'stop').write_text('stop\n')
                self.status.set('중지 요청 — 작업 정리와 보고서 저장을 기다리는 중…')
                self.stop_button.configure(state='disabled')
            except OSError as exc:
                self.add_log('중지 요청 실패: ' + str(exc))

    def handle(self, event):
        kind = event.get('kind')
        if kind == 'metrics':
            for key, var in self.cards.items():
                value = event['summary'].get(key)
                var.set('미확인 / 미제공' if value is None else '%.1f °C' % value)
            self.tree.delete(*self.tree.get_children())
            self.rows = {}
            for index, sensor in enumerate(event['temperatures']):
                iid = str(index)
                self.rows[iid] = sensor
                role = ROLE_NAMES.get(sensor.get('role'), 'CPU' if sensor['kind'] == 'cpu' else '기타')
                source = {'driver_label': '장치 라벨', 'user_mapping': '사용자 지정', 'unconfirmed': '미확인'}.get(sensor.get('role_source'), '')
                self.tree.insert('', 'end', iid=iid, values=(sensor.get('model') or sensor.get('name', ''), role,
                                 sensor['label'], '%.1f' % sensor['celsius'], sensor.get('critical_celsius') or '—', source))
        elif kind == 'stage':
            self.status.set('진행 중: ' + event['label'])
            self.add_log(event['label'] + ' 시작')
        elif kind == 'stage_end':
            self.add_log(event['label'] + (' 관찰 종료' if event['exit_code'] == 0 else ' 중단 / 결과 확인 필요'))
        elif kind == 'error' or kind == 'watch_error':
            self.add_log(event['message'])
        elif kind == 'report':
            self.reports.append(event['path'])
        elif kind == 'done':
            self.finished = True
            self.status.set('관찰 완료 — 제품 전체 합격 판정은 아닙니다.' if event['exit_code'] == 0 else '중단 / 확인 필요 — 기록과 보고서를 확인하세요.')
            self.add_log(self.status.get())

    def read_events(self, final=False):
        while True:
            try:
                with (self.session / 'gui-events.jsonl').open() as stream:
                    stream.seek(self.offset)
                    chunk = stream.read(1024 * 1024)
                    text = self.partial + chunk
                    self.offset = stream.tell()
                lines = text.split('\n')
                self.partial = lines.pop()
                for line in lines:
                    if line:
                        self.handle(json.loads(line))
            except (OSError, ValueError, KeyError) as exc:
                self.add_log('화면 기록 읽기: ' + str(exc))
                break
            if not final or not chunk:
                break

    def poll(self):
        if self.process is not None:
            self.read_events()
            if self.process.poll() is not None:
                self.read_events(final=True)
                code = self.process.returncode
                self.process = None
                self.console.close()
                self.progress.stop()
                for button in self.start_buttons:
                    button.configure(state='normal')
                self.stop_button.configure(state='disabled')
                if not self.finished:
                    self.status.set('관리자 승인이 취소되었거나 실패했습니다. 검사를 다시 시작할 수 있습니다.' if code in (126, 127) else
                                    '검사가 시작되지 않았거나 예기치 않게 종료됐습니다. console.log를 확인하세요. (코드 %s)' % code)
                    self.add_log(self.status.get())
                self.save_index()
                if self.closing:
                    self.root.destroy()
                    return
        self.root.after(400, self.poll)

    def save_index(self):
        links = ''.join('<li><a href="' + html.escape(os.path.relpath(p, self.session), quote=True) + '">' + html.escape(Path(p).parent.name) + '</a></li>' for p in self.reports)
        (self.session / 'index.html').write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><title>센서 장비 검사</title><h1>검사 결과</h1><p>' + html.escape(self.status.get()) + '</p><p>중단 설정: CPU 100°C / NVMe 90°C. 장치의 더 낮은 critical 값 우선.</p><ul>' + links + '</ul><p>단계별 보고서와 console.log를 확인하세요. 실제 센서·SDK·게임 정상 여부를 판정하지 않습니다.</p></html>')

    def open_report(self):
        if self.session and (self.session / 'index.html').exists():
            webbrowser.open((self.session / 'index.html').as_uri())
        elif self.reports:
            webbrowser.open(Path(self.reports[-1]).as_uri())
        else:
            messagebox.showinfo('보고서', '검사 종료 후 보고서가 생성됩니다.', parent=self.root)

    def open_folder(self):
        path = self.session or self.output
        path.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(['xdg-open', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def map_sensor(self):
        selected = self.tree.selection()
        sensor = self.rows.get(selected[0]) if selected else None
        if not sensor or sensor['kind'] != 'nvme':
            messagebox.showinfo('센서 선택', '표에서 NVMe 센서를 먼저 선택하세요.', parent=self.root)
            return
        if self.process is not None:
            messagebox.showinfo('검사 진행 중', '검사를 중지한 뒤 센서 역할을 지정하세요. 현재 검사의 분류는 변경하지 않습니다.', parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('NVMe 센서 역할 지정')
        dialog.transient(self.root)
        box = ttk.Frame(dialog, padding=15)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text=(sensor.get('model') or '') + ' / ' + sensor['label']).pack(anchor='w')
        ttk.Label(box, text='Sensor 1/2의 의미는 모델마다 다릅니다. 제조사 자료를 확인한 경우에만 지정하세요.').pack(anchor='w', pady=8)
        role = tk.StringVar(value=sensor.get('role', 'unspecified'))
        ttk.Combobox(box, textvariable=role, values=gui_common.ROLES, state='readonly').pack(fill='x')
        ttk.Label(box, text='확인 근거 (문서/모델 정보)').pack(anchor='w', pady=(8, 0))
        evidence = tk.StringVar()
        ttk.Entry(box, textvariable=evidence, width=65).pack(fill='x')
        def save():
            if not 1 <= len(evidence.get().strip()) <= 500:
                messagebox.showerror('확인 근거 필요', '제조사 자료 등 확인 근거를 1~500자로 입력하세요.', parent=dialog)
                return
            self.roles[sensor['sensor_key']] = {'role': role.get(), 'evidence': evidence.get().strip()}
            try:
                self.settings.parent.mkdir(parents=True, exist_ok=True)
                lab.write_json(self.settings, self.roles)
            except OSError as exc:
                messagebox.showerror('저장 실패', str(exc), parent=dialog)
                return
            self.add_log('다음 검사부터 사용자 지정 적용: ' + sensor['label'] + ' → ' + role.get())
            dialog.destroy()
        ttk.Button(box, text='저장', command=save).pack(anchor='e', pady=10)

    def close(self):
        if self.process is not None:
            self.closing = True
            self.stop()
        else:
            self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-auto', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    root = tk.Tk()
    App(root, args.output, auto=not args.no_auto)
    root.mainloop()


if __name__ == '__main__':
    main()
