"""Protocol-based AC encoder using IRremoteESP8266 IRac.

Translates HA climate commands into raw IR timings via the irhvac native
module. The returned ``list[int]`` uses signed microseconds:
positive = mark, negative = space (HA 2026.5+ / infrared-protocols v2.0+).
"""
from __future__ import annotations

import logging
from types import ModuleType
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)

# Cache the loaded irhvac module so we only import once.
_irhvac: ModuleType | None = None

# Protocol name → irhvac protocol constant (set at first use).
_PROTOCOL_MAP: dict[str, int] = {}

# HVAC mode → irhvac opmode_t constant.
_MODE_MAP: dict[str, int] = {}

# Fan mode → irhvac fanspeed_t constant.
_FAN_MAP: dict[str, int] = {}


class IRHVACUnavailableError(ImportError):
    """Raised when the native irhvac module cannot be loaded."""


_encoder_probe: tuple[bool, str | None] | None = None

# ---------------------------------------------------------------------------
# Protocol-to-model lookup table.
# Built from the C++ enums in vendor/IRremoteESP8266/src/IRsend.h.
# Each entry: { "value": int, "label": str }
# "value" 0 always means "Default / Generic".
# ---------------------------------------------------------------------------

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


def probe_protocol_encoder() -> tuple[bool, str | None]:
    """Try loading ``_irhvac.so`` once; cache result for this process."""
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
    """Return True if protocol-based AC encoding can run on this host."""
    return probe_protocol_encoder()[0]


def get_protocol_models(protocol: str | None = None) -> dict[str, list[dict[str, int | str]]]:
    """Return known model enums, optionally filtered to one protocol.

    Each model entry: ``{"value": <int>, "label": "<human name>"}``.
    Returns the full dict when ``protocol`` is None.
    """
    if protocol is None:
        return dict(PROTOCOL_MODELS)
    key = protocol.upper()
    return {key: PROTOCOL_MODELS[key]} if key in PROTOCOL_MODELS else {key: []}


def _is_musl_runtime_error(message: str) -> bool:
    return "linux_aarch64_musl" in message or "musl build" in message


def _get_irhvac() -> ModuleType:
    global _irhvac
    if _irhvac is None:
        from .loader import load_irhvac
        try:
            _irhvac = load_irhvac()
        except ImportError as exc:
            message = str(exc)
            # Common on Alpine/musl systems when loading glibc-linked binaries.
            if "ld-linux-aarch64.so.1" in message or _is_musl_runtime_error(message):
                raise IRHVACUnavailableError(
                    "Protocol encoder needs the musl build "
                    "(native/linux_aarch64_musl/_irhvac.so) on this HA host. "
                    f"{message}"
                ) from exc
            if "PyInit_irhvac" in message:
                raise IRHVACUnavailableError(
                    "The native encoder module (_irhvac.so) is not a valid "
                    "Python extension for this platform "
                    f"(missing PyInit_irhvac). {message}"
                ) from exc
            raise IRHVACUnavailableError(
                "Failed to load protocol encoder native library (irhvac): "
                f"{message}"
            ) from exc
        _build_maps(_irhvac)
    return _irhvac


def _build_maps(irhvac: ModuleType) -> None:
    """Populate protocol, mode, and fan-speed lookup tables."""
    # Protocols — map UPPERCASE name to constant.
    for attr in dir(irhvac):
        if attr.isupper() and not attr.startswith("_"):
            val = getattr(irhvac, attr, None)
            if isinstance(val, int):
                _PROTOCOL_MAP[attr.upper()] = val

    # Operation modes.
    _MODE_MAP["auto"] = getattr(irhvac, "opmode_t_kAuto", 0)
    _MODE_MAP["cool"] = getattr(irhvac, "opmode_t_kCool", 0)
    _MODE_MAP["heat"] = getattr(irhvac, "opmode_t_kHeat", 0)
    _MODE_MAP["dry"] = getattr(irhvac, "opmode_t_kDry", 0)
    _MODE_MAP["fan_only"] = getattr(irhvac, "opmode_t_kFan", 0)

    # Fan speeds.
    _FAN_MAP["auto"] = getattr(irhvac, "fanspeed_t_kAuto", 0)
    _FAN_MAP["low"] = getattr(irhvac, "fanspeed_t_kLow", 0)
    _FAN_MAP["medium"] = getattr(irhvac, "fanspeed_t_kMedium", 0)
    _FAN_MAP["high"] = getattr(irhvac, "fanspeed_t_kHigh", 0)


