"""Protocol-based AC encoder using IRremoteESP8266 IRac.

Encoding runs through a **shell script** under /tmp — the HA process
never imports irhvac and never forks.  The shell script is called via
:func:`os.system`, which uses ``/bin/sh -c`` exactly like the proven
manual test.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shlex
import tempfile
from pathlib import Path
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)


class IRHVACUnavailableError(ImportError):
    pass


PROTOCOL_MODELS: dict[str, list[dict[str, int | str]]] = {
    "ARGO": [{"value": 1, "label": "SAC_WREM2 (Default)"}, {"value": 2, "label": "SAC_WREM3"}],
    "FUJITSU_AC": [
        {"value": 1, "label": "ARRAH2E (Default)"}, {"value": 2, "label": "ARDB1"},
        {"value": 3, "label": "ARREB1E"}, {"value": 4, "label": "ARJW2"},
        {"value": 5, "label": "ARRY4"}, {"value": 6, "label": "ARREW4E"},
    ],
    "GREE": [{"value": 1, "label": "YAW1F (Default)"}, {"value": 2, "label": "YBOFB"}, {"value": 3, "label": "YX1FSF"}],
    "HAIER_AC176": [{"value": 1, "label": "V9014557_A (Default)"}, {"value": 2, "label": "V9014557_B"}],
    "HITACHI_AC1": [{"value": 1, "label": "R_LT0541_HTA_A (Default)"}, {"value": 2, "label": "R_LT0541_HTA_B"}],
    "KELON168": [{"value": 1, "label": "DG11R201 (Default)"}],
    "LG": [{"value": 1, "label": "GE6711AR2853M (Default)"}, {"value": 2, "label": "AKB75215403"}, {"value": 3, "label": "AKB74955603"}, {"value": 4, "label": "AKB73757604"}, {"value": 5, "label": "LG6711A20083V"}],
    "MIRAGE": [{"value": 1, "label": "KKG9AC1 (Default)"}, {"value": 2, "label": "KKG29AC1"}],
    "PANASONIC_AC": [{"value": 0, "label": "Unknown (Default)"}, {"value": 1, "label": "LKE"}, {"value": 2, "label": "NKE"}, {"value": 3, "label": "DKE / PKR"}, {"value": 4, "label": "JKE"}, {"value": 5, "label": "CKP"}, {"value": 6, "label": "RKR"}],
    "SHARP_AC": [{"value": 1, "label": "A907 (Default)"}, {"value": 2, "label": "A705"}, {"value": 3, "label": "A903 / 820"}],
    "TCL96AC": [{"value": 1, "label": "TAC09CHSD (Default)"}, {"value": 2, "label": "GZ055BE1"}],
    "TOSHIBA_AC": [{"value": 0, "label": "Generic A (Default)"}, {"value": 1, "label": "Generic B"}],
    "VOLTAS": [{"value": 0, "label": "Unknown (Full Function)"}, {"value": 1, "label": "122LZF (Default)"}],
    "WHIRLPOOL_AC": [{"value": 1, "label": "DG11J13A (Default)"}, {"value": 2, "label": "DG11J191"}],
}

_HARDCODED_PROTOCOLS = sorted(PROTOCOL_MODELS.keys())


def _find_native_dir() -> str | None:
    _NATIVE_DIR = Path(__file__).parent.parent / "native"
    machine = platform.machine()
    if machine in ("aarch64", "arm64", "armv8l", "armv8b"):
        arch = "linux_aarch64"
    elif machine in ("x86_64", "amd64", "AMD64"):
        arch = "linux_x86_64"
    else:
        arch = "linux_x86_64"
    for suffix in ("_musl", ""):
        d = _NATIVE_DIR / f"{arch}{suffix}"
        if (d / "irhvac.py").is_file() and (d / "_irhvac.so").is_file():
            return str(d)
    return None


# ---- write shell script + Python script to /tmp (once) -----------------------

_SHELL_SCRIPT: str | None = None
_PYTHON_SCRIPT: str | None = None


def _init_scripts(nd: str) -> tuple[str, str]:
    global _SHELL_SCRIPT, _PYTHON_SCRIPT
    if _SHELL_SCRIPT is not None and os.path.isfile(_SHELL_SCRIPT):
        return _SHELL_SCRIPT, _PYTHON_SCRIPT  # type: ignore[return-value]

    # Python encode script.
    py_code = fr"""
import json, sys
sys.path.insert(0, {shlex.quote(nd)})
import irhvac

infile = sys.argv[1]
outfile = sys.argv[2]
params = json.loads(open(infile).read())

