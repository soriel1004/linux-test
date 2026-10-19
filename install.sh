#!/usr/bin/env bash
# Download, install and start passive LAN diagnostics on Ubuntu 20.04+.
set -Eeuo pipefail
umask 077

repository='https://github.com/soriel1004/linux-test.git'
ref='main'
source_dir=''
destdir=''
skip_packages=0
temporary=''
stage=''

usage() {
    cat <<'EOF'
Usage: sudo bash install.sh [options]
  --ref REF          Git branch/tag/full commit (default: main)
  --source-dir DIR   Install local downloaded files instead of fetching GitHub
  --skip-packages    Use already installed packages (offline installation)
  --destdir DIR      Stage files under DIR only; no apt, systemctl or root needed
  --help            Show this help

Default: download files, install packages, preserve existing config, enable the
lan-diag.service and verify a fresh passive capture sample. No load test runs.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
cleanup() {
    if [[ -n "$stage" && -d "$stage" ]]; then rm -rf -- "$stage"; fi
    if [[ -n "$temporary" && -d "$temporary" ]]; then rm -rf -- "$temporary"; fi
}
trap cleanup EXIT
trap 'printf "Installation failed at line %s. Existing config/logs are preserved.\n" "$LINENO" >&2' ERR

while (($#)); do
    case "$1" in
        --ref|--source-dir|--destdir)
            (($# >= 2)) || die "missing value for $1"
            case "$1" in
                --ref) ref=$2 ;;
                --source-dir) source_dir=$2 ;;
                --destdir) destdir=$2 ;;
            esac
            shift 2 ;;
        --skip-packages) skip_packages=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done
[[ -n "$ref" && "$ref" != -* && "$ref" != *:* && "$ref" != *' '* ]] || die 'invalid Git ref'

if [[ -n "$destdir" ]]; then
    mkdir -p -- "$destdir"
    destdir=$(cd -- "$destdir" && pwd -P)
    [[ "$destdir" != / ]] || die '--destdir must not be /'
    skip_packages=1
    printf 'Staging only: %s (no host installation/service changes)\n' "$destdir"
else
    ((EUID == 0)) || die 'run with sudo, or use --destdir to stage files'
    # shellcheck source=/etc/os-release
    . /etc/os-release
    [[ "${ID:-}" == ubuntu ]] || die 'supported OS: Ubuntu 20.04 or newer'
    dpkg --compare-versions "${VERSION_ID:-0}" ge 20.04 || die 'Ubuntu 20.04+ is required'
    [[ -d /run/systemd/system ]] || die 'systemd must be running (not a plain container/WSL without systemd)'
    command -v systemctl >/dev/null || die 'systemctl is required'
    if ((skip_packages == 0)); then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            python3 git ca-certificates iproute2 ethtool iputils-ping
    fi
    for tool in python3 ip ethtool ping journalctl; do
        command -v "$tool" >/dev/null || die "missing $tool; install required packages first"
    done
fi
command -v python3 >/dev/null || die 'python3 is required'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || die 'Python 3.8+ is required'

temporary=$(mktemp -d)
if [[ -z "$source_dir" ]]; then
    command -v git >/dev/null || die 'git is required to download; alternatively use --source-dir'
    source_dir="$temporary/source"
    git init -q "$source_dir"
    git -C "$source_dir" remote add origin "$repository"
    GIT_TERMINAL_PROMPT=0 git -c http.sslVerify=true -C "$source_dir" fetch --depth=1 origin "$ref"
    git -C "$source_dir" checkout -q --detach FETCH_HEAD
    printf 'Downloaded commit: '
    git -C "$source_dir" rev-parse HEAD
else
    source_dir=$(cd -- "$source_dir" && pwd -P)
fi

files=(lan_diag.py wire_test.py service_runner.py targets.example.json service_config.example.json README.md VALIDATION.md)
for name in "${files[@]}" lan-diag.service; do
    [[ -f "$source_dir/$name" && ! -L "$source_dir/$name" ]] || die "missing or symlink source file: $name"
done

base="$destdir/opt/lan-diag"
config_dir="$destdir/etc/lan-diag"
unit_dir="$destdir/etc/systemd/system"
logs="$destdir/var/log/lan-diag"
# Check every parent, including a staging root's etc/opt/var components, so a
# symlink cannot redirect a staging install onto the host filesystem.
python3 - "$base" "$config_dir" "$unit_dir" "$logs" <<'PY'
from pathlib import Path
import sys
for name in sys.argv[1:]:
    path = Path(name)
    for part in (path, *path.parents):
        if part.is_symlink():
            sys.exit('ERROR: refusing symlink installation path: ' + str(part))
