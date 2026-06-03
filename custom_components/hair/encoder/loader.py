"""Load the irhvac native module for the current platform."""
from __future__ import annotations

import ctypes
import importlib.util
import logging
import platform
import sys
from importlib.machinery import ExtensionFileLoader
from pathlib import Path
from types import ModuleType

_LOGGER = logging.getLogger(__name__)

_NATIVE_DIR = Path(__file__).parent.parent / "native"


def _is_musl() -> bool:
    """Return True when the HA host likely uses musl (not glibc)."""
    if sys.platform != "linux":
        return False
    # glibc dynamic linker present → use glibc build.
    for glibc_ld in (
        Path("/lib/ld-linux-aarch64.so.1"),
        Path("/lib64/ld-linux-x86-64.so.2"),
    ):
        if glibc_ld.is_file():
            return False
    lib = Path("/lib")
    if lib.is_dir():
        for musl_ld in lib.glob("ld-musl-*.so.1"):
            if musl_ld.is_file():
                return True
    return True


def _native_candidates(arch_dir: str) -> list[Path]:
    """Directories to try, in order, for ``_irhvac.so``."""
    glibc_dir = _NATIVE_DIR / arch_dir
    musl_dir = _NATIVE_DIR / f"{arch_dir}_musl"
    if _is_musl():
        return [musl_dir, glibc_dir]
    return [glibc_dir, musl_dir]


def _load_so_module(so_file: Path) -> ModuleType:
    """Load a single ``_irhvac.so`` extension file as module ``irhvac``."""
    loader = ExtensionFileLoader("irhvac", str(so_file))
    spec = importlib.util.spec_from_loader("irhvac", loader)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {so_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules["irhvac"] = module
    spec.loader.exec_module(module)
    return module


def _get_arch_dir() -> str:
    machine = platform.machine()
    # Normalise common aarch64 names.
    if machine in ("aarch64", "arm64", "armv8l", "armv8b"):
        return "linux_aarch64"
    # x86_64
    if machine in ("x86_64", "amd64", "AMD64"):
        return "linux_x86_64"
    # Best-effort fallback.
    _LOGGER.warning("Unknown platform machine: %s, trying linux_x86_64", machine)
    return "linux_x86_64"


def load_irhvac() -> ModuleType:
    """Load ``_irhvac.so`` as the ``irhvac`` Python module.

    The integration ships a prebuilt extension at::

        custom_components/hair/native/linux_<arch>/_irhvac.so

    (``irhvac.py`` from the SWIG build may sit beside it but is optional;
    we load the ``.so`` directly.)

    Returns the ``irhvac`` module if available.

    Raises ``ImportError`` with a clear user-facing message when the native
    module is missing or the architecture is unsupported.
    """
    arch_dir = _get_arch_dir()
    candidates = _native_candidates(arch_dir)
    errors: list[str] = []
    tried: list[str] = []

    _LOGGER.debug(
        "Loading irhvac: arch_dir=%s, candidates=%s, musl=%s",
        arch_dir, candidates, _is_musl(),
    )

    for native_path in candidates:
        so_file = native_path / "_irhvac.so"
        tried.append(str(so_file))
        if not so_file.is_file():
            errors.append(f"{so_file}: file not found")
            continue
        try:
            module = _load_so_module(so_file)
        except Exception as exc:
            errors.append(f"{so_file}: {exc}")
            continue
        _LOGGER.info("Loaded irhvac from %s", so_file)
        return module

    # Build a user-friendly hint.
    parts: list[str] = []
    if _is_musl():
        parts.append(
            "Your HA host uses musl libc. "
            "We need a musl build. "
            "Install the musl build at "
            f"custom_components/hair/native/{arch_dir}_musl/_irhvac.so "
            "(see build/docker/Dockerfile.irhvac.musl), "
            "or switch the device to Learned mode."
        )
    else:
        parts.append(
            "Your HA host uses glibc. "
            "Download the release zip and copy "
            f"custom_components/hair/native/{arch_dir}/_irhvac.so to your HA instance, "
            "or switch the device to Learned mode."
        )
    raise ImportError(
        "Failed to load protocol encoder native library. Tried: "
        + "; ".join(tried)
        + ". Details: "
        + " | ".join(errors)
        + ". "
        + " ".join(parts)
    )
