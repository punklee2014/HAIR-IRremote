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


def _get_irhvac() -> ModuleType:
    global _irhvac
    if _irhvac is None:
        from .loader import load_irhvac

        _irhvac = load_irhvac()
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
    **__: Any,
) -> list[int]:
    """Encode an AC state as raw IR timings (signed microseconds).

    Args:
        device: The IRDevice with ``ir_protocol`` and optional ``ir_model``.
        power: True to turn on, False to turn off.
        hvac_mode: One of ``auto``, ``cool``, ``heat``, ``dry``, ``fan_only``.
        temperature: Target temperature in Celsius (e.g. 24).
        fan_mode: One of ``auto``, ``low``, ``medium``, ``high``.

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
            ac.next.degrees = int(round(temperature))
        if fan_mode and fan_mode in _FAN_MAP:
            ac.next.fanspeed = _FAN_MAP[fan_mode]
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
