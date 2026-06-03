"""Load the irhvac native module for the current platform."""
from __future__ import annotations

import logging
import platform
import sys
from importlib import import_module as _stdlib_import
from pathlib import Path
from types import ModuleType

_LOGGER = logging.getLogger(__name__)

_NATIVE_DIR = Path(__file__).parent.parent / "native"


def _is_musl() -> bool:
    """Return True when the HA host likely uses musl (not glibc).

    Result is cached at module level — the filesystem doesn't change at runtime.
    """
    if _is_musl_cached is not None:
        return _is_musl_cached
    if sys.platform != "linux":
        _is_musl_cached = False
        return False
    for glibc_ld in (
        Path("/lib/ld-linux-aarch64.so.1"),
        Path("/lib64/ld-linux-x86-64.so.2"),
    ):
        if glibc_ld.is_file():
            _is_musl_cached = False
            return False
    lib = Path("/lib")
    if lib.is_dir():
        for musl_ld in lib.glob("ld-musl-*.so.1"):
            if musl_ld.is_file():
                _is_musl_cached = True
                return True
    _is_musl_cached = True
    return True

_is_musl_cached: bool | None = None


def _native_candidates(arch_dir: str) -> list[Path]:
    """Directories to try, in order, for ``irhvac.py`` + ``_irhvac.so``."""
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
    """Load the ``irhvac`` module via its SWIG wrapper.

    The SWIG-generated ``irhvac.py`` imports ``_irhvac.so`` and exposes
    ``IRac`` etc.  We add the correct native directory to ``sys.path``
    and then ``import irhvac`` — this way the SWIG wrapper handles any
    module-name mangling (``package=pyhvac`` ↔ bare ``irhvac``).
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
        py_file = native_path / "irhvac.py"
        so_file = native_path / "_irhvac.so"

        # Use str() for human-readable messages.
        tried.append(str(native_path))

        if not py_file.is_file() or not so_file.is_file():
            errors.append(
                f"{native_path}: missing irhvac.py ({py_file.is_file()}) "
                f"or _irhvac.so ({so_file.is_file()})"
            )
            _LOGGER.debug("Skipping %s (incomplete)", native_path)
            continue

        # Inject this directory at front of sys.path so ``import irhvac``
        # finds irhvac.py (which does ``import _irhvac`` internally).
        native_str = str(native_path)
        if native_str not in sys.path:
            sys.path.insert(0, native_str)

        try:
            # Clean up any previous failed attempt so we get a fresh import.
            sys.modules.pop("irhvac", None)
            sys.modules.pop("_irhvac", None)
            sys.modules.pop("pyhvac.irhvac", None)
            module = _stdlib_import("irhvac")
        except Exception as exc:
            detail = str(exc).strip() or "unknown error"
            errors.append(f"{native_path}: {detail}")
            _LOGGER.warning("Failed to import irhvac from %s: %s", native_path, detail)
            continue
        finally:
            # Don't leave the native dir permanently on sys.path.
            if native_str in sys.path:
                sys.path.remove(native_str)

        # Verify the module is functional.
        if not hasattr(module, "IRac"):
            errors.append(f"{native_path}: imported but has no IRac class")
            _LOGGER.warning("irhvac loaded from %s but missing IRac", native_path)
            sys.modules.pop("irhvac", None)
            sys.modules.pop("_irhvac", None)
            continue

        _LOGGER.info("Loaded irhvac from %s", native_path)
        return module

    # Build a user-friendly hint.
    is_musl = _is_musl()
    parts: list[str] = []
    if is_musl:
        parts.append(
            "Your HA host runs on musl libc "
            "(no glibc dynamic linker found at /lib/ld-linux-aarch64.so.1 or "
            "/lib64/ld-linux-x86-64.so.2). "
            "You need the musl build. "
            "Install the musl build at "
            f"custom_components/hair/native/{arch_dir}_musl/ "
            "(see build/docker/Dockerfile.irhvac.musl), "
            "or switch the device to Learned mode."
        )
    else:
        parts.append(
            "Your HA host uses glibc. "
            "Download the release zip and copy "
            f"custom_components/hair/native/{arch_dir}/ to your HA instance, "
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
