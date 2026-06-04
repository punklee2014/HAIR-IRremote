"""Climate entity platform for HAIR (preset-based and protocol-based)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DeviceType
from .models import IRDevice

_LOGGER = logging.getLogger(__name__)

HVAC_MODE_TO_FEATURE: dict[HVACMode, str] = {
    HVACMode.COOL: "mode_cool",
    HVACMode.HEAT: "mode_heat",
    HVACMode.FAN_ONLY: "mode_fan_only",
    HVACMode.DRY: "mode_dry",
    HVACMode.AUTO: "mode_auto",
}

FAN_MODE_TO_FEATURE: dict[str, str] = {
    "low": "fan_low",
    "medium": "fan_medium",
    "high": "fan_high",
    "auto": "fan_auto",
}

PROTOCOL_HVAC_MODES: list[HVACMode] = [
    HVACMode.OFF,
    HVACMode.AUTO,
    HVACMode.COOL,
    HVACMode.HEAT,
    HVACMode.DRY,
    HVACMode.FAN_ONLY,
]

PROTOCOL_FAN_MODES: list[str] = ["auto", "low", "medium", "high"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    device_manager = data["device_manager"]
    factory = data["entity_factory"]

    entities: dict[str, HAIRClimateEntity] = {}

    @callback
    def _on_add(device: IRDevice) -> None:
        if device.device_type != DeviceType.AC:
            return
        if device.id in entities:
            return
        entity = HAIRClimateEntity(device, device_manager)
        entities[device.id] = entity
        async_add_entities([entity])

    @callback
    def _on_remove(device_id: str) -> None:
        entity = entities.pop(device_id, None)
        if entity is not None:
            hass.async_create_task(entity.async_remove())

    @callback
    def _on_update(device: IRDevice) -> None:
        entity = entities.get(device.id)
        if entity is not None:
            entity.update_device(device)

    factory.register_platform_hooks(
        "climate",
        on_add=_on_add,
        on_remove=_on_remove,
        on_update=_on_update,
    )
    factory.register_platform("climate", async_add_entities)

    for device in device_manager.get_all_devices():
        _on_add(device)


class HAIRClimateEntity(ClimateEntity):
    """IR-controlled climate device (preset-based or protocol-based)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_assumed_state = True
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, device: IRDevice, device_manager) -> None:
        self._device = device
        self._manager = device_manager
        self._attr_unique_id = f"hair_{device.id}_climate"
        self._attr_name = None
        self._hvac_mode = HVACMode.OFF
        self._target_temperature: float | None = None
        self._fan_mode: str | None = None
        self._swing_mode: str | None = None
        _LOGGER.info(
            "HAIRClimateEntity created: device=%s ac_control_mode=%s ir_protocol=%s is_protocol=%s",
            device.name,
            getattr(device, 'ac_control_mode', '?'),
            getattr(device, 'ir_protocol', None),
            self._is_protocol,
        )

    # ------------------------------------------------------------------
    # Protocol helper
    # ------------------------------------------------------------------

    @property
    def _is_protocol(self) -> bool:
        return (
            self._device.device_type == DeviceType.AC
            and self._device.ac_control_mode == "protocol"
        )

    # ------------------------------------------------------------------
    # Device info
    # ------------------------------------------------------------------

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._device.id)},
            "name": self._device.name,
            "manufacturer": self._device.manufacturer or "HAIR",
            "model": self._device.model or "Air Conditioner",
        }

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = (
            ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        )
        config = self._device.entity_config

        if self._is_protocol:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
            features |= ClimateEntityFeature.FAN_MODE
            features |= ClimateEntityFeature.SWING_MODE
        else:
            if config.fan_modes:
                features |= ClimateEntityFeature.FAN_MODE
            if config.temperature_presets:
                features |= ClimateEntityFeature.TARGET_TEMPERATURE
            if config.swing_modes:
                features |= ClimateEntityFeature.SWING_MODE

        return features

    @property
    def target_temperature_step(self) -> float:
        return 1.0

    @property
    def precision(self) -> float:
        return 1.0

    @property
    def hvac_modes(self) -> list[HVACMode]:
        if self._is_protocol:
            return list(PROTOCOL_HVAC_MODES)
        modes: list[HVACMode] = [HVACMode.OFF]
        configured = self._device.entity_config.hvac_modes or []
        for raw in configured:
            try:
                mode = HVACMode(raw)
            except ValueError:
                continue
            if mode not in modes:
                modes.append(mode)
        return modes

    @property
    def fan_modes(self) -> list[str] | None:
        if self._is_protocol:
            return list(PROTOCOL_FAN_MODES)
        return list(self._device.entity_config.fan_modes or []) or None

    @property
    def swing_modes(self) -> list[str] | None:
        if self._is_protocol:
            return ["off", "vertical", "horizontal", "both"]
        return list(self._device.entity_config.swing_modes or []) or None

    @property
    def temperature_unit(self) -> str:
        if self._is_protocol:
            return UnitOfTemperature.CELSIUS if self._device.celsius else UnitOfTemperature.FAHRENHEIT
        return UnitOfTemperature.FAHRENHEIT

    @property
    def min_temp(self) -> float:
        if self._is_protocol:
            return 16.0
        presets = self._device.entity_config.temperature_presets
        if presets:
            return float(min(presets))
        return 60.0

    @property
    def max_temp(self) -> float:
        if self._is_protocol:
            return 30.0
        presets = self._device.entity_config.temperature_presets
        if presets:
            return float(max(presets))
        return 86.0

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def hvac_mode(self) -> HVACMode:
        return self._hvac_mode

    @property
    def target_temperature(self) -> float | None:
        return self._target_temperature

    @property
    def fan_mode(self) -> str | None:
        return self._fan_mode

    @property
    def swing_mode(self) -> str | None:
        return self._swing_mode


    # ------------------------------------------------------------------
    # Control — protocol branch
    # ------------------------------------------------------------------

    async def _send_protocol(
        self,
        *,
        power: bool = True,
        hvac_mode: str | None = None,
        temperature: float | None = None,
        fan_mode: str | None = None,
        swing_mode: str | None = None,
    ) -> None:
        mode = hvac_mode or self._hvac_mode or HVACMode.AUTO
        if isinstance(mode, HVACMode):
            mode = mode.value

        temp = temperature if temperature is not None else self._target_temperature

        from functools import partial

        from .encoder.irremote_ac import encode as ac_encode

        _LOGGER.info(
            "Sending protocol AC command: power=%s mode=%s temp=%s fan=%s swing=%s device=%s",
            power, mode, temp, fan_mode, swing_mode, self._device.name,
        )

        encode_fn = partial(
            ac_encode,
            self._device,
            power=power,
            hvac_mode=mode,
            temperature=temp,
            fan_mode=fan_mode or self._fan_mode,
            swing_mode=swing_mode or self._swing_mode,
        )

        try:
            # Run in executor: subprocess.encode is blocking I/O.
            timings = await self._manager._hass.async_add_executor_job(encode_fn)
        except ImportError as err:
            _LOGGER.error(
                "Protocol AC unavailable on %s: %s",
                self._device.name,
                err,
            )
            raise RuntimeError(str(err)) from err
        except Exception as err:
            _LOGGER.exception(
                "Protocol AC encoder crashed on %s (power=%s mode=%s temp=%s)",
                self._device.name, power, mode, temp,
            )
            raise RuntimeError(
                f"Protocol encoder failed: {err}"
            ) from err
        await self._manager.async_send_raw_timings(
            self._device.id, timings
        )

    # ------------------------------------------------------------------
    # HVAC mode
    # ------------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if self._is_protocol:
            if hvac_mode == HVACMode.OFF:
                await self._send_protocol(power=False)
                self._hvac_mode = HVACMode.OFF
            else:
                await self._send_protocol(power=True, hvac_mode=hvac_mode.value)
                self._hvac_mode = hvac_mode
            self.async_write_ha_state()
            return

        # Learned mode (original logic).
        if hvac_mode == HVACMode.OFF:
            await self._send("turn_off", "power_toggle")
            self._hvac_mode = HVACMode.OFF
            self.async_write_ha_state()
            return

        feature = HVAC_MODE_TO_FEATURE.get(hvac_mode)
        if feature and await self._send(feature):
            self._hvac_mode = hvac_mode
            self.async_write_ha_state()
            return
        if await self._send("turn_on", "power_toggle"):
            self._hvac_mode = hvac_mode
            self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Temperature
    # ------------------------------------------------------------------

    async def async_set_temperature(self, **kwargs: Any) -> None:
        target = kwargs.get(ATTR_TEMPERATURE)
        if target is None:
            return

        if self._is_protocol:
            self._target_temperature = float(target)
            await self._send_protocol(temperature=self._target_temperature)
            self.async_write_ha_state()
            return

        # Learned mode (original logic).
        target = float(target)
        presets = self._device.entity_config.temperature_presets or []
        if presets:
            snapped = min(presets, key=lambda t: abs(t - target))
            target = float(snapped)
            await self._send(f"temp_{int(snapped)}")
        self._target_temperature = target
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Fan mode
    # ------------------------------------------------------------------

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if self._is_protocol:
            self._fan_mode = fan_mode
            await self._send_protocol(fan_mode=fan_mode)
            self.async_write_ha_state()
            return

        # Learned mode (original logic).
        feature = FAN_MODE_TO_FEATURE.get(fan_mode.lower())
        if feature and await self._send(feature):
            self._fan_mode = fan_mode
            self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Swing mode
    # ------------------------------------------------------------------

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if self._is_protocol:
            self._swing_mode = swing_mode
            await self._send_protocol(swing_mode=swing_mode)
            self.async_write_ha_state()
            return

        # Learned mode (original logic).
        feature = f"swing_{swing_mode}"
        mapping = self._device.entity_config.command_mapping
        command_name = mapping.get(feature) or mapping.get("swing_toggle")
        if command_name:
            command = self._device.get_command_by_name(command_name)
            if command is not None:
                await self._manager.async_send_command(
                    self._device.id, command.id
                )
                self._swing_mode = swing_mode
                self.async_write_ha_state()
        return self._swing_mode

    # ------------------------------------------------------------------
    # Power on / off
    # ------------------------------------------------------------------

    async def async_turn_on(self) -> None:
        if self._is_protocol:
            await self._send_protocol(power=True)
            if self._hvac_mode == HVACMode.OFF:
                self._hvac_mode = HVACMode.AUTO
            self.async_write_ha_state()
            return

        await self._send("turn_on", "power_toggle")
        if self._hvac_mode == HVACMode.OFF:
            self._hvac_mode = HVACMode.AUTO
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        if self._is_protocol:
            await self._send_protocol(power=False)
            self._hvac_mode = HVACMode.OFF
            self.async_write_ha_state()
            return

        await self._send("turn_off", "power_toggle")
        self._hvac_mode = HVACMode.OFF
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Update from device manager
    # ------------------------------------------------------------------

    @callback
    def update_device(self, device: IRDevice) -> None:
        self._device = device
        self.async_write_ha_state()
        _LOGGER.debug("Climate entity updated: ac_control_mode=%s ir_protocol=%s",
                      getattr(device, 'ac_control_mode', '?'),
                      getattr(device, 'ir_protocol', None))

    # ------------------------------------------------------------------
    # Internal: send learned command by feature key
    # ------------------------------------------------------------------

    async def _send(self, *feature_keys: str) -> bool:
        mapping = self._device.entity_config.command_mapping
        for key in feature_keys:
            command_name = mapping.get(key)
            if command_name is None:
                continue
            command = self._device.get_command_by_name(command_name)
            if command is not None:
                await self._manager.async_send_command(
                    self._device.id, command.id
                )
                return True
        _LOGGER.warning(
            "No mapped IR command on %s for features %s",
            self._device.name,
            feature_keys,
        )
        return False
