"""Protocol-based AC encoder using IRremoteESP8266 IRac.

Direct import — irhvac is loaded in-process on first encode call.
No subprocess, no JSON serialization overhead.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import sys
from pathlib import Path
from types import ModuleType
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

    # Pick versioned subdir matching runtime Python ABI (e.g. py314, py313, py312).
    py_ver = f"py{sys.version_info.major}{sys.version_info.minor}"

    for suffix in ("_musl", ""):
        arch_dir = nd_root / f"{arch}{suffix}"
        # Prefer version-tagged subdirectory (multi-ABI release layout).
        d = arch_dir / py_ver
        if (d / "irhvac.py").is_file() and (d / "_irhvac.so").is_file():
            return str(d)
        # Fall back: flat layout (single-ABI, v0.3.0 and earlier).
        if (arch_dir / "irhvac.py").is_file() and (arch_dir / "_irhvac.so").is_file():
            return str(arch_dir)
    return None


# ── lazy irhvac import & lookup tables ────────────────────────────────────────

_IRHVAC: ModuleType | None = None
_protocols: dict[str, int] = {}
_mode_map: dict[str, int] = {}
_mode_off: int = -1
_fan_map: dict[str, int] = {}
_swingv_auto: int = -1
_swingh_auto: int = -1
_load_attempted: bool = False


def _build_map(mod: ModuleType, prefix: str, strip_leading_k: bool = False) -> dict[str, int]:
    m: dict[str, int] = {}
    for attr in dir(mod):
        if attr.startswith(prefix):
            key = attr[len(prefix):]
            if strip_leading_k and key.startswith("k"):
                key = key[1:]
            if key:
                m[key.lower()] = getattr(mod, attr)
    return m


def _ensure_loaded() -> ModuleType:
    """Lazy-load irhvac on first call. Raises ImportError/RuntimeError on failure."""
    global _IRHVAC, _protocols, _mode_map, _mode_off, _fan_map
    global _swingv_auto, _swingh_auto, _load_attempted

    if _IRHVAC is not None:
        return _IRHVAC

    nd = _find_native_dir()
    if nd is None:
        _load_attempted = True
        raise ImportError("No native irhvac directory found — .so not installed")

    if nd not in sys.path:
        sys.path.insert(0, nd)

    import irhvac

    # Build lookup tables (once).
    _protocols = {
        a.upper(): getattr(irhvac, a)
        for a in dir(irhvac)
        if a.isupper() and not a.startswith("_") and isinstance(getattr(irhvac, a), int)
    }
    _mode_map = _build_map(irhvac, "opmode_t_", strip_leading_k=True)
    _mode_off = getattr(irhvac, "opmode_t_kOff", -1)
    _fan_map = _build_map(irhvac, "fanspeed_t_", strip_leading_k=True)
    _swingv_auto = getattr(irhvac, "swingv_t_kAuto", getattr(irhvac, "swingv_t_kOff", -1))
    _swingh_auto = getattr(irhvac, "swingh_t_kAuto", getattr(irhvac, "swingh_t_kOff", -1))

    _IRHVAC = irhvac
    _LOGGER.info("irhvac loaded in-process: %d protocols", len(_protocols))
    return irhvac


# ── public API ────────────────────────────────────────────────────────────────


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


async def encode(
    device: IRDevice,
    *,
    power: bool = True,
    hvac_mode: str = "auto",
    temperature: float | None = None,
    fan_mode: str | None = None,
    swing_mode: str | None = None,
    **__: Any,
) -> list[int]:
    """Encode an AC command via direct irhvac call (in-process, no subprocess)."""

    loop = asyncio.get_running_loop()

    def _do_encode() -> list[int]:
        irhvac = _ensure_loaded()

        proto_name = (device.ir_protocol or "COOLIX").upper()
        model = int(device.ir_model or 1)
        mode_str = str(hvac_mode).lower()
        degrees = int(round(float(temperature if temperature is not None else 24)))
        fan_str = str(fan_mode).lower() if fan_mode else ""
        swingv_str = str(swing_mode).lower() if swing_mode and swing_mode != "off" else ""
        swingh_str = swingv_str if swingv_str else ""

        if proto_name not in _protocols:
            raise ValueError(f"Unknown protocol: {proto_name}")

        mode_val = _mode_off
        if power and mode_str != "off":
            mode_val = _mode_map.get(mode_str, _mode_map.get("auto", 0))

        ac = irhvac.IRac(0)
        ac.next.protocol = _protocols[proto_name]
        ac.next.model = model
        ac.next.power = power
        if power:
            ac.next.mode = mode_val
            ac.next.degrees = degrees
            if fan_str and fan_str in _fan_map:
                ac.next.fanspeed = _fan_map[fan_str]
            if swingv_str:
                ac.next.swingv = _swingv_auto
            if swingh_str:
                ac.next.swingh = _swingh_auto

        _LOGGER.debug(
            "Encoding: proto=%s model=%s power=%s mode=%s degrees=%s fan=%s sv=%s sh=%s",
            proto_name, model, power, mode_val, degrees, fan_str, swingv_str, swingh_str,
        )

        ac.sendAc()
        t = ac.getTiming()
        if not t:
            raise RuntimeError("getTiming() returned None/empty")

        # Trim to single IR frame (same logic as subprocess_encode.py _trim_timing).
        t = list(t)
        if len(t) >= 4:
            hdr = 0
            for i in range(0, len(t), 2):
                if t[i] > 2000:
                    hdr += 1
                    if hdr >= 2:
                        t = t[:i]
                        break
        while t and t[-1] > 50000:
            t.pop()

        return t

    raw = await loop.run_in_executor(None, _do_encode)

    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))

    return signed