PY
for path in "$base" "$config_dir" "$unit_dir" "$logs" "$base/releases" "$base/backups"; do
    [[ ! -L "$path" ]] || die "refusing symlink installation directory: $path"
done
install -d -m 0755 "$base" "$base/releases" "$unit_dir"
install -d -m 0700 "$config_dir" "$logs" "$base/backups"
[[ ! -L "$config_dir/config.json" ]] || die 'refusing symlink config.json'
if [[ -e "$config_dir/config.json" && ! -f "$config_dir/config.json" ]]; then die 'config.json is not a regular file'; fi
if [[ -e "$base/current" && ! -L "$base/current" ]]; then die 'current must be an installer-managed symlink'; fi

# Validate an existing user's config before switching installed files/services.
config_source="$config_dir/config.json"
if [[ ! -f "$config_source" ]]; then config_source="$source_dir/service_config.example.json"; fi
python3 "$source_dir/service_runner.py" --config "$config_source" --check
stage=$(mktemp -d "$base/releases/.stage.XXXXXXXX")
for name in "${files[@]}"; do install -m 0644 "$source_dir/$name" "$stage/$name"; done
PYTHONPYCACHEPREFIX="$temporary/pycache" python3 -m py_compile \
    "$stage/lan_diag.py" "$stage/wire_test.py" "$stage/service_runner.py"
version=$(python3 - "$stage" "${files[@]}" <<'PY'
import hashlib
from pathlib import Path
import sys
digest = hashlib.sha256()
for name in sys.argv[2:]:
    digest.update(name.encode() + b'\0')
    digest.update((Path(sys.argv[1]) / name).read_bytes())
print(digest.hexdigest()[:24])
PY
)
release="$base/releases/$version"
if [[ -e "$release" || -L "$release" ]]; then
    [[ -d "$release" && ! -L "$release" ]] || die 'invalid existing release directory'
    for name in "${files[@]}"; do cmp -s "$stage/$name" "$release/$name" || die "existing release modified: $name"; done
    rm -rf -- "$stage"
else
    chmod 0755 "$stage"
    mv -- "$stage" "$release"
fi
stage=''
if [[ ! -e "$config_dir/config.json" ]]; then
    install -m 0600 "$source_dir/service_config.example.json" "$config_dir/config.json"
else
    printf 'Preserved existing config: %s\n' "$config_dir/config.json"
fi
unit="$unit_dir/lan-diag.service"
[[ ! -L "$unit" ]] || die 'refusing symlink service unit'
if [[ -e "$unit" && ! -f "$unit" ]]; then die 'service unit is not a regular file'; fi
if [[ -f "$unit" ]] && ! cmp -s "$source_dir/lan-diag.service" "$unit"; then
    backup=$(mktemp -d "$base/backups/unit-XXXXXXXX")
    cp -p -- "$unit" "$backup/lan-diag.service"
    printf 'Previous service unit backed up: %s\n' "$backup"
fi
install -m 0644 "$source_dir/lan-diag.service" "$unit"
# Atomic pointer update; keep previous releases for manual rollback.
stage=$(mktemp -d "$base/.activate.XXXXXXXX")
ln -s "releases/$version" "$stage/current"
mv -Tf -- "$stage/current" "$base/current"
rm -rf -- "$stage"
stage=''
printf 'Installed release: %s\n' "$release"

if [[ -n "$destdir" ]]; then
    printf 'Staging complete. No packages installed or services started.\n'
    exit 0
fi
systemctl daemon-reload
systemctl enable lan-diag.service
since=$(python3 -c 'import time; print(time.time())')
systemctl restart lan-diag.service
printf 'Waiting for a new capture sample (up to 180 seconds)...\n'
for ((attempt=0; attempt<90; attempt++)); do
    if systemctl is-active --quiet lan-diag.service && \
        python3 "$base/current/service_runner.py" --health --output "$logs" --since "$since" >/dev/null 2>&1; then
        printf 'Ready: boot autostart enabled and fresh passive sample verified.\n'
        printf 'Config: %s\nLogs: %s\n' "$config_dir/config.json" "$logs"
        printf 'Status: sudo systemctl status lan-diag.service\n'
        exit 0
    fi
    sleep 2
done
systemctl status --no-pager --full lan-diag.service || true
journalctl -u lan-diag.service -n 40 --no-pager || true
die 'installed, but no fresh capture sample was verified; check interface config and service logs'
