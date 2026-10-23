#!/usr/bin/env bash
# Ubuntu packages only. Existing packages are skipped; no boot service/network changes.
set -Eeuo pipefail
if [[ "$EUID" != 0 ]]; then
    printf 'Run: sudo bash setup.sh\n' >&2
    exit 2
fi
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || { printf 'Ubuntu is required.\n' >&2; exit 2; }
dpkg --compare-versions "${VERSION_ID:-0}" ge 20.04 || { printf 'Ubuntu 20.04+ is required.\n' >&2; exit 2; }
packages=(python3 iproute2 ethtool nvme-cli smartmontools lm-sensors pciutils usbutils stress-ng)
missing=()
for package in "${packages[@]}"; do
    if [[ "$(dpkg-query -W -f='${Status}' -- "$package" 2>/dev/null || true)" == 'install ok installed' ]]; then
        printf 'SKIP installed package: %s\n' "$package"
    else
        missing+=("$package")
    fi
done
if ((${#missing[@]})); then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
else
    printf 'All packages already installed; skipping APT download.\n'
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 2)'
printf 'Ready. Start with: sudo python3 sensor_lab.py inventory\n'
