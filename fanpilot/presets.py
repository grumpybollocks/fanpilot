# SPDX-License-Identifier: GPL-3.0-or-later
"""One-click presets. They only fill in the curves; everything stays editable."""

BOARD = {
    "Silent": [[45, 25], [60, 35], [72, 55], [82, 80], [88, 100]],
    "Balanced": [[40, 30], [60, 45], [75, 70], [85, 100]],
    "Performance": [[35, 40], [55, 60], [70, 85], [80, 100]],
}

GPU = {
    "Silent": [[50, 20], [65, 30], [78, 50], [88, 75], [95, 100]],
    "Balanced": [[40, 25], [60, 35], [75, 55], [85, 80], [95, 100]],
    "Performance": [[35, 35], [55, 50], [70, 70], [80, 90], [90, 100]],
}


BUILTIN_PROFILES = ("Silent", "Balanced", "Performance")


def builtin_profile(name: str, fan_ids, cpu_sensor="auto:cpu", gpu_sensor="auto:gpu",
                    is_cpu_fan=lambda fid: fid.endswith("/pwm1")) -> dict:
    """Curve settings for every fan. CPU fans follow the CPU; case fans follow the hottest of CPU and
    GPU, so they also help while the graphics card works hard (CPU only on a PC without a GPU sensor)."""
    from .hw import AMD_PWM_RE, PMFW_FAN_RE
    fans = {}
    for fid in fan_ids:
        if PMFW_FAN_RE.match(fid):
            fans[fid] = {"mode": "curve", "curve": GPU[name], "zero_rpm": name != "Performance"}
        elif AMD_PWM_RE.match(fid):
            fans[fid] = {"mode": "auto"}       # older AMD cards (beta) only when you choose so yourself
        elif is_cpu_fan(fid) or not gpu_sensor:
            fans[fid] = {"mode": "curve", "curve": BOARD[name], "source": [cpu_sensor]}
        else:
            fans[fid] = {"mode": "curve", "curve": BOARD[name], "source": [cpu_sensor, gpu_sensor]}
    return fans
