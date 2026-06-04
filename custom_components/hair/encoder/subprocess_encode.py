"""Child-process entry point for protocol-based AC encoding.

Always called from ``irremote_ac.py`` with TWO positional args,
then reads JSON params from STDIN.  This keeps the command-line
invocation simple while allowing unlimited protocol parameters.

    python3 subprocess_encode.py <native_dir> --stdin  < params.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _build_map(mod, prefix: str, strip_leading_k: bool = False) -> dict[str, int]:
    m: dict[str, int] = {}
    for attr in dir(mod):
        if attr.startswith(prefix):
            key = attr[len(prefix):]
            if strip_leading_k and key.startswith("k"):
                key = key[1:]
            if key:
                m[key.lower()] = getattr(mod, attr)
    return m


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[2] != "--stdin":
        print("Usage: subprocess_encode.py <native_dir> --stdin  < params.json",
              file=sys.stderr)
        sys.exit(2)

    native_dir = Path(sys.argv[1])

    # Read all protocol parameters from stdin (flexible JSON).
    try:
        params = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        print(f"ERROR: bad JSON from stdin: {exc}", file=sys.stderr)
        sys.exit(1)

    protocol_name = params.get("protocol", "COOLIX").upper()
    model = int(params.get("model", 1))
    power = bool(params.get("power", True))
    mode_str = str(params.get("mode", "auto")).lower()
    degrees = int(round(float(params.get("degrees", 24))))
    fan_str = params.get("fanspeed")
    swingv_str = params.get("swingv")
    swingh_str = params.get("swingh")

    # Purge stale module cache.
    sys.modules.pop("irhvac", None)
    sys.modules.pop("_irhvac", None)

    sys.path.insert(0, str(native_dir))
    import irhvac

    # Protocol lookup — getattr values only.
    protocols: dict[str, int] = {
        a.upper(): getattr(irhvac, a)
        for a in dir(irhvac)
        if a.isupper() and not a.startswith("_") and isinstance(getattr(irhvac, a), int)
    }
    if protocol_name not in protocols:
        print(f"ERROR: unknown protocol {protocol_name}", file=sys.stderr)
        sys.exit(1)
    protocol_val = protocols[protocol_name]

    # Mode lookup.
    mode_map = _build_map(irhvac, "opmode_t_", strip_leading_k=True)
    mode_val = getattr(irhvac, "opmode_t_kOff", -1)
    if power and mode_str != "off":
        if mode_str not in mode_map:
            print(f"ERROR: unknown mode {mode_str}", file=sys.stderr)
            sys.exit(1)
        mode_val = mode_map[mode_str]

    # Fan lookup.
    fan_map = _build_map(irhvac, "fanspeed_t_", strip_leading_k=True)

    # Encode.
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
            ac.next.swingv = getattr(irhvac, "swingv_t_kAuto",
                                     getattr(irhvac, "swingv_t_kOff", -1))
        if swingh_str:
            ac.next.swingh = getattr(irhvac, "swingh_t_kAuto",
                                     getattr(irhvac, "swingh_t_kOff", -1))

    # Debug log.
    print(f"[SUBENCODE] proto={protocol_name} model={model} power={power} "
          f"mode={mode_val} degrees={degrees} fan={fan_str} "
          f"swingv={swingv_str} swingh={swingh_str}",
          file=sys.stderr, flush=True)

    try:
        ac.sendAc()
    except Exception as exc:
        print(f"ERROR inside sendAc(): {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    t = ac.getTiming()
    if not t:
        print("ERROR: getTiming() returned None or empty", file=sys.stderr)
        sys.exit(1)
    if len(t) < 4:
        print(json.dumps(t))
        return

    # Trim to a single frame.
    hdr = 0
    for i in range(0, len(t), 2):
        if t[i] > 2000:
            hdr += 1
            if hdr >= 2:
                t = t[:i]
                break
    while t and t[-1] > 50000:
        t.pop()

    print(json.dumps(t))


if __name__ == "__main__":
    main()
