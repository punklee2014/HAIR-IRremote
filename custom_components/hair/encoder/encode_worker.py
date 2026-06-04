"""One-shot worker for AC protocol encoding.

Called by HA via subprocess::

    python3 encode_worker.py <native_dir> '<json_params>'

Writes JSON list[int] timings to stdout on success.  All irhvac lookups
happen in this process — matched to the proven manual test.
"""
import json
import sys


def _attrs(mod, prefix: str, strip_k: bool = False) -> dict[str, int]:
    m: dict[str, int] = {}
    for a in dir(mod):
        if a.startswith(prefix):
            k = a[len(prefix):]
            if strip_k and k.startswith("k"):
                k = k[1:]
            if k:
                m[k.lower()] = getattr(mod, a)
    return m


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: encode_worker.py <native_dir> <json_params>", file=sys.stderr)
        return 2

    native_dir = sys.argv[1]
    sys.path.insert(0, native_dir)

    # Purge any stale cached module from previous runs.
    sys.modules.pop("irhvac", None)
    sys.modules.pop("_irhvac", None)

    try:
        import irhvac  # noqa: E402
    except ImportError as exc:
        print(f"ERROR: cannot import irhvac: {exc}", file=sys.stderr)
        return 1

    try:
        p = json.loads(sys.argv[2])
    except json.JSONDecodeError as exc:
        print(f"ERROR: bad JSON: {exc}", file=sys.stderr)
        return 1

    # ---- protocol map --------------------------------------------------------
    protocols = {
        a.upper(): getattr(irhvac, a)
        for a in dir(irhvac)
        if a.isupper() and not a.startswith("_") and isinstance(getattr(irhvac, a), int)
    }
    proto_name = p.get("protocol", "COOLIX").upper()
    if proto_name not in protocols:
        print(f"ERROR: unknown protocol {proto_name}", file=sys.stderr)
        return 1

    # ---- mode / fan maps ----------------------------------------------------
    modes = _attrs(irhvac, "opmode_t_", strip_k=True)
    fans  = _attrs(irhvac, "fanspeed_t_", strip_k=True)

    # ---- encode --------------------------------------------------------------
    ac = irhvac.IRac(0)
    ac.next.protocol = protocols[proto_name]
    ac.next.model    = int(p.get("model", 1))
    ac.next.power    = bool(p.get("power", True))

    if ac.next.power:
        mode_str = str(p.get("mode", "auto")).lower()
        ac.next.mode = modes.get(mode_str,
                                 getattr(irhvac, "opmode_t_kAuto", 0))
        ac.next.degrees = int(round(float(p.get("degrees", 24))))
        fs = p.get("fanspeed")
        if fs:
            ac.next.fanspeed = fans.get(str(fs).lower(),
                                        getattr(irhvac, "fanspeed_t_kAuto", 0))
        sv = p.get("swingv")
        if sv is not None:
            ac.next.swingv = getattr(irhvac, "swingv_t_kAuto",
                                     getattr(irhvac, "swingv_t_kOff", -1))
        sh = p.get("swingh")
        if sh is not None:
            ac.next.swingh = getattr(irhvac, "swingh_t_kAuto",
                                     getattr(irhvac, "swingh_t_kOff", -1))

    print(f"[WORKER] proto={proto_name}({ac.next.protocol}) model={ac.next.model} "
          f"power={ac.next.power} mode={ac.next.mode} temp={ac.next.degrees} "
          f"fan={getattr(ac.next, 'fanspeed', 'N/A')} "
          f"swingv={getattr(ac.next, 'swingv', 'N/A')} "
          f"swingh={getattr(ac.next, 'swingh', 'N/A')}",
          file=sys.stderr, flush=True)

    try:
        ac.sendAc()
    except Exception as exc:
        print(f"ERROR inside sendAc(): {exc}", file=sys.stderr, flush=True)
        return 1
    t = ac.getTiming()
    if not t:
        print("ERROR: getTiming() returned None or empty list", file=sys.stderr)
        return 1
    if len(t) < 4:
        # Too short to be a valid frame — return as-is (probably off signal).
        print(json.dumps(t))
        return 0

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
