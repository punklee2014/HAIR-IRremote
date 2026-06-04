"""Protocol-based AC encoder using IRremoteESP8266 IRac.

A single ``python3`` child process is forked ONCE at HA startup (when
the process is still single-threaded — avoids musl pthread+fork bugs).
All subsequent encodes communicate with the child via stdin/stdout pipes.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import threading
from pathlib import Path
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)

_WORKER: subprocess.Popen | None = None
_WORKER_LOCK = threading.Lock()


class IRHVACUnavailableError(ImportError):
    pass


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


def _worker_path() -> str:
    return str(Path(__file__).parent / "encode_worker.py")


# ── pipe-based persistent worker ─────────────────────────────────────────────

def _start_worker(hass) -> None:
    """Fork the encoder worker ONCE at startup (avoids musl thread+fork).

    The worker reads one JSON line from stdin, writes one JSON line to
    stdout, then exits.  We keep stdin/stdout open for the lifetime of
    the HA process.
    """
    global _WORKER
    if _WORKER is not None and _WORKER.poll() is None:
        return

    nd = _find_native_dir()
    if nd is None:
        _LOGGER.warning("Cannot start encode worker: no native dir")
        return

    try:
        _WORKER = subprocess.Popen(
            ["/usr/bin/python3", "-u", _worker_path(), nd, "-"],  # "-" = stdin mode
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:
        _LOGGER.warning("Failed to start encode worker: %s", exc)
        _WORKER = None
        return

    _LOGGER.info("Encode worker started (pid %s)", _WORKER.pid)


def _stop_worker() -> None:
    global _WORKER
    if _WORKER is not None:
        try:
            _WORKER.stdin.close()
            _WORKER.wait(timeout=2)
        except Exception:
            _WORKER.kill()
        _WORKER = None


def _call_worker(params: dict[str, Any]) -> list[int]:
    """Send a single encode request to the persistent worker via stdin.

    Thread-safe: serialised by _WORKER_LOCK.
    """
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or _WORKER.poll() is not None:
            # Worker died — restart
            _start_worker(None)
            if _WORKER is None or _WORKER.poll() is not None:
                raise IRHVACUnavailableError("Encode worker is not running")

        json_line = json.dumps(params) + "\n"

        try:
            _WORKER.stdin.write(json_line)
            _WORKER.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError(f"Worker pipe broken: {exc}") from exc

        try:
            resp_line = _WORKER.stdout.readline()
        except Exception as exc:
            raise RuntimeError(f"Worker read error: {exc}") from exc

        if not resp_line:
            # Worker exited — read stderr for error info
            err = ""
            try:
                err = _WORKER.stderr.read()[:500]
            except Exception:
                pass
            raise RuntimeError(f"Worker exited unexpectedly: {err.strip()}")

        try:
            return json.loads(resp_line.strip())
        except json.JSONDecodeError:
            raise RuntimeError(f"Worker returned bad JSON: {resp_line[:200]}")


# ── public API ───────────────────────────────────────────────────────────────

def probe_protocol_encoder() -> tuple[bool, str | None]:
    if _find_native_dir() is None:
        return False, "No native irhvac directory"
    return True, None


def is_protocol_encoder_available() -> bool:
    return probe_protocol_encoder()[0]


def get_protocol_models(protocol: str | None = None) -> dict[str, list[dict[str, int | str]]]:
    if protocol is None:
        return dict(PROTOCOL_MODELS)
    return {protocol.upper(): PROTOCOL_MODELS.get(protocol.upper(), [])}


def get_supported_protocols() -> list[str]:
    return sorted(PROTOCOL_MODELS.keys())


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
    nd = _find_native_dir()
    if nd is None:
        raise IRHVACUnavailableError("No native irhvac directory")

    params: dict[str, Any] = {
        "protocol": (device.ir_protocol or "COOLIX").upper(),
        "model": device.ir_model or 1,
        "power": bool(power),
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

    raw = _call_worker(params)

    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))

    return signed
