"""Protocol-based AC encoder using IRremoteESP8266 IRac.

Each button press fires a one-shot ``python3`` subprocess running
``encode_worker.py`` via ``asyncio.create_subprocess_exec`` — fully
async, zero thread pool usage, zero in-process C loading.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
import subprocess  # only for CalledProcessError
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
    return str(Path(__file__).parent / "encode_worker.py")


# ── system python3 (cached) ──────────────────────────────────────────────────

_SYS_PYTHON: str | None = None


def _get_system_python() -> str:
    global _SYS_PYTHON
    if _SYS_PYTHON is not None:
        return _SYS_PYTHON
    for candidate in ("python3", "/usr/bin/python3", "/usr/local/bin/python3"):
        found = shutil.which(candidate)
        if found and Path(found).is_file():
            _SYS_PYTHON = found
            _LOGGER.info("System python3 at %s", found)
            return found
    raise IRHVACUnavailableError(
        "Cannot find system python3 — tried python3, /usr/bin/python3, "
        "/usr/local/bin/python3"
    )


# ── async subprocess ─────────────────────────────────────────────────────────

async def _call_worker(params: dict[str, Any]) -> list[int]:
    """Call ``encode_worker.py`` via ``asyncio.create_subprocess_exec``.

    Fully async — no thread pool, no blocking I/O, microsecond-level
    error detection on subprocess crash.
    """
    # Minimal env — only HOME and PATH.  Anything inherited from HA's
    # polluted Docker env can leak paths that confuse musl ld on load.
    import os
    bare_env: dict[str, str] = {}
    for key in ("HOME", "PATH"):
        val = os.environ.get(key)
        if val:
            bare_env[key] = val

    try:
        proc = await asyncio.create_subprocess_exec(
            _get_system_python(),
            _worker_path(),
            _find_native_dir(),
            json.dumps(params),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=bare_env,
        )
    except FileNotFoundError:
        raise IRHVACUnavailableError("System python3 not found")

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=10,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("Encoder subprocess timed out")

    returncode = proc.returncode
    stderr = (stderr_bytes or b"").decode(errors="replace")
    stdout = (stdout_bytes or b"").decode(errors="replace")

    # Log any debug output from the worker (stderr).
    if stderr.strip():
        _LOGGER.debug("Worker stderr: %s", stderr.strip()[:500])

    if returncode != 0:
        _LOGGER.error(
            "Encoder exit=%s stderr=%s stdout=%s",
            returncode, stderr.strip()[:300], stdout.strip()[:200],
        )
        raise RuntimeError(
            f"Encoder subprocess exited {returncode}: "
            f"{(stderr or stdout or 'no output').strip()[:500]}"
        )

    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError:
        raise RuntimeError(f"Encoder returned bad JSON: {stdout[:200]}")


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

    raw = await _call_worker(params)

    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))

    return signed