ac = irhvac.IRac(0)
ac.next.protocol = params["protocol"]
ac.next.model = params.get("model", 1)
ac.next.power = params["power"]
if params["power"]:
    ac.next.mode = params["mode"]
    if "degrees" in params:
        ac.next.degrees = params["degrees"]
    if "fanspeed" in params:
        ac.next.fanspeed = params["fanspeed"]
    if "swingv" in params:
        ac.next.swingv = params["swingv"]
    if "swingh" in params:
        ac.next.swingh = params["swingh"]

ac.sendAc()
t = ac.getTiming()
if t is None:
    sys.exit(1)
with open(outfile, "w") as f:
    json.dump(t, f)
"""
    fd, py_path = tempfile.mkstemp(suffix=".py", prefix="hair_enc_", dir="/tmp")
    with os.fdopen(fd, "w") as f:
        f.write(py_code)
    os.chmod(py_path, 0o644)
    _PYTHON_SCRIPT = py_path

    # Shell wrapper — /bin/sh explicitly to match "shell" in manual test.
    # NB: CD into the native dir FIRST so the dynamic linker resolves
    #     _irhvac.so relative to the working directory (matching manual test).
    sh_code = f"""#!/bin/sh
/usr/bin/python3 {shlex.quote(py_path)} "$1" "$2"
"""
    fd, sh_path = tempfile.mkstemp(suffix=".sh", prefix="hair_run_", dir="/tmp")
    with os.fdopen(fd, "w") as f:
        f.write(sh_code)
    os.chmod(sh_path, 0o755)
    _SHELL_SCRIPT = sh_path

    _LOGGER.info("Init encode scripts: py=%s sh=%s", py_path, sh_path)
    return sh_path, py_path


# ---- public API -------------------------------------------------------------

def probe_protocol_encoder() -> tuple[bool, str | None]:
    nd = _find_native_dir()
    if nd is None:
        return False, "No native irhvac directory found"
    return True, None


def is_protocol_encoder_available() -> bool:
    return _find_native_dir() is not None


def get_protocol_models(protocol: str | None = None) -> dict[str, list[dict[str, int | str]]]:
    if protocol is None:
        return dict(PROTOCOL_MODELS)
    key = protocol.upper()
    return {key: PROTOCOL_MODELS[key]} if key in PROTOCOL_MODELS else {key: []}


def get_supported_protocols() -> list[str]:
    return list(_HARDCODED_PROTOCOLS)


def encode(
    device: IRDevice,
    *,
    power: bool = True,
    hvac_mode: str = "auto",
    temperature: float | None = None,
    fan_mode: str | None = None,
    swing_mode: str | None = None,
    **__: Any,
) -> list[int]:
    nd = _find_native_dir()
    if nd is None:
        raise IRHVACUnavailableError("No native irhvac directory found")

    sh_script, py_script = _init_scripts(nd)  # noqa: F841

    # Build params: use hardcoded int values for protocol/mode/fan.
    params: dict[str, Any] = {
        "protocol": _PROTO_VAL.get(device.ir_protocol or "", 15),  # default COOLIX
        "model": device.ir_model or 1,
        "power": power,
    }
    if power:
        params["mode"] = _MODE_VAL.get(hvac_mode.lower(), 0)
        params["degrees"] = round(temperature) if temperature is not None else 24
        if fan_mode:
            params["fanspeed"] = _FAN_VAL.get(fan_mode.lower(), 2)
        if swing_mode and swing_mode != "off":
            if swing_mode in ("vertical", "on", "both"):
                params["swingv"] = 0
            if swing_mode in ("horizontal", "both"):
                params["swingh"] = 0

    # Write params + result to temp files.
    fd1, params_file = tempfile.mkstemp(suffix=".json", prefix="hair_p_", dir="/tmp")
    with os.fdopen(fd1, "w") as f:
        json.dump(params, f)

    fd2, result_file = tempfile.mkstemp(suffix=".json", prefix="hair_r_", dir="/tmp")
    os.close(fd2)

    cmd = f"/bin/sh {shlex.quote(sh_script)} {shlex.quote(params_file)} {shlex.quote(result_file)}"
    _LOGGER.debug("Running: %s", cmd)

    rc = os.system(cmd)

    if rc != 0:
        err = ""
        try:
            if os.path.getsize(result_file) > 0:
                with open(result_file) as f:
                    err = f.read()[:500]
        except Exception:
            pass
        raise RuntimeError(f"Encoder exited {rc}: {err.strip()}")

    try:
        with open(result_file) as f:
            raw: list[int] = json.loads(f.read().strip())
    except Exception as exc:
        raise RuntimeError(f"Encoder result parse error: {exc}")

    # Cleanup.
    for tmp in (params_file, result_file):
        try:
            os.remove(tmp)
        except OSError:
            pass

    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))

    return signed


# ---- hardcoded int values (matches irhvac constants) ------------------------

# Everyone uses the same opmode/fanspeed/swing constants.
# These are taken from the IRremoteESP8266 source.
_PROTO_VAL: dict[str, int] = {
    # From decode_type_t enum in vendor/IRremoteESP8266/src/IRremoteESP8266.h
    "UNKNOWN": -1, "UNUSED": 0, "RC5": 1, "RC6": 2, "NEC": 3, "SONY": 4,
    "PANASONIC": 5, "JVC": 6, "SAMSUNG": 7, "WHYNTER": 8, "AIWA_RC_T501": 9,
    "LG": 10, "SANYO": 11, "MITSUBISHI": 12, "DISH": 13, "SHARP": 14,
    "COOLIX": 15, "DAIKIN": 16, "DENON": 17, "KELVINATOR": 18, "SHERWOOD": 19,
    "MITSUBISHI_AC": 20, "RCMM": 21, "SANYO_LC7461": 22, "RC5X": 23,
    "GREE": 24, "PRONTO": 25, "NEC_LIKE": 26, "ARGO": 27, "TROTEC": 28,
    "NIKAI": 29, "RAW": 30, "GLOBALCACHE": 31, "TOSHIBA_AC": 32,
    "FUJITSU_AC": 33, "MIDEA": 34, "MAGIQUEST": 35, "LASERTAG": 36,
    "CARRIER_AC": 37, "HAIER_AC": 38, "MITSUBISHI2": 39, "HITACHI_AC": 40,
    "HITACHI_AC1": 41, "HITACHI_AC2": 42, "GICABLE": 43,
    "HAIER_AC_YRW02": 44, "WHIRLPOOL_AC": 45, "SAMSUNG_AC": 46,
    "LUTRON": 47, "ELECTRA_AC": 48, "PANASONIC_AC": 49, "PIONEER": 50,
    "LG2": 51, "MWM": 52, "DAIKIN2": 53, "VESTEL_AC": 54, "TECO": 55,
    "SAMSUNG36": 56, "TCL112AC": 57, "LEGOPF": 58, "MITSUBISHI_HEAVY_88": 59,
    "MITSUBISHI_HEAVY_152": 60, "DAIKIN216": 61, "SHARP_AC": 62,
    "GOODWEATHER": 63, "INAX": 64, "DAIKIN160": 65, "NEOCLIMA": 66,
    "DAIKIN176": 67, "DAIKIN128": 68, "AMCOR": 69, "DAIKIN152": 70,
    "DAIKIN64": 71, "DELONGHI_AC": 72, "MULTIBRACKETS": 73, "DAIKIN312": 83,
    "TOSHIBA_AC2": 84, "KELON168": 85, "TCL96AC": 86, "MIDEA24": 87,
    "DAIKIN200": 88, "HAIER_AC176": 89, "VOLTAS": 90, "TRANSCOLD": 91,
    "TECHNIBEL_AC": 92, "MIRAGE": 93, "DAIKIN_AC152": 94, "CORONA_AC": 95,
    "ELITESCREENS": 96, "AIRTON": 97, "AIRWELL": 98, "DELONGHI": 99,
    "DOSHISHA": 100, "EPSON": 101, "SYMPHONY": 102, "GORENJE": 103,
    "KELON": 104, "CARRIER_AC40": 105, "CARRIER_AC64": 106, "CARRIER_AC128": 107,
    "CLIMABUTLER": 108, "BOSCH144": 109, "WOWWEE": 110,
    "CARRIER_AC84": 111, "YORK": 112, "BLUESTARHEAVY": 113, "HITACHI_AC344": 114,
    "HITACHI_AC264": 115, "HITACHI_AC296": 116, "HITACHI_AC424": 117,
    "SAMSUNG_AC2": 118, "XMP": 119, "HAIER_AC160": 120, "TEKNOPOINT": 121,
    "ZEPEAL": 122, "TRUMA": 123, "CORONA": 124, "METZ": 125, "TOTO": 126,
    "ARGO2": 127, "BOSE": 128, "ARRIS": 129, "RHOSS": 130, "EUROM": 131,
}
_MODE_VAL: dict[str, int] = {
    "off": -1, "auto": 0, "cool": 1, "heat": 2, "dry": 3, "fan_only": 4,
}
_FAN_VAL: dict[str, int] = {
    "auto": 0, "min": 1, "low": 2, "medium": 3, "high": 4, "max": 5,
}
