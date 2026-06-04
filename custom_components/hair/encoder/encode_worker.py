"""Persistent worker for AC protocol encoding over stdin/stdout pipes.

Mode 1 (pipe — used by HA via ``Popen``):
    python3 encode_worker.py <native_dir> -
    Reads JSON lines from stdin, writes JSON lists to stdout, loops forever.

Mode 2 (one-shot — for manual testing):
    python3 encode_worker.py <native_dir> '<json_params>'
    Reads JSON from argv[2], writes JSON timings to stdout, exits.
"""
import json
import sys


# ── shared encoder logic ────────────────────────────────────────────────────

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


def encode_one(native_dir: str, p: dict) -> list[int]:
    sys.path.insert(0, native_dir)
    import irhvac  # noqa: E402

    protocols = {
        a.upper(): getattr(irhvac, a)
        for a in dir(irhvac)
        if a.isupper() and not a.startswith("_") and isinstance(getattr(irhvac, a), int)
    }

    proto_name = p.get("protocol", "COOLIX").upper()
    if proto_name not in protocols:
        raise ValueError(f"Unknown protocol: {proto_name}")

    modes = _attrs(irhvac, "opmode_t_", strip_k=True)
    fans  = _attrs(irhvac, "fanspeed_t_", strip_k=True)

    ac = irhvac.IRac(0)
    ac.next.protocol = protocols[proto_name]
    ac.next.model    = int(p.get("model", 1))
    ac.next.power    = bool(p.get("power", True))

    if ac.next.power:
        ac.next.mode     = modes.get(str(p.get("mode", "auto")).lower(), 0)
        ac.next.degrees  = float(p.get("degrees", 24))
        fs = p.get("fanspeed")
        if fs:
            ac.next.fanspeed = fans.get(str(fs).lower(), 0)
        sv = p.get("swingv")
        if sv is not None:
            ac.next.swingv = int(sv)
        sh = p.get("swingh")
        if sh is not None:
            ac.next.swingh = int(sh)

    ac.sendAc()
    t = ac.getTiming()
    if t is None:
        raise RuntimeError("getTiming() returned None")

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

    return t


# ── entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: encode_worker.py <native_dir> [-] | <native_dir> <json>", file=sys.stderr)
        return 2

    native_dir = sys.argv[1]

    if len(sys.argv) >= 3 and sys.argv[2] == "-":
        # ── pipe mode: read JSON lines from stdin, loop forever ──
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
                result = encode_one(native_dir, p)
                print(json.dumps(result), flush=True)
            except Exception as exc:
                print(json.dumps({"error": str(exc)}), flush=True)

    else:
        # ── one-shot mode: JSON from argv[2] ──
        params_str = sys.argv[2] if len(sys.argv) >= 3 else "{}"
        try:
            p = json.loads(params_str)
            result = encode_one(native_dir, p)
            print(json.dumps(result))
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
