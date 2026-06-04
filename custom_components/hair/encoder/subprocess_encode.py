"""Child-process entry point for protocol-based AC encoding.

This script runs as a subprocess so that a C++ segfault inside
``_irhvac.so`` kills only the child, not the Home Assistant process.

Called by ``irremote_ac.encode()`` with:
    python3 -c "..." <native_dir> <protocol> <model> <mode> <degrees> [--fan SPEED] [--swingv V] [--swingh H] [--off]
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 6:
        print("USAGE: subprocess_encode.py <native_dir> <protocol> <model> <mode> <degrees> [...]", file=sys.stderr)
        sys.exit(2)

    native_dir = Path(sys.argv[1])
    protocol_val = int(sys.argv[2])
    model_val = int(sys.argv[3])
    mode_val = int(sys.argv[4])
    degrees = float(sys.argv[5])

    args = sys.argv[6:]
    power = True
    if "--off" in args:
        power = False
        args.remove("--off")

    fan_val = None
    swingv_val = None
    swingh_val = None

    i = 0
    while i < len(args):
        if args[i] == "--fan" and i + 1 < len(args):
            fan_val = int(args[i + 1])
            i += 2
        elif args[i] == "--swingv" and i + 1 < len(args):
            swingv_val = int(args[i + 1])
            i += 2
        elif args[i] == "--swingh" and i + 1 < len(args):
            swingh_val = int(args[i + 1])
            i += 2
        else:
            i += 1

    sys.path.insert(0, str(native_dir))
    import irhvac

    ac = irhvac.IRac(0)
    ac.next.protocol = protocol_val
    ac.next.model = model_val
    ac.next.power = power
    if power:
        ac.next.mode = mode_val
        ac.next.degrees = degrees
        if fan_val is not None:
            ac.next.fanspeed = fan_val
        if swingv_val is not None:
            ac.next.swingv = swingv_val
        if swingh_val is not None:
            ac.next.swingh = swingh_val

    ac.sendAc()
    timings = ac.getTiming()
    if timings is None:
        print("ERROR: getTiming() returned None", file=sys.stderr)
        sys.exit(1)

    # Print as compact JSON list
    import json

    print(json.dumps(timings))


if __name__ == "__main__":
    main()
