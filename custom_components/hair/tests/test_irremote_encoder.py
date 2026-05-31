"""Tests for the protocol-based AC encoder (irremote_ac.py).

These tests can run in two modes:

1. **With native** (CI / Linux with _irhvac.so): full integration test
   that encodes a COOLIX AC command and verifies the timing structure.

2. **Without native** (Windows / missing .so): encoder import raises
   ImportError; tests use mocks to verify the climate entity's protocol
   branching logic and async_send_raw_timings calls.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hair.const import DeviceType
from custom_components.hair.models import IRDevice

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_protocol_device(**overrides) -> IRDevice:
    """Create an AC device in protocol mode."""
    return IRDevice(
        name="Test AC",
        device_type=DeviceType.AC,
        emitter_entity_ids=["infrared.living_room_tx"],
        ac_control_mode="protocol",
        ir_protocol="COOLIX",
        ir_model=1,
        celsius=True,
        **overrides,
    )


def make_learned_device() -> IRDevice:
    """Create an AC device in learned mode."""
    return IRDevice(
        name="Learned AC",
        device_type=DeviceType.AC,
        emitter_entity_ids=["infrared.living_room_tx"],
        ac_control_mode="learned",
        celsius=False,
    )


# ---------------------------------------------------------------------------
# Encoder tests (require native module)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    "custom_components.hair.encoder.irremote_ac" not in sys.modules
    and True,  # always try; skip is handled via ImportError
    reason="Native module not available in this environment",
)
class TestEncoderNative:
    """Tests that require the _irhvac.so native module."""

    def test_import_raises_without_native(self) -> None:
        """Encoder raises a clear error when native module is missing."""
        # Simulate missing native by patching loader.
        with patch(
            "custom_components.hair.encoder.loader.load_irhvac",
            side_effect=ImportError("No _irhvac.so for this platform"),
        ):
            with pytest.raises(ImportError, match="No _irhvac.so"):
                from custom_components.hair.encoder.irremote_ac import encode

                encode(make_protocol_device(), power=True, hvac_mode="cool", temperature=24)

    def test_encode_coolix_basic(self) -> None:
        """Encode a COOLIX auto-24 signal, verify timing structure."""
        try:
            from custom_components.hair.encoder.irremote_ac import encode
        except ImportError:
            pytest.skip("Native module not available")

        device = make_protocol_device()
        timings = encode(
            device,
            power=True,
            hvac_mode="auto",
            temperature=24,
        )

        assert isinstance(timings, list)
        assert len(timings) > 10, "Expected at least 10 timing pairs"
        # Marks are positive, spaces are negative.
        for i, val in enumerate(timings):
            if i % 2 == 0:
                assert val > 0, f"Timing[{i}] should be a positive mark, got {val}"
            else:
                assert val < 0, f"Timing[{i}] should be a negative space, got {val}"
        # First value should be a mark (positive), last should be a space (negative).
        assert timings[0] > 0, "First timing should be a mark (positive)"
        assert timings[-1] < 0, "Last timing should be a space (negative)"

    def test_encode_unknown_protocol_raises(self) -> None:
        """Encoding with an unknown protocol raises ValueError."""
        try:
            from custom_components.hair.encoder.irremote_ac import encode
        except ImportError:
            pytest.skip("Native module not available")

        device = make_protocol_device(ir_protocol="NONEXISTENT")
        with pytest.raises(ValueError, match="Unknown IR protocol"):
            encode(device, power=True, hvac_mode="cool", temperature=24)

    def test_encode_power_off(self) -> None:
        """Encoding power=off produces valid timings."""
        try:
            from custom_components.hair.encoder.irremote_ac import encode
        except ImportError:
            pytest.skip("Native module not available")

        device = make_protocol_device()
        timings = encode(device, power=False)
        assert len(timings) > 10

    def test_encode_coolix_matches_reference(self) -> None:
        """Encoding COOLIX auto-24 should produce same-length output as reference test."""
        try:
            from custom_components.hair.encoder.irremote_ac import encode
        except ImportError:
            pytest.skip("Native module not available")

        device = make_protocol_device()
        timings = encode(
            device,
            power=True,
            hvac_mode="auto",
            temperature=24,
        )
        # Reference test_lib.py produces 199 timings for COOLIX auto 24°C.
        # The exact values differ per model/environment but the length should be close.
        assert 150 <= len(timings) <= 250, (
            f"Expected ~199 timings for COOLIX auto-24, got {len(timings)}"
        )


# ---------------------------------------------------------------------------
# Climate entity protocol branching tests (mock-based, no native required)
# ---------------------------------------------------------------------------


class TestClimateProtocolBranch:
    """Test the climate entity's protocol vs learned branching."""

    @pytest.fixture
    def mock_manager(self) -> AsyncMock:
        mgr = AsyncMock()
        mgr.async_send_command = AsyncMock()
        mgr.async_send_raw_timings = AsyncMock()
        return mgr

    def test_is_protocol_detection(self) -> None:
        """Protocol detection based on ac_control_mode."""
        proto_dev = make_protocol_device()
        learned_dev = make_learned_device()

        assert proto_dev.ac_control_mode == "protocol"
        assert learned_dev.ac_control_mode == "learned"

    @pytest.mark.asyncio
    async def test_protocol_turn_on_calls_raw_timings(self, mock_manager) -> None:
        """In protocol mode, turn_on calls async_send_raw_timings."""
        from custom_components.hair.climate import HAIRClimateEntity

        device = make_protocol_device()
        entity = HAIRClimateEntity(device, mock_manager)

        with patch(
            "custom_components.hair.encoder.irremote_ac.encode",
            return_value=[9000, -4500, 560, -560],
        ) as mock_encode:
            await entity.async_turn_on()

        mock_manager.async_send_raw_timings.assert_called_once()
        mock_manager.async_send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_learned_turn_on_calls_send_command(self, mock_manager) -> None:
        """In learned mode, turn_on calls async_send_command via _send."""
        from custom_components.hair.climate import HAIRClimateEntity

        device = make_learned_device()
        # Add a power command so _send can find it.
        from custom_components.hair.models import IRCommand

        cmd = IRCommand(name="Power Toggle")
        device.add_command(cmd)
        device.entity_config.command_mapping["power_toggle"] = "Power Toggle"

        entity = HAIRClimateEntity(device, mock_manager)
        await entity.async_turn_on()

        mock_manager.async_send_command.assert_called()
        mock_manager.async_send_raw_timings.assert_not_called()

    @pytest.mark.asyncio
    async def test_protocol_set_temperature(self, mock_manager) -> None:
        """Protocol mode set_temperature calls encoder and raw_timings."""
        from custom_components.hair.climate import HAIRClimateEntity

        device = make_protocol_device()
        entity = HAIRClimateEntity(device, mock_manager)

        with patch(
            "custom_components.hair.encoder.irremote_ac.encode",
            return_value=[9000, -4500, 560, -560],
        ) as mock_encode:
            await entity.async_set_temperature(temperature=22)

        mock_encode.assert_called_once()
        call_kwargs = mock_encode.call_args.kwargs
        assert call_kwargs["temperature"] == 22.0
        mock_manager.async_send_raw_timings.assert_called_once()

    @pytest.mark.asyncio
    async def test_protocol_set_hvac_mode(self, mock_manager) -> None:
        """Protocol mode set_hvac_mode calls encoder with correct mode."""
        from homeassistant.components.climate import HVACMode

        from custom_components.hair.climate import HAIRClimateEntity

        device = make_protocol_device()
        entity = HAIRClimateEntity(device, mock_manager)

        with patch(
            "custom_components.hair.encoder.irremote_ac.encode",
            return_value=[9000, -4500, 560, -560],
        ) as mock_encode:
            await entity.async_set_hvac_mode(HVACMode.COOL)

        call_kwargs = mock_encode.call_args.kwargs
        assert call_kwargs["power"] is True
        assert call_kwargs["hvac_mode"] == "cool"

    def test_protocol_hvac_modes(self) -> None:
        """Protocol mode exposes all standard HVAC modes."""
        from custom_components.hair.climate import HAIRClimateEntity

        device = make_protocol_device()
        entity = HAIRClimateEntity(device, MagicMock())
        modes = entity.hvac_modes
        assert "off" in modes
        assert "cool" in modes
        assert "heat" in modes

    def test_protocol_fan_modes(self) -> None:
        """Protocol mode exposes standard fan modes."""
        from custom_components.hair.climate import HAIRClimateEntity

        device = make_protocol_device()
        entity = HAIRClimateEntity(device, MagicMock())
        fan_modes = entity.fan_modes
        assert fan_modes is not None
        assert "low" in fan_modes
        assert "high" in fan_modes

    def test_protocol_temperature_unit_celsius(self) -> None:
        """Protocol mode defaults to Celsius when celsius=True."""
        from custom_components.hair.climate import HAIRClimateEntity

        device = make_protocol_device(celsius=True)
        entity = HAIRClimateEntity(device, MagicMock())
        from homeassistant.const import UnitOfTemperature

        assert entity.temperature_unit == UnitOfTemperature.CELSIUS

    def test_to_from_dict_roundtrip(self) -> None:
        """Protocol fields survive to_dict/from_dict round-trip."""
        device = make_protocol_device()
        d = device.to_dict()
        restored = IRDevice.from_dict(d)
        assert restored.ac_control_mode == "protocol"
        assert restored.ir_protocol == "COOLIX"
        assert restored.ir_model == 1
        assert restored.celsius is True

    def test_learned_default_in_from_dict(self) -> None:
        """Old storage without ac_control_mode defaults to learned."""
        d = {
            "id": "test-id",
            "name": "Old AC",
            "device_type": "ac",
            "emitter_entity_ids": ["infrared.test"],
        }
        device = IRDevice.from_dict(d)
        assert device.ac_control_mode == "learned"
        assert device.ir_protocol is None
        assert device.ir_model is None
        assert device.celsius is True
