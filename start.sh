#!/usr/bin/env bash
set -Eeuo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd -- "$root"
if ! bash "$root/setup.sh" --check; then
    printf '\n필요한 패키지만 설치합니다. 관리자 암호를 입력하세요.\n'
    if ! sudo bash "$root/setup.sh"; then
        printf '\n설치에 실패했습니다. 위의 오류를 확인하세요. Enter로 종료합니다.\n'
        read -r _ || true
        exit 2
    fi
else
    printf 'SKIP: 필요한 패키지가 모두 설치되어 있습니다.\n'
fi
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    printf '그래픽 데스크톱에서 실행하세요. CLI 사용법은 README.md를 참고하세요.\n' >&2
    exit 2
fi
exec /usr/bin/python3 "$root/desktop_app.py" "$@"
