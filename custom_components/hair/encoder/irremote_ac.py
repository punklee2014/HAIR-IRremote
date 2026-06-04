"""Protocol-based AC encoder using IRremoteESP8266 IRac.

All native code runs in a subprocess — the HA process never imports
``irhvac`` / ``_irhvac.so``.  This prevents C++ segfaults (seen on
musl/aarch64) from taking down Home Assistant.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)

# ---- error markers ----------------------------------------------------------

class IRHVACUnavailableError(ImportError):
    """Raised when the native irhvac module cannot be loaded."""


# ---- protocol model table (static, no native dependency) ---------------------

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

# Hardcoded protocol list (subset from IRremoteESP8266 SupportedProtocols.md).
# Used when the native module is unavailable.
_HARDCODED_PROTOCOLS = [
    "ARGO", "COOLIX", "DAIKIN", "DAIKIN128", "DAIKIN152", "DAIKIN160",
    "DAIKIN176", "DAIKIN2", "DAIKIN216", "DAIKIN312", "DAIKIN64",
    "ELECTRA_AC", "FUJITSU_AC", "GOODWEATHER", "GREE", "HAIER_AC",
    "HAIER_AC176", "HAIER_AC_YRW02", "HITACHI_AC", "HITACHI_AC1",
    "HITACHI_AC264", "HITACHI_AC296", "HITACHI_AC344", "HITACHI_AC424",
    "KELVINATOR", "MIDEA", "MITSUBISHI_AC", "MITSUBISHI136",
    "MITSUBISHI152", "MITSUBISHI_AC", "NEOCLIMA", "PANASONIC_AC",
    "SAMSUNG_AC", "SANYO_AC", "SHARP_AC", "TCL96AC", "TECHNIBEL_AC",
    "TOSHIBA_AC", "TRANSCOLD", "TROTEC", "VESTEL_AC", "VOLTAS",
    "WHIRLPOOL_AC",
]


# ---- internal: native directory resolution (no irhvac import) ----------------

def _find_native_dir() -> str | None:
    """Return the native dir containing irhvac.py + _irhvac.so, or None."""
    _NATIVE_DIR = Path(__file__).parent.parent / "native"
    machine = platform.machine()
    if machine in ("aarch64", "arm64", "armv8l", "armv8b"):
        arch = "linux_aarch64"
    elif machine in ("x86_64", "amd64", "AMD64"):
        arch = "linux_x86_64"
    else:
        arch = "linux_x86_64"

    # Prefer musl, fallback glibc.
    for suffix in ("_musl", ""):
        d = _NATIVE_DIR / f"{arch}{suffix}"
        if (d / "irhvac.py").is_file() and (d / "_irhvac.so").is_file():
            return str(d)
    return None


# ---- public API (no irhvac import) -------------------------------------------

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
    nd = _find_native_dir()
    if nd is None:
        return list(_HARDCODED_PROTOCOLS)
    # Try a quick subprocess to list protocols.
    try:
        proc = subprocess.run(
            [
                sys.executable, "-c",
                "import sys; sys.path.insert(0, {!r}); "
                "import irhvac; "
                "print(repr([a for a in dir(irhvac) "
                "if a.isupper() and not a.startswith('_') "
                "and isinstance(getattr(irhvac, a), int)]))".format(nd),
            ],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return sorted(eval(proc.stdout.strip()))
    except Exception:
        pass
    return list(_HARDCODED_PROTOCOLS)


# ---- public: encode (always via subprocess) ----------------------------------

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
    """Encode an AC state as raw IR timings via a child process."""
    nd = _find_native_dir()
    if nd is None:
        raise IRHVACUnavailableError(
            "No native irhvac directory found for this platform"
        )

    script_path = str(Path(__file__).parent / "subprocess_encode.py")
    protocol_name = (device.ir_protocol or "").strip()

    cmd: list[str] = [
        sys.executable, script_path, nd,
        protocol_name,
        str(device.ir_model or 1),
        hvac_mode,
        str(round(temperature) if temperature is not None else 24),
    ]
    if not power:
        cmd.append("--off")
    if fan_mode:
        cmd += ["--fan", fan_mode]
    if swing_mode and swing_mode != "off":
        cmd += ["--swing", swing_mode]

    _LOGGER.info("Subprocess AC encode: %s", " ".join(cmd[2:]))

    try:
        # Clear dangerous env vars that may be inherited from HA and
        # cause the musl .so to load wrong libraries (SIGSEGV).
        clean_env: dict[str, str] = {}
        for k, v in os.environ.items():
            if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH"):
                clean_env[k] = v
        # Use "python3" (system) instead of sys.executable (HA venv may
        # differ from the ABI the .so was compiled against).
        py_exe = shutil.which("python3") or sys.executable

        proc = subprocess.run(
            [py_exe, script_path, *cmd[2:]],
            capture_output=True,
            text=True,
            timeout=15,
            env=clean_env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("AC encoder subprocess timed out (15 s)")

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:300]
        raise RuntimeError(
            f"AC encoder subprocess exited {proc.returncode}: {stderr}"
        )

    try:
        raw: list[int] = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        raise RuntimeError(
            f"AC encoder returned bad JSON: {proc.stdout[:200]}"
        )

    # Convert all-positive [mark, space, ...] to signed.
    signed: list[int] = []
    for i, val in enumerate(raw):
        if i % 2 == 0:
            signed.append(int(val))
        else:
            signed.append(-int(val))

    _LOGGER.debug("Encoded %s: %d timings", protocol_name, len(signed))
    return signed
