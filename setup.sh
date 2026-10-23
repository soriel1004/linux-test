#!/usr/bin/env bash
# Missing packages only; --check never installs or requires root.
set -Eeuo pipefail
packages=(python3 python3-tk policykit-1 fonts-noto-cjk xdg-utils iproute2 ethtool nvme-cli smartmontools lm-sensors pciutils usbutils stress-ng)

install_missing_packages() {
    local package
    local -a missing=()
    for package in "$@"; do
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
}

main() {
    local check=0 package missing=0
    case "${1:-}" in
        --check) check=1 ;;
        '') ;;
        *) printf 'Usage: sudo bash setup.sh | bash setup.sh --check\n' >&2; return 2 ;;
    esac
    if ((check)); then
        for package in "${packages[@]}"; do
            if [[ "$(dpkg-query -W -f='${Status}' -- "$package" 2>/dev/null || true)" != 'install ok installed' ]]; then
                printf 'MISSING: %s\n' "$package"
                missing=1
            fi
        done
        return "$missing"
    fi
    [[ "$EUID" == 0 ]] || { printf 'Run: sudo bash setup.sh\n' >&2; return 2; }
    . /etc/os-release
    [[ "${ID:-}" == ubuntu ]] || { printf 'Ubuntu is required.\n' >&2; return 2; }
    dpkg --compare-versions "${VERSION_ID:-0}" ge 20.04 || { printf 'Ubuntu 20.04+ is required.\n' >&2; return 2; }
    install_missing_packages "${packages[@]}"
    /usr/bin/python3 -c 'import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 8) else 2)'
    command -v pkexec >/dev/null || { printf 'pkexec is required (policykit-1).\n' >&2; return 2; }
    printf 'Ready. Click Sensor Lab.desktop to start.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
