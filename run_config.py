"""Shared limits and release identity for desktop and command-line runners."""
VERSION = '1.1.0'
MAX_DURATION_SECONDS = 7 * 24 * 60 * 60
PRESET_MINUTES = (30, 60, 90, 120, 150, 180)
DURATION_OPTIONS = ('30분', '1시간', '1시간 30분', '2시간', '2시간 30분', '3시간', '직접 입력')


def selected_duration(choice, custom_minutes=''):
    if choice not in DURATION_OPTIONS:
        raise ValueError('검사 시간을 선택하세요.')
    if choice != DURATION_OPTIONS[-1]:
        return PRESET_MINUTES[DURATION_OPTIONS.index(choice)] * 60
    try:
        minutes = int(custom_minutes)
    except (ValueError, TypeError):
        raise ValueError('직접 입력 시간은 정수 분으로 입력하세요.') from None
    if not 181 <= minutes <= MAX_DURATION_SECONDS // 60:
        raise ValueError('직접 입력 시간은 181~10080분(최대 7일)입니다.')
    return minutes * 60


def stage_durations(mode, duration):
    """Split the selected total time; preparation/report overhead is additional."""
    if mode != 'auto':
        return [duration]
    baseline = min(60, duration - 30)
    load, extra = divmod(duration - baseline, 3)
    return [baseline] + [load + (index < extra) for index in range(3)]
