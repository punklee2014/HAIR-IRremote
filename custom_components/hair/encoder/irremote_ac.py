"""Protocol-based AC encoder using IRremoteESP8266 IRac.

All native code runs in a subprocess — the HA process never imports
``irhvac`` / ``_irhvac.so``.  The encode script is written to ``/tmp``
to avoid Docker overlayfs issues with script files under ``/config``.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)


class IRHVACUnavailableError(ImportError):
    """Raised when the native irhvac module cannot be loaded."""


PROTOCOL_MODELS: dict[str, list[dict[str, int | str]]] = {
    "ARGO": [{"value": 1, "label": "SAC_WREM2 (Default)"}, {"value": 2, "label": "SAC_WREM3"}],
    "FUJITSU_AC": [
        {"value": 1, "label": "ARRAH2E (Default)"}, {"value": 2, "label": "ARDB1"},
        {"value": 3, "label": "ARREB1E"}, {"value": 4, "label": "ARJW2"},
        {"value": 5, "label": "ARRY4"}, {"value": 6, "label": "ARREW4E"},
    ],
    "GREE": [
        {"value": 1, "label": "YAW1F (Default)"}, {"value": 2, "label": "YBOFB"},
        {"value": 3, "label": "YX1FSF"},
    ],
    "HAIER_AC176": [{"value": 1, "label": "V9014557_A (Default)"}, {"value": 2, "label": "V9014557_B"}],
    "HITACHI_AC1": [{"value": 1, "label": "R_LT0541_HTA_A (Default)"}, {"value": 2, "label": "R_LT0541_HTA_B"}],
    "KELON168": [{"value": 1, "label": "DG11R201 (Default)"}],
    "LG": [
        {"value": 1, "label": "GE6711AR2853M (Default)"}, {"value": 2, "label": "AKB75215403"},
        {"value": 3, "label": "AKB74955603"}, {"value": 4, "label": "AKB73757604"},
        {"value": 5, "label": "LG6711A20083V"},
    ],
    "MIRAGE": [{"value": 1, "label": "KKG9AC1 (Default)"}, {"value": 2, "label": "KKG29AC1"}],
    "PANASONIC_AC": [
        {"value": 0, "label": "Unknown (Default)"}, {"value": 1, "label": "LKE"},
        {"value": 2, "label": "NKE"}, {"value": 3, "label": "DKE / PKR"},
        {"value": 4, "label": "JKE"}, {"value": 5, "label": "CKP"}, {"value": 6, "label": "RKR"},
    ],
    "SHARP_AC": [
        {"value": 1, "label": "A907 (Default)"}, {"value": 2, "label": "A705"},
        {"value": 3, "label": "A903 / 820"},
    ],
    "TCL96AC": [{"value": 1, "label": "TAC09CHSD (Default)"}, {"value": 2, "label": "GZ055BE1"}],
    "TOSHIBA_AC": [{"value": 0, "label": "Generic A (Default)"}, {"value": 1, "label": "Generic B"}],
    "VOLTAS": [{"value": 0, "label": "Unknown (Full Function)"}, {"value": 1, "label": "122LZF (Default)"}],
    "WHIRLPOOL_AC": [{"value": 1, "label": "DG11J13A (Default)"}, {"value": 2, "label": "DG11J191"}],
}

_HARDCODED_PROTOCOLS = [
    "ARGO", "COOLIX", "DAIKIN", "DAIKIN128", "DAIKIN152", "DAIKIN160",
    "DAIKIN176", "DAIKIN2", "DAIKIN216", "DAIKIN312", "DAIKIN64",
    "ELECTRA_AC", "FUJITSU_AC", "GOODWEATHER", "GREE", "HAIER_AC",
    "HAIER_AC176", "HAIER_AC_YRW02", "HITACHI_AC", "HITACHI_AC1",
    "HITACHI_AC264", "HITACHI_AC296", "HITACHI_AC344", "HITACHI_AC424",
    "KELVINATOR", "MIDEA", "MITSUBISHI_AC", "MITSUBISHI136",
    "MITSUBISHI152", "NEOCLIMA", "PANASONIC_AC",
    "SAMSUNG_AC", "SANYO_AC", "SHARP_AC", "TCL96AC", "TECHNIBEL_AC",
    "TOSHIBA_AC", "TRANSCOLD", "TROTEC", "VESTEL_AC", "VOLTAS",
    "WHIRLPOOL_AC",
]

# ---- one-time: copy subprocess script to /tmp -------------------------------

_ENCODE_SCRIPT: str | None = None

_ENCODE_CODE = r"""
import json, sys
from pathlib import Path

native_dir = Path(sys.argv[1])
sys.path.insert(0, str(native_dir))
import irhvac

