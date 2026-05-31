"""Load the irhvac native module for the current platform."""
from __future__ import annotations

import logging
import platform
from pathlib import Path
from types import ModuleType

_LOGGER = logging.getLogger(__name__)

_NATIVE_DIR = Path(__file__).parent.parent / "native"


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
    """Import irhvac from the precompiled native module for this architecture.

    Returns the ``irhvac`` module if available.

    Raises ``ImportError`` with a clear user-facing message when the native
    module is missing or the architecture is unsupported.
    """
    arch_dir = _get_arch_dir()
    native_path = _NATIVE_DIR / arch_dir

    if not native_path.is_dir():
        raise ImportError(
            f"HAIR-IRremote native module not found for {arch_dir}. "
            f"Expected directory: {native_path}. "
            f"Reinstall the integration or build _irhvac.so for your platform."
        )

    so_file = native_path / "_irhvac.so"
    if not so_file.exists():
        raise ImportError(
            f"HAIR-IRremote native binary missing: {so_file}. "
            f"Reinstall the integration or build from source."
        )

    # Insert the native path so ``import irhvac`` finds both irhvac.py and
    # _irhvac.so in the same directory.
    import sys
    path_str = str(native_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

    try:
        import irhvac  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            f"Failed to import irhvac from {native_path}: {exc}"
        ) from exc

    _LOGGER.info("Loaded irhvac from %s", native_path)
    return irhvac
