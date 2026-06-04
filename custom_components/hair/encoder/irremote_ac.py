"""Protocol-based AC encoder using IRremoteESP8266 IRac.

One-shot subprocess worker — each encode call spawns via /bin/sh -c
for reliable native library loading, communicating via stdin/stdout JSON.
"""
from __future__ import annotations

import asyncio
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


# ── one-shot subprocess worker ────────────────────────────────────────────────

_ENCODE_TIMEOUT = 10  # seconds
_probe_result: bool | None = None  # module-level cache: True if .so loads


def _check_native(nd: str) -> bool:
    """Run probe once: can the native .so be loaded in a subprocess?

    Caches result; returns True if probe exits 0.
    """
    global _probe_result
    if _probe_result is not None:
        return _probe_result

    shell_cmd = f"cd {nd} && python3 {_worker_path()} --probe {nd}"
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", shell_cmd],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        _LOGGER.error("Native probe timed out")
        _probe_result = False
        return False

    ok = proc.returncode == 0
    stderr_tail = proc.stderr.strip()[-200:] if proc.stderr else ""
    if ok:
        _LOGGER.info("Native probe succeeded: %s", stderr_tail)
    else:
        _LOGGER.error(
            "Native probe FAILED (exit=%s): %s", proc.returncode, stderr_tail
        )
    _probe_result = ok
    return ok


async def _call_worker(nd: str, request: dict[str, Any]) -> list[int]:
    """Run a one-shot subprocess encode via /bin/sh -c (proven environment)."""
    loop = asyncio.get_running_loop()

    def _run_oneshot() -> list[int]:
        shell_cmd = f"cd {nd} && python3 {_worker_path()} --once {nd}"
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", shell_cmd],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=_ENCODE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Encoder timed out after {_ENCODE_TIMEOUT}s")

        stderr_tail = proc.stderr[-500:] if proc.stderr else ""
        if proc.returncode != 0:
            _LOGGER.error("Encoder exit=%s stderr=%s", proc.returncode, stderr_tail)
            raise RuntimeError(
                f"Encoder failed (exit={proc.returncode}): {stderr_tail}"
            )

        try:
            data = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            raise RuntimeError(f"Encoder returned bad JSON: {proc.stdout[:200]}")

        if isinstance(data, dict) and "err" in data:
            raise RuntimeError(f"Encoder error: {data['err']}")

        if not isinstance(data, list):
            raise RuntimeError(f"Encoder returned non-list: {proc.stdout[:200]}")

        return data

    return await loop.run_in_executor(None, _run_oneshot)


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
    if not _check_native(nd):
        raise RuntimeError(
            "Native encoder probe failed — irhvac .so may be incompatible "
            "with this Python version. Check HA logs for probe details."
        )
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