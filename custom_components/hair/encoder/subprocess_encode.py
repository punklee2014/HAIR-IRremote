"""Child-process entry point for protocol-based AC encoding.

This script runs as a subprocess so that loading or using ``_irhvac.so``
never touches the Home Assistant process.

Called with positional args:
    <native_dir> <protocol_name> <model> <mode_str> <degrees>
    [--fan FAN_STR] [--swing SWING_STR] [--off]

All protocol/mode/fan lookups happen inside this process.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _build_map(mod, prefix: str, strip_leading_k: bool = False) -> dict[str, int]:
    """Build a {lowercase_name: int_value} dict from irhvac attributes.

    e.g. prefix="opmode_t_" on `opmode_t_kCool` → key "cool".
    If strip_leading_k=True, strips an extra leading "k" used by mode enums.
    """
    m: dict[str, int] = {}
    for attr in dir(mod):
        if attr.startswith(prefix):
            key = attr[len(prefix):]
            if strip_leading_k and key.startswith("k"):
                key = key[1:]
            if key:
                m[key.lower()] = getattr(mod, attr, 0)
    return m


def main() -> None:
    if len(sys.argv) < 6:
        print("USAGE: ... <native_dir> <protocol> <model> <mode> <degrees> [...]",
              file=sys.stderr)
        sys.exit(2)

    native_dir = Path(sys.argv[1])
    protocol_name = sys.argv[2].upper()
    model = int(sys.argv[3])
    mode_str = sys.argv[4].lower()
    degrees = float(sys.argv[5])

    args = sys.argv[6:]
    power = "--off" not in args
    fan_str = None
    swingv_str = None
    swingh_str = None

    i = 0
    while i < len(args):
        if args[i] == "--fan" and i + 1 < len(args):
            fan_str = args[i + 1]
            i += 2
        elif args[i] == "--swing" and i + 1 < len(args):
            sw = args[i + 1]
            if sw in ("vertical", "on"):
                swingv_str = sw
            elif sw == "horizontal":
                swingh_str = sw
            elif sw == "both":
                swingv_str = "vertical"
                swingh_str = "horizontal"
            i += 2
        else:
            i += 1

    # ---- load irhvac (in subprocess, isolated from HA) --------------------
    sys.path.insert(0, str(native_dir))
    import irhvac

    # Protocol lookup.
    protocols: dict[str, int] = {a.upper(): getattr(irhvac, a)
                                 for a in dir(irhvac)
                                 if a.isupper() and not a.startswith("_")
                                 and isinstance(getattr(irhvac, a), int)}
    if protocol_name not in protocols:
        print(f"ERROR: unknown protocol {protocol_name}", file=sys.stderr)
        sys.exit(1)
    protocol_val = protocols[protocol_name]

    # Mode lookup (skip if power is off — mode is irrelevant).
    mode_val = 0
    if power or mode_str not in ("off",):
        mode_map = _build_map(irhvac, "opmode_t_", strip_leading_k=True)
        if mode_str not in mode_map:
            print(f"ERROR: unknown mode {mode_str}", file=sys.stderr)
            sys.exit(1)
        mode_val = mode_map[mode_str]

    # Fan lookup (also uses k prefix: fanspeed_t_kLow → low).
    fan_map = _build_map(irhvac, "fanspeed_t_", strip_leading_k=True)

    # ---- encode -----------------------------------------------------------
    ac = irhvac.IRac(0)
    ac.next.protocol = protocol_val
    ac.next.model = model
    ac.next.power = power
    if power:
        ac.next.mode = mode_val
        ac.next.degrees = degrees
        if fan_str and fan_str in fan_map:
            ac.next.fanspeed = fan_map[fan_str]
        if swingv_str:
            ac.next.swingv = getattr(irhvac, "swingv_t_kAuto", 0)
        if swingh_str:
            ac.next.swingh = getattr(irhvac, "swingh_t_kAuto", 0)

    ac.sendAc()
    timings = ac.getTiming()
    if timings is None:
        print("ERROR: getTiming() returned None", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(timings))


if __name__ == "__main__":
    main()
