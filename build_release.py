#!/usr/bin/env python3
"""Build versioned portable ZIP/TAR distributions from tracked source files."""
import argparse
import io
from pathlib import Path
import subprocess
import tarfile
import zipfile

from run_config import VERSION


def build(output):
    root = Path(__file__).resolve().parent
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=str(root)).decode().split('\0')
    entries = []
    for name in filter(None, files):
        source = root / name
        mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
        entries.append(('sensor-lab/' + name, source.read_bytes(), mode))
    launcher = (root / 'Sensor Lab.desktop').read_text().replace('Name=Sensor Lab\n', 'Name=Sensor Lab v' + VERSION + '\n').replace('Name[ko]=센서 장비 자동 검사\n', 'Name[ko]=센서 장비 자동 검사 v' + VERSION + '\n')
    entries.append(('Sensor Lab_v' + VERSION + '.desktop', launcher.encode(), 0o755))
    base = output / ('sensor-lab_v' + VERSION)
    zip_path, tar_path = Path(str(base) + '.zip'), Path(str(base) + '.tar.gz')
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    with tarfile.open(tar_path, 'w:gz') as archive:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode, info.size = mode, len(data)
            archive.addfile(info, io.BytesIO(data))
    return zip_path, tar_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='../dist')
    args = parser.parse_args()
    for path in build(args.output):
        print(path.resolve())


if __name__ == '__main__':
    main()
