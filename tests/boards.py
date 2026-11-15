# SPDX-License-Identifier: GPL-3.0-or-later
"""Simulated machines for boards other than the reference one, in `fanpilotctl report` format.

Built from the kernel's driver documentation (docs/UNIVERSAL_RESEARCH.md) until real reports arrive;
every real report in tests/reports/ is tested the same way. Values are plausible, not measured.
"""


def _attrs(d: dict, readonly=()) -> dict:
    return {k: {"v": f"{v}\n", "w": k not in readonly and not k.endswith(("_input", "_label", "name"))}
            for k, v in d.items()}


def chip(dir_, name, device, driver, values: dict, readonly=()) -> dict:
    return {"dir": dir_, "device": device, "driver": driver, "attrs": _attrs({"name": name, **values}, readonly)}


def k10temp(dir_="hwmon2"):
    return chip(dir_, "k10temp", "0000:00:18.3", "k10temp",
                {"temp1_input": 52125, "temp1_label": "Tctl", "temp3_input": 47250, "temp3_label": "Tccd1"})


def nct6798(dir_="hwmon4", enable=5, fans=(820, 0, 1150, 610, 0, 0, 0)):
    """ASUS-style Nuvoton: 7 PWM channels, BIOS mode 5 (Smart Fan IV), unlabelled voltages."""
    v = {}
    for i, rpm in enumerate(fans, 1):
        v[f"pwm{i}"] = 153
        v[f"pwm{i}_enable"] = enable
        v[f"pwm{i}_mode"] = 1
        v[f"fan{i}_input"] = rpm
    for i, (lab, t) in enumerate([("SYSTIN", 34000), ("CPUTIN", 41500), ("AUXTIN0", 127000), ("AUXTIN1", -62000),
                                  ("PECI Agent 0 Calibration", 47000)], 1):
        v[f"temp{i}_input"] = t
        v[f"temp{i}_label"] = lab
    for i in range(15):
        v[f"in{i}_input"] = 1000 + i
    return chip(dir_, "nct6798", "nct6775.656", "nct6775", v)


def asusec(dir_="hwmon5"):
    """asus-ec-sensors: read-only by design (no pwm attributes at all)."""
    return chip(dir_, "asusec", "asus-ec-sensors", "asus-ec-sensors",
                {"temp1_input": 44000, "temp1_label": "Chipset", "temp2_input": 41000, "temp2_label": "CPU",
                 "temp3_input": 33000, "temp3_label": "Motherboard", "fan1_input": 2400, "fan1_label": "Chipset"})


def nvme(dir_, dev, temp=38850):
    return chip(dir_, "nvme", dev, None, {"temp1_input": temp, "temp1_label": "Composite", "temp1_crit": 84850})


def asus_amd() -> dict:
    """ASUS AM4 board with nct6798 + asus_ec_sensors, AMD CPU, no GPU sensor."""
    return {"fanpilot_report": 1, "dmi": {"board_vendor": "ASUSTeK COMPUTER INC.", "board_name": "TUF GAMING X570-PLUS"},
            "cpu": "AMD Ryzen 7 5700X3D 8-Core Processor",
            "hwmon": [nvme("hwmon1", "nvme0"), k10temp("hwmon2"), nct6798("hwmon4"), asusec("hwmon5")], "drm": []}


def msi_intel() -> dict:
    """MSI Intel board with the out-of-tree nct6687 (labelled fans), two NVMe drives, no GPU sensor."""
    fans = {}
    for i, (lab, rpm) in enumerate([("CPU Fan", 1320), ("Pump Fan", 0), ("System Fan #1", 900),
                                    ("System Fan #2", 0)], 1):
        fans.update({f"pwm{i}": 128, f"pwm{i}_enable": 2, f"fan{i}_input": rpm, f"fan{i}_label": lab})
    fans.update({"temp1_input": 38000, "temp1_label": "CPU", "temp2_input": 35000, "temp2_label": "System"})
    core = chip("hwmon3", "coretemp", "coretemp.0", "coretemp",
                {"temp1_input": 45000, "temp1_label": "Package id 0", "temp2_input": 43000, "temp2_label": "Core 0",
                 "temp3_input": 44000, "temp3_label": "Core 4"})
    return {"fanpilot_report": 1,
            "dmi": {"board_vendor": "Micro-Star International Co., Ltd.", "board_name": "PRO Z690-A WIFI (MS-7D25)"},
            "cpu": "12th Gen Intel(R) Core(TM) i5-12600K",
            "hwmon": [nvme("hwmon0", "nvme0", 36850), nvme("hwmon1", "nvme1", 41850), core,
                      chip("hwmon5", "nct6687", "nct6687.2592", "nct6687", fans)], "drm": []}


