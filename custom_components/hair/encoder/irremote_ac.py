"""Protocol-based AC encoder using IRremoteESP8266 IRac.

Loads ``irhvac`` in-process — same as the proven manual test.  No
subprocess, no HTTP server, no fork.
"""
from __future__ import annotations

import logging
import platform
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)

_irhvac: ModuleType | None = None
_PROTOCOL_MAP: dict[str, int] = {}
_MODE_MAP: dict[str, int] = {}
_FAN_MAP: dict[str, int] = {}


class IRHVACUnavailableError(ImportError):
    """Raised when the native irhvac module cannot be loaded."""


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


def _import_irhvac() -> ModuleType:
    """Import irhvac — matching the proven manual test exactly."""
    global _irhvac
    if _irhvac is not None:
        return _irhvac

    nd = _find_native_dir()
    if nd is None:
        raise IRHVACUnavailableError("No native irhvac directory")
    if nd not in sys.path:
        sys.path.insert(0, nd)

    import irhvac

    _irhvac = irhvac
    _LOGGER.info("Loaded irhvac from %s", nd)

    # Build maps using getattr (not raw ints — SWIG musl wants enum objects).
    _PROTOCOL_MAP.clear()
    for a in dir(irhvac):
        if a.isupper() and not a.startswith("_") and isinstance(getattr(irhvac, a), int):
            _PROTOCOL_MAP[a.upper()] = getattr(irhvac, a)

    _MODE_MAP.clear()
    for name in ("auto", "cool", "heat", "dry", "fan_only"):
        _MODE_MAP[name] = getattr(irhvac, f"opmode_t_k{name.capitalize()}", 0)
    _MODE_MAP["off"] = getattr(irhvac, "opmode_t_kOff", -1)

    _FAN_MAP.clear()
    for name in ("auto", "low", "medium", "high"):
        _FAN_MAP[name] = getattr(irhvac, f"fanspeed_t_k{name.capitalize()}", 0)

    return irhvac


def probe_protocol_encoder() -> tuple[bool, str | None]:
    try:
        _import_irhvac()
        return True, None
    except Exception as exc:
        return False, str(exc)


def is_protocol_encoder_available() -> bool:
    return probe_protocol_encoder()[0]


def get_protocol_models(protocol: str | None = None) -> dict[str, list[dict[str, int | str]]]:
    if protocol is None:
        return dict(PROTOCOL_MODELS)
    key = protocol.upper()
    return {key: PROTOCOL_MODELS[key]} if key in PROTOCOL_MODELS else {key: []}


def get_supported_protocols() -> list[str]:
    try:
        _import_irhvac()
        return sorted(_PROTOCOL_MAP.keys())
    except IRHVACUnavailableError:
        return sorted(PROTOCOL_MODELS.keys())


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
    """Encode an AC state.  Uses getattr for ALL enum values — matching
    the proven manual test approach that avoids SWIG bool/int mismatches."""
    mod = _import_irhvac()

    protocol_name = (device.ir_protocol or "").upper()
    if protocol_name not in _PROTOCOL_MAP:
        raise ValueError(f"Unknown IR protocol '{device.ir_protocol}'")

    ac = mod.IRac(0)
    ac.next.protocol = _PROTOCOL_MAP[protocol_name]
    ac.next.model = device.ir_model or 1

    if power:
        ac.next.power = True
        ac.next.mode = _MODE_MAP.get(hvac_mode, _MODE_MAP["auto"])
        if temperature is not None:
            ac.next.degrees = round(temperature)
        if fan_mode and fan_mode in _FAN_MAP:
            ac.next.fanspeed = _FAN_MAP[fan_mode]
        if swing_mode and swing_mode != "off":
            if swing_mode in ("vertical", "on", "both"):
                ac.next.swingv = getattr(mod, "swingv_t_kAuto", 0)
            if swing_mode in ("horizontal", "both"):
                ac.next.swingh = getattr(mod, "swingh_t_kAuto", 0)
    else:
        ac.next.power = False

    ac.sendAc()
    raw = ac.getTiming()
    if raw is None:
        raise RuntimeError("IRac.getTiming() returned None")

    # Truncate repeats — only one frame.
    hdr = 0
    for i in range(0, len(raw), 2):
        if raw[i] > 2000:
            hdr += 1
            if hdr >= 2:
                raw = raw[:i]
                break
    while raw and raw[-1] > 50000:
        raw.pop()

    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))

    ac.resetTiming()
    _LOGGER.debug("Encoded %s: %d timings", protocol_name, len(signed))
    return signed
