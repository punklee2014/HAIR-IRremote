"""Protocol-based AC encoder via HTTP to a standalone encode server.

No native code runs in-process — the encode server is an independent
``python3`` process (start it once via SSH).  HA communicates over
http://127.0.0.1:9876/encode.
"""
from __future__ import annotations

import json
import logging
import platform
import socket
import urllib.request
from pathlib import Path
from typing import Any

from ..models import IRDevice

_LOGGER = logging.getLogger(__name__)

_ENCODE_SERVER_PORT = 9876


class IRHVACUnavailableError(ImportError):
    """Raised when the native irhvac module cannot be loaded."""


PROTOCOL_MODELS: dict[str, list[dict[str, int | str]]] = {
    "ARGO": [{"value": 1, "label": "SAC_WREM2 (Default)"}, {"value": 2, "label": "SAC_WREM3"}],
    "FUJITSU_AC": [
        {"value": 1, "label": "ARRAH2E (Default)"}, {"value": 2, "label": "ARDB1"},
        {"value": 3, "label": "ARREB1E"}, {"value": 4, "label": "ARJW2"},
        {"value": 5, "label": "ARRY4"}, {"value": 6, "label": "ARREW4E"},
    ],
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
    elif machine in ("x86_64", "amd64", "AMD64"):
        arch = "linux_x86_64"
    else:
        arch = "linux_x86_64"
    for suffix in ("_musl", ""):
        d = nd_root / f"{arch}{suffix}"
        if (d / "irhvac.py").is_file() and (d / "_irhvac.so").is_file():
            return str(d)
    return None


def probe_protocol_encoder() -> tuple[bool, str | None]:
    nd = _find_native_dir()
    if nd is None:
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

    _LOGGER.debug("RPC encode: %s", params)
    raw = _rpc(params)

    signed: list[int] = []
    for i, val in enumerate(raw):
        signed.append(int(val) if i % 2 == 0 else -int(val))

    return signed