def get_supported_protocols() -> list[str]:
    """Return protocol names available in the loaded irhvac module."""
    _get_irhvac()
    return sorted(_PROTOCOL_MAP.keys())


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
    """Encode an AC state as raw IR timings (signed microseconds).

    Args:
        device: The IRDevice with ``ir_protocol`` and optional ``ir_model``.
        power: True to turn on, False to turn off.
        hvac_mode: One of ``auto``, ``cool``, ``heat``, ``dry``, ``fan_only``.
        temperature: Target temperature in Celsius (e.g. 24).
        fan_mode: One of ``auto``, ``low``, ``medium``, ``high``.
        swing_mode: One of ``off``, ``vertical``, ``horizontal``, ``both``.

    Returns:
        ``list[int]`` of signed microsecond timings (positive=mark,
        negative=space), compatible with ``RawTimingsCommand`` and
        ``infrared.async_send_command``.

    Raises:
        ValueError: If the protocol is unknown or not set.
        ImportError: If the native module cannot be loaded.
    """
    irhvac = _get_irhvac()

    protocol_name = (device.ir_protocol or "").upper()
    if not protocol_name or protocol_name not in _PROTOCOL_MAP:
        raise ValueError(
            f"Unknown IR protocol '{device.ir_protocol}'. "
            f"Supported: {sorted(_PROTOCOL_MAP.keys())}"
        )

    # Create IRac with a dummy GPIO pin (we only need the timing data).
    ac = irhvac.IRac(0)

    ac.next.protocol = _PROTOCOL_MAP[protocol_name]
    ac.next.model = device.ir_model or 1  # 1 = generic/default model

    if power:
        ac.next.power = 1
        ac.next.mode = _MODE_MAP.get(hvac_mode, _MODE_MAP["auto"])
        if temperature is not None:
            ac.next.degrees = round(temperature)
        if fan_mode and fan_mode in _FAN_MAP:
            ac.next.fanspeed = _FAN_MAP[fan_mode]
        # Swing mapping: off/vertical/horizontal/both.
        if swing_mode:
            if swing_mode in ("vertical", "on"):
                ac.next.swingv = getattr(irhvac, "swingv_t_kAuto", 0)
            elif swing_mode in ("horizontal",):
                ac.next.swingh = getattr(irhvac, "swingh_t_kAuto", 0)
            elif swing_mode in ("both",):
                ac.next.swingv = getattr(irhvac, "swingv_t_kAuto", 0)
                ac.next.swingh = getattr(irhvac, "swingh_t_kAuto", 0)
    else:
        ac.next.power = 0

    ac.sendAc()

    # getTiming() returns all-positive [mark, space, mark, space, ...].
    # Convert to HA signed format: positive=mark, negative=space.
    raw_timings: list[int] = []
    timings = ac.getTiming()
    if timings is None:
        raise RuntimeError("IRac.getTiming() returned None")

    for i, val in enumerate(timings):
        if i % 2 == 0:
            raw_timings.append(int(val))  # mark: positive
        else:
            raw_timings.append(-int(val))  # space: negative

    ac.resetTiming()

    _LOGGER.debug(
        "Encoded %s power=%s mode=%s temp=%s fan=%s → %d timings",
        protocol_name,
        power,
        hvac_mode,
        temperature,
        fan_mode,
        len(raw_timings),
    )
    return raw_timings
