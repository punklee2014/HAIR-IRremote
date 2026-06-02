"""Tests for encoder.loader (native _irhvac.so loading)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from custom_components.hair.encoder.loader import _NATIVE_DIR, load_irhvac


def test_load_irhvac_uses_so_file_path(tmp_path: Path) -> None:
    """load_irhvac loads native/linux_<arch>/_irhvac.so via ExtensionFileLoader."""
    native_dir = tmp_path / "linux_aarch64"
    native_dir.mkdir()
    so_file = native_dir / "_irhvac.so"
    so_file.write_bytes(b"\x7fELF")  # placeholder; not executed

    fake_module = ModuleType("irhvac")
    fake_module.IRac = MagicMock()

    mock_loader = MagicMock()
    mock_loader.exec_module = MagicMock()

    with (
        patch(
            "custom_components.hair.encoder.loader._get_arch_dir",
            return_value="linux_aarch64",
        ),
        patch(
            "custom_components.hair.encoder.loader._NATIVE_DIR",
            tmp_path,
        ),
        patch(
            "custom_components.hair.encoder.loader.ExtensionFileLoader",
            return_value=mock_loader,
        ) as mock_ext_ctor,
        patch(
            "custom_components.hair.encoder.loader.importlib.util.module_from_spec",
            return_value=fake_module,
        ),
        patch(
            "custom_components.hair.encoder.loader.importlib.util.spec_from_loader",
            return_value=MagicMock(loader=mock_loader),
        ),
    ):
        result = load_irhvac()

    mock_ext_ctor.assert_called_once_with("irhvac", str(so_file))
    mock_loader.exec_module.assert_called_once()
    assert result is fake_module
    assert sys.modules.get("irhvac") is fake_module


def test_load_irhvac_missing_so_raises() -> None:
    with (
        patch(
            "custom_components.hair.encoder.loader._get_arch_dir",
            return_value="linux_aarch64",
        ),
        patch(
            "custom_components.hair.encoder.loader._NATIVE_DIR",
            Path("/nonexistent_native_root"),
        ),
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
    musl_dir = tmp_path / f"{arch}_musl"
    glibc_dir = tmp_path / arch
    musl_dir.mkdir()
    glibc_dir.mkdir()
    (musl_dir / "_irhvac.so").write_bytes(b"\x7fELF")

    with (
        patch("custom_components.hair.encoder.loader._NATIVE_DIR", tmp_path),
        patch("custom_components.hair.encoder.loader._is_musl", return_value=True),
    ):
        candidates = loader._native_candidates(arch)

    assert candidates[0] == musl_dir
    assert candidates[1] == glibc_dir
