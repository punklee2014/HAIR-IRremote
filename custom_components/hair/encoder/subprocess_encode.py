"""Child-process entry point for protocol-based AC encoding.

Two modes:

1. One-shot (default) — positional argv, prints JSON to stdout, exits::

    python3 subprocess_encode.py <nd> <proto> <model> <mode> <degrees> [--fan F] [--swing S] [--off]

2. Daemon (--daemon) — long-lived, stdin/stdout JSON line protocol::

    python3 subprocess_encode.py --daemon <nd>

   Request (stdin, one JSON line):

     {"proto":"FUJITSU_AC","model":1,"mode":"cool","degrees":24,"power":true}

   Success (stdout, one JSON line):

     [123,-456,789,-1011]

   Error (stdout, one JSON line):

     {"err":"unknown protocol XYZ"}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


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


def _trim_timing(t: list[int]) -> list[int]:
    """Trim to a single IR frame."""
    if len(t) < 4:
        return t
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


def daemon_main(native_dir: str) -> None:
    """Long-lived daemon: read JSON requests from stdin, write JSON results to stdout."""
    sys.path.insert(0, native_dir)
    import irhvac

    # ── one-time lookups ─────────────────────────────────────────────────
    protocols: dict[str, int] = {
        a.upper(): getattr(irhvac, a)
        for a in dir(irhvac)
        if a.isupper() and not a.startswith("_") and isinstance(getattr(irhvac, a), int)
    }
    mode_map = _build_map(irhvac, "opmode_t_", strip_leading_k=True)
    mode_off = getattr(irhvac, "opmode_t_kOff", -1)
    fan_map = _build_map(irhvac, "fanspeed_t_", strip_leading_k=True)
    swingv_auto = getattr(irhvac, "swingv_t_kAuto", getattr(irhvac, "swingv_t_kOff", -1))
    swingh_auto = getattr(irhvac, "swingh_t_kAuto", getattr(irhvac, "swingh_t_kOff", -1))

    def _respond(payload: Any) -> None:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            _respond({"err": f"bad json: {exc}"})
            continue

        proto_name = str(req.get("proto", "")).upper()
        model = int(req.get("model", 1))
        mode_str = str(req.get("mode", "auto")).lower()
        degrees = int(round(float(req.get("degrees", 24))))
        power = bool(req.get("power", True))
        fan_str = str(req.get("fan", "")).lower() if req.get("fan") else ""
        swingv_str = str(req.get("swingv", "")).lower() if req.get("swingv") else ""
        swingh_str = str(req.get("swingh", "")).lower() if req.get("swingh") else ""

        if proto_name not in protocols:
            _respond({"err": f"unknown protocol {proto_name}"})
            continue

        mode_val = mode_off
        if power and mode_str != "off":
            mode_val = mode_map.get(mode_str, mode_map.get("auto", 0))

        ac = irhvac.IRac(0)
        ac.next.protocol = protocols[proto_name]
        ac.next.model = model
        ac.next.power = power
        if power:
            ac.next.mode = mode_val
            ac.next.degrees = degrees
            if fan_str and fan_str in fan_map:
                ac.next.fanspeed = fan_map[fan_str]
            if swingv_str:
                ac.next.swingv = swingv_auto
            if swingh_str:
                ac.next.swingh = swingh_auto

        print(
            f"[SUBENCODE] proto={proto_name} model={model} power={power} "
            f"mode={mode_val} degrees={degrees} fan={fan_str} "
            f"sv={swingv_str} sh={swingh_str}",
            file=sys.stderr, flush=True,
        )

        try:
            ac.sendAc()
        except Exception as exc:
            _respond({"err": f"sendAc() failed: {exc}"})
            continue

        t = ac.getTiming()
        if not t:
            _respond({"err": "getTiming() returned None/empty"})
            continue

        _respond(_trim_timing(t))

def oneshot_main() -> None:
    """One-shot mode: positional argv, print JSON, exit."""
    if len(sys.argv) < 6:
        print(
            "Usage: subprocess_encode.py <nd> <proto> <model> <mode> <degrees> [...]",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.modules.pop("irhvac", None)
    sys.modules.pop("_irhvac", None)

    native_dir = str(Path(sys.argv[1]))
    protocol_name = sys.argv[2].upper()
    model = int(sys.argv[3])
    mode_str = sys.argv[4].lower()
    degrees = int(round(float(sys.argv[5])))

    flags = sys.argv[6:]
    power = "--off" not in flags
    fan_str: str | None = None
    swingv_str: str | None = None
    swingh_str: str | None = None

    i = 0
    while i < len(flags):
        if flags[i] == "--fan" and i + 1 < len(flags):
            fan_str = flags[i + 1]
            i += 2
        elif flags[i] == "--swing" and i + 1 < len(flags):
            sw = flags[i + 1]
            if sw in ("vertical", "on"):
                swingv_str = sw
            elif sw == "horizontal":
                swingh_str = sw
            elif sw == "both":
                swingv_str = "vertical"
                swingh_str = "horizontal"
            i += 2
        elif flags[i] == "--off":
            i += 1
        else:
            i += 1

    sys.path.insert(0, native_dir)
    import irhvac

    protocols: dict[str, int] = {
        a.upper(): getattr(irhvac, a)
        for a in dir(irhvac)
        if a.isupper() and not a.startswith("_") and isinstance(getattr(irhvac, a), int)
    }
    if protocol_name not in protocols:
        print(f"ERROR: unknown protocol {protocol_name}", file=sys.stderr)
        sys.exit(1)

    mode_map = _build_map(irhvac, "opmode_t_", strip_leading_k=True)
    mode_val = getattr(irhvac, "opmode_t_kOff", -1)
    if power and mode_str != "off":
        mode_val = mode_map.get(mode_str, mode_map.get("auto", 0))

    fan_map = _build_map(irhvac, "fanspeed_t_", strip_leading_k=True)

    ac = irhvac.IRac(0)
    ac.next.protocol = protocols[protocol_name]
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

    print(f"[SUBENCODE] proto={protocol_name} model={model} power={power} "
          f"mode={mode_val} degrees={degrees} fan={fan_str} "
          f"sv={swingv_str} sh={swingh_str}",
          file=sys.stderr, flush=True)

    try:
        ac.sendAc()
    except Exception as exc:
        print(f"ERROR inside sendAc(): {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    t = ac.getTiming()
    if not t:
        print("ERROR: getTiming() returned None/empty", file=sys.stderr)
        sys.exit(1)

    t = _trim_timing(t)
    print(json.dumps(t))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--daemon":
        daemon_main(sys.argv[2])
    else:
        oneshot_main()