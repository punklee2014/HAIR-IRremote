"""Protocol-based AC encoder using IRremoteESP8266 IRac.

Translates HA climate commands into raw IR timings via the irhvac native
module.  The returned ``list[int]`` uses signed microseconds:
positive = mark, negative = space (HA 2026.5+ / infrared-protocols v2.0+).
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)

# Cache the loaded irhvac module (set on first successful import).
_irhvac: ModuleType | None = None

# Protocol / mode / fan-speed maps (populated at first import).
_PROTOCOL_MAP: dict[str, int] = {}
_MODE_MAP: dict[str, int] = {}
_FAN_MAP: dict[str, int] = {}


class IRHVACUnavailableError(ImportError):
    """Raised when the native irhvac module cannot be loaded."""


_encoder_probe: tuple[bool, str | None] | None = None

# ---- protocol model table (same as before) ----------------------------------

PROTOCOL_MODELS: dict[str, list[dict[str, int | str]]] = {
    "ARGO": [
        {"value": 1, "label": "SAC_WREM2 (Default)"},
        {"value": 2, "label": "SAC_WREM3"},
    ],
    "FUJITSU_AC": [
        {"value": 1, "label": "ARRAH2E (Default)"},
        {"value": 2, "label": "ARDB1"},
        {"value": 3, "label": "ARREB1E"},
        {"value": 4, "label": "ARJW2"},
        {"value": 5, "label": "ARRY4"},
        {"value": 6, "label": "ARREW4E"},
    ],
    "GREE": [
        {"value": 1, "label": "YAW1F (Default)"},
        {"value": 2, "label": "YBOFB"},
        {"value": 3, "label": "YX1FSF"},
    ],
    "HAIER_AC176": [
        {"value": 1, "label": "V9014557_A (Default)"},
        {"value": 2, "label": "V9014557_B"},
    ],
    "HITACHI_AC1": [
        {"value": 1, "label": "R_LT0541_HTA_A (Default)"},
        {"value": 2, "label": "R_LT0541_HTA_B"},
    ],
    "KELON168": [
        {"value": 1, "label": "DG11R201 (Default)"},
    ],
    "LG": [
        {"value": 1, "label": "GE6711AR2853M (Default)"},
        {"value": 2, "label": "AKB75215403"},
        {"value": 3, "label": "AKB74955603"},
        {"value": 4, "label": "AKB73757604"},
        {"value": 5, "label": "LG6711A20083V"},
    ],
    "MIRAGE": [
        {"value": 1, "label": "KKG9AC1 (Default)"},
        {"value": 2, "label": "KKG29AC1"},
    ],
    "PANASONIC_AC": [
        {"value": 0, "label": "Unknown (Default)"},
        {"value": 1, "label": "LKE"},
        {"value": 2, "label": "NKE"},
        {"value": 3, "label": "DKE / PKR"},
        {"value": 4, "label": "JKE"},
        {"value": 5, "label": "CKP"},
        {"value": 6, "label": "RKR"},
    ],
    "SHARP_AC": [
        {"value": 1, "label": "A907 (Default)"},
        {"value": 2, "label": "A705"},
        {"value": 3, "label": "A903 / 820"},
    ],
    "TCL96AC": [
        {"value": 1, "label": "TAC09CHSD (Default)"},
        {"value": 2, "label": "GZ055BE1"},
    ],
    "TOSHIBA_AC": [
        {"value": 0, "label": "Generic A (Default)"},
        {"value": 1, "label": "Generic B"},
    ],
    "VOLTAS": [
        {"value": 0, "label": "Unknown (Full Function)"},
        {"value": 1, "label": "122LZF (Default)"},
    ],
    "WHIRLPOOL_AC": [
        {"value": 1, "label": "DG11J13A (Default)"},
        {"value": 2, "label": "DG11J191"},
    ],
}

# ---- public API -------------------------------------------------------------

def probe_protocol_encoder() -> tuple[bool, str | None]:
    global _encoder_probe
    if _encoder_probe is not None:
        return _encoder_probe
    try:
        _get_irhvac()
        _encoder_probe = (True, None)
    except ImportError as exc:
        _encoder_probe = (False, str(exc))
    return _encoder_probe


def is_protocol_encoder_available() -> bool:
    return probe_protocol_encoder()[0]


def get_protocol_models(protocol: str | None = None) -> dict[str, list[dict[str, int | str]]]:
    if protocol is None:
        return dict(PROTOCOL_MODELS)
    key = protocol.upper()
    return {key: PROTOCOL_MODELS[key]} if key in PROTOCOL_MODELS else {key: []}


def get_supported_protocols() -> list[str]:
    _get_irhvac()
    return sorted(_PROTOCOL_MAP.keys())


# ---- internal: load ---------------------------------------------------------

def _get_irhvac() -> ModuleType:
    global _irhvac
    if _irhvac is not None:
        return _irhvac
    from .loader import load_irhvac
    try:
        _irhvac = load_irhvac()
    except ImportError as exc:
        raise IRHVACUnavailableError(str(exc)) from exc
    _build_maps(_irhvac)
    return _irhvac


def _build_maps(irhvac: ModuleType) -> None:
    for attr in dir(irhvac):
        if attr.isupper() and not attr.startswith("_"):
            val = getattr(irhvac, attr, None)
            if isinstance(val, int):
                _PROTOCOL_MAP[attr.upper()] = val

    _MODE_MAP["auto"] = getattr(irhvac, "opmode_t_kAuto", 0)
    _MODE_MAP["cool"] = getattr(irhvac, "opmode_t_kCool", 0)
    _MODE_MAP["heat"] = getattr(irhvac, "opmode_t_kHeat", 0)
    _MODE_MAP["dry"] = getattr(irhvac, "opmode_t_kDry", 0)
    _MODE_MAP["fan_only"] = getattr(irhvac, "opmode_t_kFan", 0)

    _FAN_MAP["auto"] = getattr(irhvac, "fanspeed_t_kAuto", 0)
    _FAN_MAP["low"] = getattr(irhvac, "fanspeed_t_kLow", 0)
    _FAN_MAP["medium"] = getattr(irhvac, "fanspeed_t_kMedium", 0)
    _FAN_MAP["high"] = getattr(irhvac, "fanspeed_t_kHigh", 0)


# ---- internal: encode -------------------------------------------------------

def _native_dir() -> str:
    """Return the path to the native dir that has irhvac.py + _irhvac.so."""
    from .loader import _get_arch_dir, _native_candidates

    arch = _get_arch_dir()
    for p in _native_candidates(arch):
        if (p / "irhvac.py").is_file() and (p / "_irhvac.so").is_file():
            return str(p)
    raise IRHVACUnavailableError("No native irhvac directory found")


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
    """Encode AC state via a child process (crash-safe for musl)."""
    # Pre-load lightweight Python-side only (for protocol names / maps).
    # Actual encoding runs in a subprocess → segfault won't kill HA.
    irhvac = _get_irhvac()

    protocol_name = (device.ir_protocol or "").upper()
    if protocol_name not in _PROTOCOL_MAP:
        raise ValueError(
            f"Unknown IR protocol '{device.ir_protocol}'. "
            f"Supported: {sorted(_PROTOCOL_MAP.keys())}"
        )

    nd = _native_dir()
    script_path = str(Path(__file__).parent / "subprocess_encode.py")

    # Build command-line args.
    cmd: list[str] = [
        sys.executable, script_path, nd,
        str(_PROTOCOL_MAP[protocol_name]),
        str(device.ir_model or 1),
        str(_MODE_MAP.get(hvac_mode, _MODE_MAP["auto"])),
        str(round(temperature) if temperature is not None else 24),
    ]
    if not power:
        cmd.append("--off")
    if fan_mode and fan_mode in _FAN_MAP:
        cmd += ["--fan", str(_FAN_MAP[fan_mode])]
    if swing_mode:
        irhvac = _get_irhvac()  # already have it, reuse
        if swing_mode in ("vertical", "on"):
            cmd += ["--swingv", str(getattr(irhvac, "swingv_t_kAuto", 0))]
        elif swing_mode in ("horizontal",):
            cmd += ["--swingh", str(getattr(irhvac, "swingh_t_kAuto", 0))]
        elif swing_mode in ("both",):
            cmd += [
                "--swingv", str(getattr(irhvac, "swingv_t_kAuto", 0)),
                "--swingh", str(getattr(irhvac, "swingh_t_kAuto", 0)),
            ]

    _LOGGER.info("Subprocess AC encode: %s protocol=%s", " ".join(cmd[2:]), protocol_name)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("AC encoder subprocess timed out (15 s)")

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:300]
        raise RuntimeError(f"AC encoder subprocess exited {proc.returncode}: {stderr}")

    # Parse JSON output.
    try:
        raw: list[int] = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        raise RuntimeError(f"AC encoder returned bad JSON: {proc.stdout[:200]}")

    # Convert all-positive [mark, space, ...] to signed [mark, -space, ...].
    signed: list[int] = []
    for i, val in enumerate(raw):
        if i % 2 == 0:
            signed.append(int(val))
        else:
            signed.append(-int(val))

    _LOGGER.debug("Encoded %s: %d timings", protocol_name, len(signed))
    return signed