protocol_name = sys.argv[2].upper()
model = int(sys.argv[3])
mode_str = sys.argv[4].lower()
degrees = float(sys.argv[5])

args = sys.argv[6:]
power = "--off" not in args
fan_str = None
swingv, swingh = None, None
i = 0
while i < len(args):
    if args[i] == "--fan" and i+1 < len(args):
        fan_str = args[i+1]; i += 2
    elif args[i] == "--swing" and i+1 < len(args):
        sw = args[i+1]
        if sw in ("vertical","on"): swingv = sw
        elif sw == "horizontal": swingh = sw
        elif sw == "both": swingv = "vertical"; swingh = "horizontal"
        i += 2
    else:
        i += 1

def bm(prefix, strip_k=False):
    m = {}
    for a in dir(irhvac):
        if a.startswith(prefix):
            k = a[len(prefix):]
            if strip_k and k.startswith("k"): k = k[1:]
            if k: m[k.lower()] = getattr(irhvac, a, 0)
    return m

protocols = {a.upper(): getattr(irhvac,a) for a in dir(irhvac) if a.isupper() and not a.startswith("_") and isinstance(getattr(irhvac,a), int)}
if protocol_name not in protocols:
    print(f"ERROR: unknown protocol {protocol_name}", file=sys.stderr); sys.exit(1)

mode_val = 0
if power or mode_str != "off":
    mm = bm("opmode_t_", strip_k=True)
    if mode_str not in mm:
        print(f"ERROR: unknown mode {mode_str}", file=sys.stderr); sys.exit(1)
    mode_val = mm[mode_str]

fm = bm("fanspeed_t_", strip_k=True)

ac = irhvac.IRac(0)
ac.next.protocol = protocols[protocol_name]
ac.next.model = model
ac.next.power = power
if power:
    ac.next.mode = mode_val
    ac.next.degrees = degrees
    if fan_str and fan_str in fm:
        ac.next.fanspeed = fm[fan_str]
    if swingv:
        ac.next.swingv = getattr(irhvac, "swingv_t_kAuto", 0)
    if swingh:
        ac.next.swingh = getattr(irhvac, "swingh_t_kAuto", 0)

ac.sendAc()
t = ac.getTiming()
if t is None:
    print("ERROR: getTiming() returned None", file=sys.stderr); sys.exit(1)
print(json.dumps(t))
"""


def _get_encode_script() -> str:
    """Return path to a tempfile containing the encode script.

    Written once to /tmp to avoid Docker overlayfs issues.
    """
    global _ENCODE_SCRIPT
    if _ENCODE_SCRIPT is not None and os.path.isfile(_ENCODE_SCRIPT):
        return _ENCODE_SCRIPT
    fd, path = tempfile.mkstemp(suffix=".py", prefix="hair_encode_", dir="/tmp")
    with os.fdopen(fd, "w") as f:
        f.write(_ENCODE_CODE)
    os.chmod(path, 0o755)
    _ENCODE_SCRIPT = path
    _LOGGER.info("Wrote encode script to %s", path)
    return path


# ---- public API -------------------------------------------------------------

def _find_native_dir() -> str | None:
    nd_root = Path(__file__).parent.parent / "native"
    machine = platform.machine()
    if machine in ("aarch64", "arm64", "armv8l", "armv8b"):
        arch = "linux_aarch64"
    elif machine in ("x86_64", "amd64", "AMD64"):
        arch = "linux_x86_64"
    else:
        arch = "linux_x86_64"
    for suffix in ("_musl", ""):
        d = nd_root / f"{arch}{suffix}"
        if (d / "irhvac.py").is_file() and (d / "_irhvac.so").is_file():
            return str(d)
    return None


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

    script = _get_encode_script()
    degrees = round(temperature) if temperature is not None else 24

    args = [
        sys.executable, script, nd,
        (device.ir_protocol or "").upper(),
        str(device.ir_model or 1),
        hvac_mode,
        str(degrees),
    ]
    if not power:
        args.append("--off")
    if fan_mode:
        args += ["--fan", fan_mode]
    if swing_mode and swing_mode != "off":
        args += ["--swing", swing_mode]

    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH")
    }

    _LOGGER.debug("Subprocess: %s", " ".join(args[3:]))

    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=15, env=clean_env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("AC encoder timed out")

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:500]
        out = (proc.stdout or "").strip()[:200]
        raise RuntimeError(
            f"AC encoder exited {proc.returncode}: stderr={err} stdout={out}"
        )

    try:
        raw: list[int] = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        raise RuntimeError(f"AC encoder bad JSON: {proc.stdout[:200]}")

    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))

    return signed
