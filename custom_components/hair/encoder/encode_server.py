"""Standalone HTTP server for AC protocol encoding.

Start this ONCE on the HA host (via SSH, systemd, or docker entrypoint):

    python3 /config/custom_components/hair/encoder/encode_server.py &
    # or:
    nohup python3 /config/custom_components/hair/encoder/encode_server.py &

HA discovers the server at http://127.0.0.1:9876/encode.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_ENCODE_SERVER_PORT = 9876


# ── server main (runs in its own python3 process) ────────────────────────────

def build_maps(mod):
    proto = {}
    for a in dir(mod):
        if a.isupper() and not a.startswith("_") and isinstance(getattr(mod, a), int):
            proto[a.upper()] = getattr(mod, a)
    mode = {
        "off": -1, "auto": 0, "cool": 1, "heat": 2, "dry": 3, "fan_only": 4,
    }
    fan = {
        "auto": 0, "low": 2, "medium": 3, "high": 4,
    }
    return proto, mode, fan


def find_native_dir():
    this_dir = Path(__file__).parent  # encoder/
    native_root = this_dir.parent / "native"
    if not native_root.is_dir():
        raise FileNotFoundError(f"No native directory at {native_root}")
    for arch_dir in sorted(os.listdir(native_root)):
        d = native_root / arch_dir
        if (d / "irhvac.py").is_file() and (d / "_irhvac.so").is_file():
            return str(d)
    raise FileNotFoundError("No native irhvac directory found")


def serve_forever(native_dir: str, port: int = _ENCODE_SERVER_PORT):
    sys.path.insert(0, native_dir)
    import irhvac

    PROTO, MODE, FAN = build_maps(irhvac)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/encode":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                params = json.loads(body)
            except Exception:
                self.send_error(400, "Bad JSON")
                return

            try:
                ac = irhvac.IRac(0)
                proto_name = params.get("protocol", "COOLIX").upper()
                print(f"[encode] protocol={proto_name} model={params.get('model')} "
                      f"power={params.get('power')} mode={params.get('mode')} "
                      f"degrees={params.get('degrees')} fan={params.get('fanspeed')} "
                      f"swingv={params.get('swingv')} swingh={params.get('swingh')}",
                      flush=True)
                ac.next.protocol = PROTO.get(proto_name, 15)
                ac.next.model = params.get("model", 1)
                ac.next.power = bool(params.get("power", True))
                if ac.next.power:
                    ac.next.mode = MODE.get(params.get("mode", "auto"), 0)
                    ac.next.degrees = params.get("degrees", 24)
                    fs = params.get("fanspeed")
                    if fs:
                        ac.next.fanspeed = FAN.get(fs, 0)
                    if "swingv" in params:
                        ac.next.swingv = params["swingv"]
                    if "swingh" in params:
                        ac.next.swingh = params["swingh"]

                ac.sendAc()
                t = ac.getTiming()
                if t is None:
                    self.send_error(500, "getTiming returned None")
                    return

                # Strip trailing large gap value (100ms+ end-of-transmission
                # marker from sendAc).  Keeping it causes the IR transmitter
                # to fire multiple bursts instead of one.
                while t and t[-1] > 50000:
                    t.pop()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(t).encode())
            except Exception as exc:
                self.send_error(500, str(exc))

        def log_message(self, fmt, *args):
            pass  # silent

    srv = HTTPServer(("127.0.0.1", port), Handler)
    print(f"HAIR encode server ready on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


def start_encode_server(hass=None):
    """No-op: the server must be started externally by the user.

    Returns None — HA integration should check for the server at
    startup and warn the user if it's not running.
    """
    return None  # Server is user-managed, not HA-managed.


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    native_dir = sys.argv[1] if len(sys.argv) > 1 else find_native_dir()
    port = int(sys.argv[2]) if len(sys.argv) > 2 else _ENCODE_SERVER_PORT
    serve_forever(native_dir, port)