def readonly_board() -> dict:
    """Only read-only chips (e.g. the fan driver is missing): FanPilot must not control anything."""
    return {"fanpilot_report": 1, "dmi": {"board_vendor": "ASUSTeK COMPUTER INC.", "board_name": "ROG STRIX B650E-F"},
            "cpu": "AMD Ryzen 7 7700X 8-Core Processor",
            "hwmon": [k10temp("hwmon1"), asusec("hwmon2"),
                      chip("hwmon3", "nct6683", "nct6683.2592", "nct6683",
                           {"pwm1": 100, "fan1_input": 950, "temp1_input": 40000},
                           readonly=("pwm1",))],
            "drm": [], "platform": [{"name": "nct6683.2592", "hwmon": True}]}


def amdgpu_hwmon(dir_, pci, pwm_writable=True, with_pwm=True, rpm=900):
    v = {"temp1_input": 52000, "temp1_label": "edge", "temp2_input": 61000, "temp2_label": "junction",
         "fan1_input": rpm}
    ro = ()
    if with_pwm:
        v.update({"pwm1": 100, "pwm1_enable": 2})
        ro = () if pwm_writable else ("pwm1", "pwm1_enable")
    return chip(dir_, "amdgpu", pci, "amdgpu", v, readonly=ro)


def amd_card(pci, card, gc_major, fan_ctrl=False):
    attrs = {"vendor": "0x1002\n", "device": "0x73bf\n", f"ip_discovery/die/0/GC/0/major": f"{gc_major}\n"}
    c = {"card": card, "device": pci, "driver": "amdgpu", "attrs": attrs}
    if fan_ctrl:
        c["fan_ctrl"] = {"fan_curve": {"v": "OD_FAN_CURVE:\n0: 0C 0%\n1: 0C 0%\n2: 0C 0%\n3: 0C 0%\n4: 0C 0%\n"
                                            "OD_RANGE:\nFAN_CURVE(hotspot temp): 25C 100C\n"
                                            "FAN_CURVE(fan speed): 20% 100%\n", "w": True},
                         "fan_zero_rpm_enable": {"v": "FAN_ZERO_RPM_ENABLE:\n1\n", "w": True}}
    return c


def rdna2_pc() -> dict:
    """RX 6800-class card (GC 10): pwm1 manual control (beta), no OverDrive curve."""
    r = asus_amd()
    r["hwmon"].append(amdgpu_hwmon("hwmon6", "0000:0b:00.0"))
    r["drm"] = [amd_card("0000:0b:00.0", "card0", 10)]
    return r


def rdna3_no_od_pc() -> dict:
    """RX 7900-class card (GC 11) without OverDrive: pwm1 exists but the firmware ignores it."""
    r = asus_amd()
    r["hwmon"].append(amdgpu_hwmon("hwmon6", "0000:0b:00.0"))
    r["drm"] = [amd_card("0000:0b:00.0", "card0", 11)]
    return r


def two_rdna4_pc() -> dict:
    """Two RX 9000 cards with OverDrive: two firmware curves."""
    r = asus_amd()
    r["hwmon"] += [amdgpu_hwmon("hwmon6", "0000:0b:00.0", with_pwm=False),
                   amdgpu_hwmon("hwmon7", "0000:0e:00.0", with_pwm=False)]
    r["drm"] = [amd_card("0000:0b:00.0", "card0", 12, True), amd_card("0000:0e:00.0", "card1", 12, True)]
    return r
