"""Protocol-based AC encoder using IRremoteESP8266 IRac.

Each encode runs as a one-shot ``python3`` subprocess — reads JSON params
from stdin, imports ``irhvac``, encodes, prints JSON timings to stdout.
Zero in-process C loading, zero daemon, zero port.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)


class IRHVACUnavailableError(ImportError):
    pass


PROTOCOL_MODELS: dict[str, list[dict[str, int | str]]] = {
    "ARGO": [{"value": 1, "label": "SAC_WREM2 (Default)"}, {"value": 2, "label": "SAC_WREM3"}],
    "FUJITSU_AC": [{"value": 1, "label": "ARRAH2E (Default)"}, {"value": 2, "label": "ARDB1"}, {"value": 3, "label": "ARREB1E"}, {"value": 4, "label": "ARJW2"}, {"value": 5, "label": "ARRY4"}, {"value": 6, "label": "ARREW4E"}],
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


def _find_native_dir() -> str | None:
    nd_root = Path(__file__).parent.parent / "native"
    machine = platform.machine()
    if machine in ("aarch64", "arm64", "armv8l", "armv8b"):
        arch = "linux_aarch64"
    else:
        arch = "linux_x86_64"
    for suffix in ("_musl", ""):
        d = nd_root / f"{arch}{suffix}"
        if (d / "irhvac.py").is_file() and (d / "_irhvac.so").is_file():
            return str(d)
    return None


def probe_protocol_encoder() -> tuple[bool, str | None]:
    if _find_native_dir() is None:
        return False, "No native irhvac directory"
    return True, None


def is_protocol_encoder_available() -> bool:
    return probe_protocol_encoder()[0]


def get_protocol_models(protocol: str | None = None) -> dict[str, list[dict[str, int | str]]]:
    if protocol is None:
        return dict(PROTOCOL_MODELS)
    return {protocol.upper(): PROTOCOL_MODELS.get(protocol.upper(), [])}


def get_supported_protocols() -> list[str]:
    return sorted(PROTOCOL_MODELS.keys())


# ── one-shot subprocess encoder ──────────────────────────────────────────────

# Inline Python 3 script that reads JSON params from stdin, imports irhvac,
# calls sendAc(), and prints JSON timings to stdout.  This script is passed
# as ``-c`` to ``python3`` (the system interpreter), never run in the HA
# process.
_ENCODE_INLINE = r"""
import json, sys
native_dir = sys.argv[1]
sys.path.insert(0, native_dir)
import irhvac

p = json.loads(sys.stdin.read())

def _attrs(prefix, strip_k=False):
    m = {}
    for a in dir(irhvac):
        if a.startswith(prefix):
            k = a[len(prefix):]
            if strip_k and k.startswith("k"):
                k = k[1:]
            if k:
                m[k.lower()] = getattr(irhvac, a)
    return m

# protocol: uses decode_type_t constants (all UPPER + not starting with _)
protocols = {
    a.upper(): getattr(irhvac, a)
    for a in dir(irhvac)
    if a.isupper() and not a.startswith("_") and isinstance(getattr(irhvac, a), int)
}
proto_name = p.get("protocol", "COOLIX").upper()
if proto_name not in protocols:
    print(f"ERROR: unknown protocol {proto_name}", file=sys.stderr); sys.exit(1)

modes = _attrs("opmode_t_", strip_k=True)
fans  = _attrs("fanspeed_t_", strip_k=True)

ac = irhvac.IRac(0)
ac.next.protocol = protocols[proto_name]
ac.next.model       = int(p.get("model", 1))
ac.next.power       = bool(p.get("power", True))
if ac.next.power:
    ac.next.mode    = modes.get(p.get("mode", "auto"), 0)
    ac.next.degrees  = float(p.get("degrees", 24))
    fs = p.get("fanspeed")
    if fs:
        ac.next.fanspeed = fans.get(str(fs).lower(), 0)
    sv = p.get("swingv")
    if sv is not None:
        ac.next.swingv = int(sv)
    sh = p.get("swingh")
    if sh is not None:
        ac.next.swingh = int(sh)

ac.sendAc()
t = ac.getTiming()
if t is None:
    print("ERROR: getTiming returned None", file=sys.stderr); sys.exit(1)

# truncate to one frame
hdr = 0
for i in range(0, len(t), 2):
    if t[i] > 2000:
        hdr += 1
        if hdr >= 2:
            t = t[:i]; break
while t and t[-1] > 50000:
    t.pop()

print(json.dumps(t))
"""


def _encode_subprocess(native_dir: str, params: dict[str, Any]) -> list[int]:
    """Run the inline encoder via system ``python3``.

    Reads JSON params from stdin, writes JSON timings to stdout.
    If the subprocess crashes the HA process is unaffected.
    """
    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH")
    }

    try:
        proc = subprocess.run(
            ["python3", "-c", _ENCODE_INLINE, native_dir],
            input=json.dumps(params),
            capture_output=True,
            text=True,
            timeout=10,
            env=clean_env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("AC encoder subprocess timed out")
    except FileNotFoundError:
        raise RuntimeError("System python3 not found — is it installed?")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(f"Encoder subprocess exited {proc.returncode}: {err}")

    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        raise RuntimeError(f"Encoder returned bad JSON: {proc.stdout[:200]}")


# ── public encode ────────────────────────────────────────────────────────────

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
    """Encode AC state via a stateless ``python3`` subprocess.

    The subprocess imports ``irhvac`` and does ALL protocol/mode/fan
    lookups.  No native code ever runs inside HA.
    """
    nd = _find_native_dir()
    if nd is None:
        raise IRHVACUnavailableError("No native irhvac directory")

    params: dict[str, Any] = {
        "protocol": (device.ir_protocol or "COOLIX").upper(),
        "model": device.ir_model or 1,
        "power": bool(power),
    }
    if power:
        params["mode"] = hvac_mode
        params["degrees"] = round(temperature) if temperature is not None else 24
        if fan_mode:
            params["fanspeed"] = fan_mode
        if swing_mode and swing_mode != "off":
            if swing_mode in ("vertical", "on", "both"):
                params["swingv"] = 0
            if swing_mode in ("horizontal", "both"):
                params["swingh"] = 0

    _LOGGER.debug("Subprocess encode: %s", params)

    raw = _encode_subprocess(nd, params)

    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))

    return signed
