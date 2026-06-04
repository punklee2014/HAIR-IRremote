"""Protocol-based AC encoder using IRremoteESP8266 IRac.

Persistent daemon worker — single long-lived subprocess for all encode calls.
Communicates via stdin/stdout JSON line protocol.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import platform
import subprocess
from pathlib import Path
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)


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
    return str(Path(__file__).parent / "subprocess_encode.py")


# ── persistent daemon worker ─────────────────────────────────────────────────

_worker_proc: subprocess.Popen | None = None
_worker_nd: str | None = None


def _start_worker(nd: str) -> subprocess.Popen:
    """Start daemon via /bin/sh -c to replicate proven shell environment."""
    shell_cmd = f"cd {nd} && exec python3 {_worker_path()} --daemon {nd}"
    return subprocess.Popen(
        ["/bin/sh", "-c", shell_cmd],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop_worker() -> None:
    global _worker_proc, _worker_nd
    proc = _worker_proc
    _worker_proc = None
    _worker_nd = None
    if proc is None:
        return
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


atexit.register(_stop_worker)


async def _call_worker(nd: str, request: dict[str, Any]) -> list[int]:
    """Send JSON request to daemon worker, return timing list.

    Auto-starts worker on first call; auto-restarts if worker dies.
    """
    global _worker_proc, _worker_nd

    loop = asyncio.get_running_loop()

    def _send_recv() -> list[int]:
        global _worker_proc, _worker_nd

        # Start or reset worker if needed.
        if _worker_proc is None or _worker_proc.poll() is not None or _worker_nd != nd:
            _stop_worker()
            _worker_proc = _start_worker(nd)
            _worker_nd = nd

        line = json.dumps(request) + "\n"
        proc = _worker_proc

        try:
            proc.stdin.write(line)
            proc.stdin.flush()
            resp_line = proc.stdout.readline()
        except (BrokenPipeError, OSError, ValueError) as exc:
            _LOGGER.error("Worker pipe broken: %s", exc)
            _stop_worker()
            raise RuntimeError(f"Encoder worker communication failed: {exc}")

        if not resp_line:
            exit_code = proc.poll()
            _LOGGER.error("Worker died! exit=%s", exit_code)
            _stop_worker()
            raise RuntimeError(
                f"Encoder worker exited unexpectedly (exit={exit_code})"
            )

        resp_line = resp_line.strip()
        try:
            data = json.loads(resp_line)
        except json.JSONDecodeError:
            raise RuntimeError(f"Worker returned bad JSON: {resp_line[:200]}")

        if isinstance(data, dict) and "err" in data:
            raise RuntimeError(f"Encoder error: {data['err']}")

        if not isinstance(data, list):
            raise RuntimeError(f"Worker returned non-list: {resp_line[:200]}")

        return data

    return await loop.run_in_executor(None, _send_recv)


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


async def encode(
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
    request: dict[str, Any] = {
        "proto": (device.ir_protocol or "COOLIX").upper(),
        "model": int(device.ir_model or 1),
        "mode": str(hvac_mode).lower(),
        "degrees": int(round(float(temperature if temperature is not None else 24))),
        "power": power,
    }
    if fan_mode:
        request["fan"] = str(fan_mode).lower()
    if swing_mode and swing_mode != "off":
        sw = str(swing_mode).lower()
        if sw in ("vertical", "on", "both"):
            request["swingv"] = "vertical"
        if sw in ("horizontal", "both"):
            request["swingh"] = "horizontal"

    raw = await _call_worker(nd, request)

    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))

    return signed