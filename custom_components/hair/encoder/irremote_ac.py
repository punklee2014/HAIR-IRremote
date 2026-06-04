"""Protocol-based AC encoder using IRremoteESP8266 IRac.

Each button press calls ``subprocess_encode.py`` with pure positional
string argv — no JSON, no stdin pipe, no temp files.  Exactly matches
the proven command-line test.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
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
    raise IRHVACUnavailableError("Cannot find system python3")


async def _call_worker(nd: str, args: list[str]) -> list[int]:
    """Run ``subprocess_encode.py`` with pure positional string args.

    Command: python3 subprocess_encode.py <nd> <proto> <model> <mode> <degrees> [flags]
    """
    # Reproduce the proven manual test EXACTLY:
    #   cd <nd> && python3 <worker> <nd> <args>
    # /bin/sh resolves python3 from its own PATH — identical to SSH shell.
    shell_cmd = f"cd {nd} && python3 {_worker_path()} {nd} {' '.join(args)}"

    def _run():
        return subprocess.run(["/bin/sh", "-c", shell_cmd],
                              capture_output=True, text=True, timeout=5)

    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, _run)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Encoder subprocess timed out")

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()[:500]
        raise RuntimeError(
            f"Encoder subprocess exited {res.returncode}: {err or 'no output'}"
        )

    try:
        return json.loads(res.stdout.strip())
    except json.JSONDecodeError:
        raise RuntimeError(f"Encoder returned bad JSON: {res.stdout[:200]}")


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
    proto = (device.ir_protocol or "COOLIX").upper()
    model = str(device.ir_model or 1)
    mode = str(hvac_mode).lower()
    temp = str(round(temperature) if temperature is not None else 24)

    # Pure positional argv — no JSON, no files, no pipes.
    args = [proto, model, mode, temp]
    if not power:
        args.append("--off")
    if fan_mode:
        args.extend(["--fan", str(fan_mode).lower()])
    if swing_mode and swing_mode != "off":
        args.extend(["--swing", str(swing_mode).lower()])

    raw = await _call_worker(nd, args)

    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))

    return signed
