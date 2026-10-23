"""Desktop job validation and stage selection, independent of Tk/display."""
import os
from pathlib import Path

MODES = ('auto', 'monitor', 'cpu', 'memory', 'disk', 'wire')
ROLES = ('composite', 'controller', 'nand', 'unspecified')


def process_start(pid):
    try:
        return Path('/proc', str(pid), 'stat').read_text().rsplit(') ', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def validate_job(data):
    if not isinstance(data, dict) or set(data) - {'mode', 'duration', 'disk_dir', 'interfaces', 'mbps', 'roles', 'parent_pid', 'parent_start'}:
        raise ValueError('invalid desktop job fields')
    mode = data.get('mode')
    if mode not in MODES:
        raise ValueError('unknown test mode')
    duration = data.get('duration', 300)
    if type(duration) is not int or not 10 <= duration <= 3600:
        raise ValueError('stage duration must be 10..3600 seconds')
    disk_dir = data.get('disk_dir', '/var/tmp')
    if not isinstance(disk_dir, str) or not Path(disk_dir).is_absolute():
        raise ValueError('disk directory must be absolute')
    roles = data.get('roles', {})
    if not isinstance(roles, dict) or len(roles) > 100:
        raise ValueError('invalid sensor map')
    for key, item in roles.items():
        if (not isinstance(key, str) or not isinstance(item, dict) or
                item.get('role') not in ROLES or not isinstance(item.get('evidence'), str) or
                not 1 <= len(item['evidence'].strip()) <= 500):
            raise ValueError('sensor roles require role and model/document evidence')
    if type(data.get('parent_pid')) is not int or data['parent_pid'] <= 1 or not isinstance(data.get('parent_start'), str):
        raise ValueError('invalid parent process')
    if mode == 'wire':
        names = data.get('interfaces')
        if not isinstance(names, list) or len(names) != 2 or len(set(names)) != 2:
            raise ValueError('select two distinct LAN ports')
        import re
        if any(not isinstance(n, str) or n.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', n) for n in names):
            raise ValueError('invalid LAN port name')
        mbps = data.get('mbps', 50)
        if type(mbps) not in (int, float) or not 0.1 <= mbps <= 9000:
            raise ValueError('invalid LAN load')
    return dict(data, mode=mode, duration=duration, disk_dir=disk_dir, roles=roles)


def stages(job, output):
    duration = str(job['duration'])
    cpu = ('CPU 부하', 'load', ['--cpu', '--workers', str(min(2, os.cpu_count() or 1)), '--cpu-load', '60'])
    memory = ('RAM 512 MiB', 'load', ['--memory-mb', '512'])
    disk = ('NVMe 파일 I/O', 'load', ['--disk-dir', job['disk_dir'], '--disk-mib', '8'])
    by_mode = {'cpu': [cpu], 'memory': [memory], 'disk': [disk],
               'auto': [('기준 기록 1분', 'monitor', []), cpu, memory, disk],
               'monitor': [('상태 기록', 'monitor', [])],
               'wire': [('LAN 양방향', 'wire', ['--interfaces', *job.get('interfaces', []),
                                               '--mbps', str(job.get('mbps', 50)), '--run'])]}
    result = []
    for label, kind, args in by_mode[job['mode']]:
        seconds = '60' if job['mode'] == 'auto' and kind == 'monitor' else duration
        result.append((label, kind, (['monitor'] if kind == 'monitor' else []) + args +
                       ['--duration', seconds, '--output', str(output)]))
    return result
