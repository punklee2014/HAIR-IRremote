"""Tests for encoder.loader (native _irhvac.so loading via SWIG wrapper)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from custom_components.hair.encoder.loader import _NATIVE_DIR, load_irhvac


def _setup_native_dir(tmp_path: Path, arch: str, *, has_so: bool = True) -> Path:
    """Create a fake native/<arch>/ directory with irhvac.py and _irhvac.so."""
    d = tmp_path / arch
    d.mkdir()
    (d / "irhvac.py").write_text("# SWIG wrapper")
    if has_so:
        (d / "_irhvac.so").write_bytes(b"\x7fELF")
    return d


def test_load_irhvac_via_swig_wrapper(tmp_path: Path) -> None:
    """load_irhvac uses the SWIG irhvac.py wrapper, not the .so directly."""
    native_dir = _setup_native_dir(tmp_path, "linux_aarch64")

    fake_module = ModuleType("irhvac")
    fake_module.IRac = MagicMock()

    with (
        patch("custom_components.hair.encoder.loader._get_arch_dir",
              return_value="linux_aarch64"),
        patch("custom_components.hair.encoder.loader._NATIVE_DIR", tmp_path),
        patch("custom_components.hair.encoder.loader._stdlib_import",
              return_value=fake_module) as mock_import,
    ):
        result = load_irhvac()

    mock_import.assert_called_once_with("irhvac")
    assert result is fake_module
    assert hasattr(result, "IRac")


def test_load_irhvac_missing_so_raises() -> None:
    with (
        patch("custom_components.hair.encoder.loader._get_arch_dir",
              return_value="linux_aarch64"),
        patch("custom_components.hair.encoder.loader._NATIVE_DIR",
              Path("/nonexistent_native_root")),
        pytest.raises(ImportError, match="Failed to load protocol encoder"),
    ):
        load_irhvac()


def test_load_irhvac_missing_IRac_raises(tmp_path: Path) -> None:
    """Module without IRac class is rejected."""
    native_dir = _setup_native_dir(tmp_path, "linux_aarch64")

    fake_module = ModuleType("irhvac")
    # deliberately no IRac

    with (
        patch("custom_components.hair.encoder.loader._get_arch_dir",
              return_value="linux_aarch64"),
        patch("custom_components.hair.encoder.loader._NATIVE_DIR", tmp_path),
        patch("custom_components.hair.encoder.loader._stdlib_import",
              return_value=fake_module),
        pytest.raises(ImportError, match="Failed to load protocol encoder"),
    ):
        load_irhvac()


def test_native_dir_relative_to_integration() -> None:
    """Packaged path is custom_components/hair/native/."""
    assert _NATIVE_DIR.name == "native"
    assert _NATIVE_DIR.parent.name == "hair"


def test_musl_host_prefers_musl_directory(tmp_path: Path) -> None:
    """On musl hosts, linux_<arch>_musl is tried before the glibc build."""
    from custom_components.hair.encoder import loader

    arch = "linux_aarch64"
    _setup_native_dir(tmp_path, f"{arch}_musl")
    _setup_native_dir(tmp_path, arch)

    with (
        patch("custom_components.hair.encoder.loader._NATIVE_DIR", tmp_path),
        patch("custom_components.hair.encoder.loader._is_musl", return_value=True),
    ):
        candidates = loader._native_candidates(arch)

    assert candidates[0] == tmp_path / f"{arch}_musl"
    assert candidates[1] == tmp_path / arch
