"""Load the irhvac native module for the current platform."""
from __future__ import annotations

import logging
import platform
import sys
from pathlib import Path
from types import ModuleType

_LOGGER = logging.getLogger(__name__)

_NATIVE_DIR = Path(__file__).parent.parent / "native"
_irhvac_cache: ModuleType | None = None


def _is_musl() -> bool:
    """Guess whether the host uses musl (not glibc).  Result is cached."""
    key = "_hair_musl_guess"
    cached = getattr(sys, key, None)
    if cached is not None:
        return cached
    if sys.platform != "linux":
        setattr(sys, key, False)
        return False
    for glibc_ld in (
        Path("/lib/ld-linux-aarch64.so.1"),
        Path("/lib64/ld-linux-x86-64.so.2"),
    ):
        if glibc_ld.is_file():
            setattr(sys, key, False)
            return False
    lib = Path("/lib")
    if lib.is_dir():
        for musl_ld in lib.glob("ld-musl-*.so.1"):
            if musl_ld.is_file():
                setattr(sys, key, True)
                return True
    setattr(sys, key, True)
    return True


def _native_candidates(arch_dir: str) -> list[Path]:
    glibc_dir = _NATIVE_DIR / arch_dir
    musl_dir = _NATIVE_DIR / f"{arch_dir}_musl"
    if _is_musl():
        return [musl_dir, glibc_dir]
    return [glibc_dir, musl_dir]


def _get_arch_dir() -> str:
    machine = platform.machine()
    if machine in ("aarch64", "arm64", "armv8l", "armv8b"):
        return "linux_aarch64"
    if machine in ("x86_64", "amd64", "AMD64"):
        return "linux_x86_64"
    _LOGGER.warning("Unknown platform machine: %s, trying linux_x86_64", machine)
    return "linux_x86_64"


def load_irhvac() -> ModuleType:
    """Return the ``irhvac`` module.

    On first call the matching native directory is added to ``sys.path``
    and ``irhvac`` (the SWIG wrapper) is imported.  Subsequent calls
    return the cached module — the path is never removed.
    """
    global _irhvac_cache
    if _irhvac_cache is not None:
        return _irhvac_cache

    arch_dir = _get_arch_dir()
    candidates = _native_candidates(arch_dir)
    tried: list[str] = []

    _LOGGER.debug("Loading irhvac (arch=%s musl=%s)", arch_dir, _is_musl())

    for native_path in candidates:
        so = native_path / "_irhvac.so"
        py = native_path / "irhvac.py"
        tried.append(str(native_path))
        if not so.is_file() or not py.is_file():
            _LOGGER.debug("Skipping %s (incomplete)", native_path)
            continue

        native_str = str(native_path)
        if native_str not in sys.path:
            sys.path.insert(0, native_str)

        try:
            # Clear stale caches so re-import always picks up this dir.
            sys.modules.pop("irhvac", None)
            sys.modules.pop("_irhvac", None)
            from importlib import import_module as _import

            mod = _import("irhvac")
        except Exception as exc:
            _LOGGER.warning("Failed to import irhvac from %s: %s", native_path, exc)
            continue

        if not hasattr(mod, "IRac"):
            _LOGGER.warning("irhvac from %s is missing IRac", native_path)
            sys.modules.pop("irhvac", None)
            sys.modules.pop("_irhvac", None)
            continue

        _LOGGER.info("Loaded irhvac from %s", native_path)
        _irhvac_cache = mod
        return mod

    raise ImportError(
        "Protocol encoder native library not found.  Tried: "
        + "; ".join(tried)
        + ".  "
        + (
            "Your host seems to use musl — install "
            f"custom_components/hair/native/{arch_dir}_musl/."
            if _is_musl()
            else (
                "Download the release zip and copy "
                f"custom_components/hair/native/{arch_dir}/ "
                "to your HA instance."
            )
        )
    )
