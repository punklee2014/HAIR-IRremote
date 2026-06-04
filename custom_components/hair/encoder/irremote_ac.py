"""Protocol-based AC encoder using IRremoteESP8266 IRac.

All native code runs in a lightweight background HTTP server (started
automatically by HA on setup, killed on unload).  HA communicates via
http://127.0.0.1:9876/encode.

This is the ONLY crash-safe approach for musl/aarch64 — process-internal
irhvac loading causes SIGSEGV due to libpython ABI conflict with HA's
Python runtime.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)

_ENCODE_SERVER_PORT = 9876
_SERVER_PROC: subprocess.Popen | None = None

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


class IRHVACUnavailableError(ImportError):
    pass


# ── encode server (runs as independent python3 process) ──────────────────────

_ENCODE_SERVER_CODE = r"""
import json, os, sys
from http.server import HTTPServer, BaseHTTPRequestHandler

native_dir = os.environ["HAIR_NATIVE_DIR"]
sys.path.insert(0, native_dir)
import irhvac

PROTO = {a.upper(): getattr(irhvac, a) for a in dir(irhvac)
         if a.isupper() and not a.startswith("_") and isinstance(getattr(irhvac, a), int)}
MODE = {
    "off": getattr(irhvac, "opmode_t_kOff", -1),
    "auto": getattr(irhvac, "opmode_t_kAuto", 0),
    "cool": getattr(irhvac, "opmode_t_kCool", 1),
    "heat": getattr(irhvac, "opmode_t_kHeat", 2),
    "dry": getattr(irhvac, "opmode_t_kDry", 3),
    "fan_only": getattr(irhvac, "opmode_t_kFan", 4),
}
FAN = {
    "auto": getattr(irhvac, "fanspeed_t_kAuto", 0),
    "low": getattr(irhvac, "fanspeed_t_kLow", 2),
    "medium": getattr(irhvac, "fanspeed_t_kMedium", 3),
    "high": getattr(irhvac, "fanspeed_t_kHigh", 4),
}
SWINGV = getattr(irhvac, "swingv_t_kAuto", 0)
SWINGH = getattr(irhvac, "swingh_t_kAuto", 0)

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/encode":
            self.send_error(404); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            p = json.loads(self.rfile.read(length))
        except Exception:
            self.send_error(400); return
        try:
            ac = irhvac.IRac(0)
            ac.next.protocol = PROTO.get(p.get("protocol","COOLIX").upper(), 15)
            ac.next.model = p.get("model", 1)
            ac.next.power = bool(p.get("power", True))
            if ac.next.power:
                ac.next.mode = MODE.get(p.get("mode","auto"), MODE["auto"])
                ac.next.degrees = p.get("degrees", 24)
                fs = p.get("fanspeed")
                if fs: ac.next.fanspeed = FAN.get(fs, FAN["auto"])
                if "swingv" in p: ac.next.swingv = p["swingv"]
                if "swingh" in p: ac.next.swingh = p["swingh"]
            ac.sendAc()
            t = ac.getTiming()
            if t is None:
                self.send_error(500); return
            # truncate to single frame
            hdr = 0
            for i in range(0, len(t), 2):
                if t[i] > 2000:
                    hdr += 1
                    if hdr >= 2: t = t[:i]; break
            while t and t[-1] > 50000: t.pop()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(t).encode())
        except Exception as e:
            self.send_error(500, str(e))
    def log_message(self, f, *a): pass

HTTPServer(("127.0.0.1", int(os.environ.get("HAIR_PORT", "9876"))), H).serve_forever()
"""


def _find_native_dir() -> str | None:
    nd_root = Path(__file__).parent.parent / "native"
    machine = platform.machine()
    if machine in ("aarch64", "arm64", "armv8l", "armv8b"):
        arch = "linux_aarch64"
    else:
        arch = "linux_x86_64"
    for suffix in ("_musl", ""):
        d = nd_root / f"{arch}{suffix}"
        if (d / "irhvac.py").is_file() and (d / "_irhvac.so").is_file():
            return str(d)
    return None


def start_encode_server(hass) -> None:
    """Start the encode server as an independent subprocess."""
    global _SERVER_PROC
    if _SERVER_PROC is not None and _SERVER_PROC.poll() is None:
        return  # already running

    nd = _find_native_dir()
    if nd is None:
        _LOGGER.warning("Cannot start encode server: no native dir found")
        return

    env = os.environ.copy()
    env["HAIR_NATIVE_DIR"] = nd
    env["HAIR_PORT"] = str(_ENCODE_SERVER_PORT)
    # Strip musl-hostile variables
    for v in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH"):
        env.pop(v, None)

    # Use system python3 (NOT sys.executable — HA's Python 3.14 runtime
    # has ABI differences with the musl .so).  System python3 was verified
    # to work correctly in manual testing.
    py_exe = "python3"

    try:
        _SERVER_PROC = subprocess.Popen(
            [py_exe, "-c", _ENCODE_SERVER_CODE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,  # survive HA restart
        )
    except Exception as exc:
        _LOGGER.warning("Failed to start encode server: %s", exc)
        return

    # Wait for server to be ready.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", _ENCODE_SERVER_PORT), timeout=0.5)
            s.close()
            _LOGGER.info("Encode server ready (pid %s)", _SERVER_PROC.pid)
            return
        except (OSError, ConnectionRefusedError):
            time.sleep(0.1)
    _LOGGER.warning("Encode server did not become ready within 3s")


def stop_encode_server() -> None:
    global _SERVER_PROC
    if _SERVER_PROC is not None:
        try:
            _SERVER_PROC.send_signal(signal.SIGTERM)
            _SERVER_PROC.wait(timeout=2)
        except Exception:
            try:
                _SERVER_PROC.kill()
            except Exception:
                pass
        _SERVER_PROC = None
        _LOGGER.info("Encode server stopped")


# ── public API ───────────────────────────────────────────────────────────────

def probe_protocol_encoder() -> tuple[bool, str | None]:
    if _find_native_dir() is None:
        return False, "No native irhvac directory"
    try:
        s = socket.create_connection(("127.0.0.1", _ENCODE_SERVER_PORT), timeout=1)
        s.close()
        return True, None
    except Exception as exc:
        return False, f"Encode server not reachable: {exc}"


def is_protocol_encoder_available() -> bool:
    return probe_protocol_encoder()[0]


def get_protocol_models(protocol: str | None = None) -> dict[str, list[dict[str, int | str]]]:
    if protocol is None:
        return dict(PROTOCOL_MODELS)
    return {protocol.upper(): PROTOCOL_MODELS.get(protocol.upper(), [])}


def get_supported_protocols() -> list[str]:
    return sorted(PROTOCOL_MODELS.keys())


def _rpc(params: dict[str, Any]) -> list[int]:
    url = f"http://127.0.0.1:{_ENCODE_SERVER_PORT}/encode"
    data = json.dumps(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=10) as resp:
        return json.loads(resp.read().decode())


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
    params: dict[str, Any] = {
        "protocol": (device.ir_protocol or "COOLIX").upper(),
        "model": device.ir_model or 1,
        "power": power,
    }
    if power:
        params["mode"] = hvac_mode
        params["degrees"] = round(temperature) if temperature is not None else 24
        if fan_mode:
            params["fanspeed"] = fan_mode
        if swing_mode and swing_mode != "off":
            if swing_mode in ("vertical", "on", "both"):
                params["swingv"] = 0
            if swing_mode in ("horizontal", "both"):
                params["swingh"] = 0

    raw = _rpc(params)
    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))
    return signed
