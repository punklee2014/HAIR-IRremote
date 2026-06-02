"""WebSocket API for HAIR frontend communication."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .capture import (
    CaptureProviderType,
    get_available_capture_providers,
    get_capture_provider_for_device,
)
from .capture_orchestrator import (
    CaptureInProgressError,
    CaptureOrchestrator,
)
from .command_templates import get_action_options, get_templates_for_device_type
from .const import (
    DEFAULT_CAPTURE_TIMEOUT,
    DOMAIN,
    WS_PREFIX,
    CaptureState,
    CommandCategory,
    DeviceType,
    normalize_device_type,
)
from .device_manager import DeviceManager, category_for_command_name
from .models import IRDevice, IRTrigger
from .signal_monitor import SignalMonitor
from .signal_store import SignalStore
from .trigger_manager import TriggerManager

_LOGGER = logging.getLogger(__name__)


def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register all WebSocket commands.

    Idempotent — registering the same command twice is harmless because
    we guard with a hass.data flag.
    """
    if hass.data.get(f"{DOMAIN}_ws_registered"):
        return
    hass.data[f"{DOMAIN}_ws_registered"] = True

    websocket_api.async_register_command(hass, ws_get_devices)
    websocket_api.async_register_command(hass, ws_get_device)
    websocket_api.async_register_command(hass, ws_create_device)
    websocket_api.async_register_command(hass, ws_update_device)
    websocket_api.async_register_command(hass, ws_delete_device)
    websocket_api.async_register_command(hass, ws_delete_command)
    websocket_api.async_register_command(hass, ws_send_command)
    websocket_api.async_register_command(hass, ws_start_capture)
    websocket_api.async_register_command(hass, ws_cancel_capture)
    websocket_api.async_register_command(hass, ws_save_captured_command)
    websocket_api.async_register_command(hass, ws_get_command_templates)
    websocket_api.async_register_command(hass, ws_get_capture_providers)

    # Protocol AC
    websocket_api.async_register_command(hass, ws_get_protocols)
    websocket_api.async_register_command(hass, ws_get_protocol_models)

    # Signal Monitor (unknown devices)
    websocket_api.async_register_command(hass, ws_get_unknown_devices)
    websocket_api.async_register_command(hass, ws_get_unknown_device)
    websocket_api.async_register_command(hass, ws_dismiss_unknown)
    websocket_api.async_register_command(hass, ws_undismiss_unknown)
    websocket_api.async_register_command(hass, ws_assign_signal)
    websocket_api.async_register_command(hass, ws_assign_new_device)
    websocket_api.async_register_command(hass, ws_delete_signal)
    websocket_api.async_register_command(hass, ws_test_signal)
    websocket_api.async_register_command(hass, ws_rename_unknown)
    websocket_api.async_register_command(hass, ws_clear_unknowns)

    # Action mapping
    websocket_api.async_register_command(hass, ws_get_action_options)
    websocket_api.async_register_command(hass, ws_update_mapping)

    # Triggers
    websocket_api.async_register_command(hass, ws_get_triggers)
    websocket_api.async_register_command(hass, ws_create_trigger)
    websocket_api.async_register_command(hass, ws_update_trigger)
    websocket_api.async_register_command(hass, ws_delete_trigger)
    websocket_api.async_register_command(hass, ws_subscribe_triggers)


def _protocol_encoder_available(data: dict[str, Any]) -> bool:
    return bool(data.get("protocol_encoder_available"))


def _protocol_encoder_error_message(data: dict[str, Any]) -> str:
    detail = data.get("protocol_encoder_error") or "unknown error"
    return (
        "Protocol AC encoder is not available on this Home Assistant host. "
        f"{detail}"
    )


def _get_first_entry_data(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return the first entry's hass.data for HAIR.

    HAIR is a hub integration with at most one entry per HA instance.
    """
    entries = hass.data.get(DOMAIN, {})
    for value in entries.values():
        if isinstance(value, dict) and "device_manager" in value:
            return value
    return None


def _device_summary(device: IRDevice, hass: HomeAssistant) -> dict[str, Any]:
    return {
        "id": device.id,
        "name": device.name,
        "device_type": str(device.device_type),
        "manufacturer": device.manufacturer,
        "model": device.model,
        "emitter_entity_ids": list(device.emitter_entity_ids),
        "command_count": len(device.commands),
        "created_at": device.created_at,
        "updated_at": device.updated_at,
        "ac_control_mode": device.ac_control_mode,
        "ir_protocol": device.ir_protocol,
    }


def _device_full(device: IRDevice) -> dict[str, Any]:
    full = device.to_dict()
    full["command_count"] = len(device.commands)
    return full


# --- Device Operations ---

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/devices",
})
@websocket_api.async_response
async def ws_get_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_result(msg["id"], [])
        return
    manager: DeviceManager = data["device_manager"]
    devices = [_device_summary(d, hass) for d in manager.get_all_devices()]
    connection.send_result(msg["id"], devices)


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/device",
    vol.Required("device_id"): str,
})
@websocket_api.async_response
async def ws_get_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_found", "HAIR not configured")
        return
    manager: DeviceManager = data["device_manager"]
    device = manager.get_device(msg["device_id"])
    if device is None:
        connection.send_error(msg["id"], "not_found", "Device not found")
        return
    connection.send_result(msg["id"], _device_full(device))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/device/create",
    vol.Required("name"): str,
    vol.Required("device_type"): str,
    vol.Required("emitter_entity_ids"): [str],
    vol.Optional("manufacturer"): vol.Any(str, None),
    vol.Optional("model"): vol.Any(str, None),
    vol.Optional("capture_device_id"): vol.Any(str, None),
    vol.Optional("capture_provider_type"): str,
    vol.Optional("ac_control_mode"): str,
    vol.Optional("ir_protocol"): vol.Any(str, None),
    vol.Optional("ir_model"): vol.Any(int, None),
    vol.Optional("celsius"): bool,
})
@websocket_api.async_response
async def ws_create_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    manager: DeviceManager = data["device_manager"]

    try:
        device_type = normalize_device_type(msg["device_type"])
    except ValueError:
        connection.send_error(msg["id"], "invalid_format", "Unknown device_type")
        return

    provider_value = msg.get("capture_provider_type") or CaptureProviderType.ESPHOME
    try:
        provider_type = CaptureProviderType(provider_value)
    except ValueError:
        connection.send_error(
            msg["id"], "invalid_format", "Unknown capture_provider_type"
        )
        return

    ac_mode = msg.get("ac_control_mode", "learned")
    if (
        device_type == DeviceType.AC
        and ac_mode == "protocol"
        and not _protocol_encoder_available(data)
    ):
        connection.send_error(
            msg["id"],
            "protocol_unavailable",
            _protocol_encoder_error_message(data),
        )
        return

    device = IRDevice(
        name=msg["name"],
        device_type=device_type,
        manufacturer=msg.get("manufacturer"),
        model=msg.get("model"),
        emitter_entity_ids=list(msg["emitter_entity_ids"]),
        capture_device_id=msg.get("capture_device_id"),
        capture_provider_type=provider_type,
        ac_control_mode=ac_mode,
        ir_protocol=msg.get("ir_protocol"),
        ir_model=msg.get("ir_model"),
        celsius=msg.get("celsius", True),
    )
    await manager.async_create_device(device)
    connection.send_result(msg["id"], _device_full(device))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/device/update",
    vol.Required("device_id"): str,
    vol.Optional("name"): str,
    vol.Optional("manufacturer"): vol.Any(str, None),
    vol.Optional("model"): vol.Any(str, None),
    vol.Optional("emitter_entity_ids"): [str],
    vol.Optional("device_type"): str,
    vol.Optional("ac_control_mode"): str,
    vol.Optional("ir_protocol"): vol.Any(str, None),
    vol.Optional("ir_model"): vol.Any(int, None),
    vol.Optional("celsius"): bool,
})
@websocket_api.async_response
async def ws_update_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    manager: DeviceManager = data["device_manager"]
    device = manager.get_device(msg["device_id"])
    if device is None:
        connection.send_error(msg["id"], "not_found", "Device not found")
        return

    if "name" in msg:
        device.name = msg["name"]
    if "manufacturer" in msg:
        device.manufacturer = msg["manufacturer"]
    if "model" in msg:
        device.model = msg["model"]
    if "emitter_entity_ids" in msg:
        device.emitter_entity_ids = list(msg["emitter_entity_ids"])
    if "device_type" in msg:
        try:
            device.device_type = normalize_device_type(msg["device_type"])
        except ValueError:
            connection.send_error(
                msg["id"], "invalid_format", "Unknown device_type"
            )
            return
    if "ac_control_mode" in msg:
        device.ac_control_mode = msg["ac_control_mode"]
    if "ir_protocol" in msg:
        device.ir_protocol = msg["ir_protocol"]
    if "ir_model" in msg:
        device.ir_model = msg["ir_model"]
    if "celsius" in msg:
        device.celsius = bool(msg["celsius"])

    await manager.async_update_device(device)
    connection.send_result(msg["id"], _device_full(device))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/device/delete",
    vol.Required("device_id"): str,
})
@websocket_api.async_response
async def ws_delete_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    manager: DeviceManager = data["device_manager"]
    removed = await manager.async_remove_device(msg["device_id"])
    if not removed:
        connection.send_error(msg["id"], "not_found", "Device not found")
        return
    connection.send_result(msg["id"], {"removed": True})


# --- Command Operations ---

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/command/send",
    vol.Required("device_id"): str,
    vol.Required("command_id"): str,
})
@websocket_api.async_response
async def ws_send_command(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    manager: DeviceManager = data["device_manager"]
    try:
        await manager.async_send_command(msg["device_id"], msg["command_id"])
    except KeyError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    except Exception as err:
        _LOGGER.error("Send command failed: %s", err, exc_info=True)
        connection.send_error(msg["id"], "send_failed", str(err))
        return
    connection.send_result(msg["id"], {"sent": True})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/command/delete",
    vol.Required("device_id"): str,
    vol.Required("command_id"): str,
})
@websocket_api.async_response
async def ws_delete_command(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    manager: DeviceManager = data["device_manager"]
    removed = await manager.async_remove_command(
        msg["device_id"], msg["command_id"]
    )
    if not removed:
        connection.send_error(msg["id"], "not_found", "Command not found")
        return
    connection.send_result(msg["id"], {"removed": True})


# --- Capture Operations (with event streaming) ---

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/capture/start",
    vol.Required("device_id"): str,
    vol.Optional("timeout", default=DEFAULT_CAPTURE_TIMEOUT): int,
})
@websocket_api.async_response
async def ws_start_capture(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Start IR capture and stream events to the client.

    The handler responds with the session_id immediately and then sends
    further messages as ``event``-typed pushes:
    - capture_listening
    - capture_received   { result, duplicate_of? }
    - capture_timeout
    - capture_error      { error }
    - capture_cancelled
    """
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return

    manager: DeviceManager = data["device_manager"]
    orchestrator: CaptureOrchestrator = data["orchestrator"]
    device = manager.get_device(msg["device_id"])
    if device is None:
        connection.send_error(msg["id"], "not_found", "Device not found")
        return

    capture_device_id = device.capture_device_id
    if capture_device_id is None:
        connection.send_error(
            msg["id"],
            "no_capture_device",
            "Device has no capture hardware configured",
        )
        return

    provider = await get_capture_provider_for_device(
        hass, device.capture_provider_type, capture_device_id
    )
    if provider is None:
        connection.send_error(
            msg["id"],
            "provider_unavailable",
            "Capture provider not available",
        )
        return

    msg_id = msg["id"]
    timeout = msg.get("timeout", DEFAULT_CAPTURE_TIMEOUT)

    try:
        session = await orchestrator.start_capture(
            provider, device.id, timeout=timeout
        )
    except CaptureInProgressError as err:
        connection.send_error(msg_id, "in_progress", str(err))
        return
    except Exception as err:
        connection.send_error(msg_id, "capture_failed", str(err))
        return

    # Subscribe to capture events and forward them as pushed events.
    @callback
    def _on_event(state: CaptureState, result) -> None:
        payload: dict[str, Any]
        if state == CaptureState.LISTENING:
            payload = {"type": "capture_listening"}
        elif state == CaptureState.CAPTURED and result is not None:
            duplicate = orchestrator.check_duplicate(device, result)
            payload = {
                "type": "capture_received",
                "result": result.to_dict(),
            }
            if duplicate is not None:
                payload["duplicate_of"] = {
                    "id": duplicate.id,
                    "name": duplicate.name,
                }
        elif state == CaptureState.TIMEOUT:
            payload = {"type": "capture_timeout"}
        elif state == CaptureState.ERROR:
            payload = {"type": "capture_error", "error": "Capture failed"}
        elif state == CaptureState.CANCELLED:
            payload = {"type": "capture_cancelled"}
        else:
            return
        connection.send_event(msg_id, payload)

    unsubscribe = orchestrator.subscribe(session.session_id, _on_event)
    connection.subscriptions[msg_id] = unsubscribe

    # Acknowledge the subscription with the session id so the client
    # can later cancel/save against it.
    connection.send_result(
        msg_id,
        {
            "session_id": session.session_id,
            "device_id": device.id,
            "timeout": timeout,
        },
    )

    # Re-emit the listening state so clients that subscribed after the
    # initial dispatch still see it.
    connection.send_event(msg_id, {"type": "capture_listening"})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/capture/cancel",
    vol.Required("session_id"): str,
})
@websocket_api.async_response
async def ws_cancel_capture(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    orchestrator: CaptureOrchestrator = data["orchestrator"]
    await orchestrator.cancel_capture(msg["session_id"])
    connection.send_result(msg["id"], {"cancelled": True})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/capture/save",
    vol.Required("device_id"): str,
    vol.Required("session_id"): str,
    vol.Required("command_name"): str,
    vol.Optional("command_category"): str,
})
@websocket_api.async_response
async def ws_save_captured_command(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    manager: DeviceManager = data["device_manager"]
    orchestrator: CaptureOrchestrator = data["orchestrator"]

    device = manager.get_device(msg["device_id"])
    if device is None:
        connection.send_error(msg["id"], "not_found", "Device not found")
        return

    result = orchestrator.get_session_result(msg["session_id"])
    if result is None:
        connection.send_error(
            msg["id"], "no_capture", "No captured signal for that session"
        )
        return

    category_value = msg.get("command_category")
    if category_value:
        try:
            category = CommandCategory(category_value)
        except ValueError:
            category = category_for_command_name(msg["command_name"])
    else:
        category = category_for_command_name(msg["command_name"])

    command = result.to_command(msg["command_name"], category)
    await manager.async_add_command(device.id, command)
    connection.send_result(msg["id"], command.to_dict())


# --- Template & Provider Info ---

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/templates",
    vol.Required("device_type"): str,
})
@websocket_api.async_response
async def ws_get_command_templates(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    templates = get_templates_for_device_type(msg["device_type"])
    connection.send_result(
        msg["id"], [t.to_dict() for t in templates]
    )


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/capture/providers",
})
@websocket_api.async_response
async def ws_get_capture_providers(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    providers = await get_available_capture_providers(hass)
    connection.send_result(msg["id"], providers)


# --- Protocol AC ---


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/protocols",
})
@websocket_api.async_response
async def ws_get_protocols(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the available IR protocols for protocol-based AC control."""
    try:
        from .encoder.irremote_ac import get_supported_protocols

        protocols = get_supported_protocols()
    except ImportError:
        # Native module not available; return a hardcoded common subset.
        protocols = [
            "ARGO", "COOLIX", "DAIKIN", "DAIKIN128", "DAIKIN152", "DAIKIN160",
            "DAIKIN176", "DAIKIN2", "DAIKIN216", "DAIKIN312", "DAIKIN64",
            "ELECTRA_AC", "FUJITSU_AC", "GOODWEATHER", "GREE", "HAIER_AC",
            "HAIER_AC176", "HAIER_AC_YRW02", "HITACHI_AC", "HITACHI_AC1",
            "HITACHI_AC264", "HITACHI_AC296", "HITACHI_AC344", "HITACHI_AC424",
            "KELVINATOR", "MIDEA", "MITSUBISHI_AC", "MITSUBISHI136",
            "MITSUBISHI152", "MITSUBISHI_AC", "NEOCLIMA", "PANASONIC_AC",
            "SAMSUNG_AC", "SANYO_AC", "SHARP_AC", "TCL96AC", "TECHNIBEL_AC",
            "TOSHIBA_AC", "TRANSCOLD", "TROTEC", "VESTEL_AC", "VOLTAS",
            "WHIRLPOOL_AC",
        ]

    connection.send_result(msg["id"], {"protocols": protocols})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/protocol/models",
    vol.Optional("protocol"): str,
})
@websocket_api.async_response
async def ws_get_protocol_models(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return model enum values for one or all protocols."""
    from .encoder.irremote_ac import get_protocol_models

    proto = msg.get("protocol")
    models = get_protocol_models(proto)
    connection.send_result(msg["id"], {"models": models})


# --- Signal Monitor (Unknown Devices) ---


def _unknown_device_summary(device) -> dict[str, Any]:
    """Build a summary dict for an unknown device."""
    return {
        "id": device.id,
        "fingerprint": device.fingerprint,
        "protocol": device.protocol,
        "device_address": device.device_address,
        "label": device.label,
        "signal_count": len(device.signals),
        "hit_count": device.hit_count,
        "first_seen": device.first_seen,
        "last_seen": device.last_seen,
        "dismissed": device.dismissed,
    }


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/unknown/devices",
    vol.Optional("include_dismissed", default=False): bool,
    vol.Optional("min_hits"): vol.Any(int, None),
})
@websocket_api.async_response
async def ws_get_unknown_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return unknown devices sorted by activity."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_result(msg["id"], [])
        return
    monitor: SignalMonitor = data["signal_monitor"]
    devices = monitor.get_unknown_devices(
        include_dismissed=msg.get("include_dismissed", False),
        min_hits=msg.get("min_hits"),
    )
    connection.send_result(
        msg["id"], [_unknown_device_summary(d) for d in devices]
    )


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/unknown/device",
    vol.Required("device_id"): str,
})
@websocket_api.async_response
async def ws_get_unknown_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return a single unknown device with all its signals."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    monitor: SignalMonitor = data["signal_monitor"]
    device = monitor.get_unknown_device(msg["device_id"])
    if device is None:
        connection.send_error(msg["id"], "not_found", "Unknown device not found")
        return
    connection.send_result(msg["id"], device.to_dict())


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/unknown/dismiss",
    vol.Required("device_id"): str,
})
@websocket_api.async_response
async def ws_dismiss_unknown(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Dismiss an unknown device (hide from list)."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    monitor: SignalMonitor = data["signal_monitor"]
    if not monitor.dismiss_device(msg["device_id"]):
        connection.send_error(msg["id"], "not_found", "Unknown device not found")
        return
    connection.send_result(msg["id"], {"dismissed": True})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/unknown/undismiss",
    vol.Required("device_id"): str,
})
@websocket_api.async_response
async def ws_undismiss_unknown(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Restore a dismissed unknown device."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    monitor: SignalMonitor = data["signal_monitor"]
    if not monitor.undismiss_device(msg["device_id"]):
        connection.send_error(msg["id"], "not_found", "Unknown device not found")
        return
    connection.send_result(msg["id"], {"undismissed": True})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/unknown/assign",
    vol.Required("device_id"): str,
    vol.Required("signal_fingerprint"): str,
    vol.Required("hair_device_id"): str,
    vol.Required("command_name"): str,
    vol.Optional("command_category", default="custom"): str,
})
@websocket_api.async_response
async def ws_assign_signal(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Assign an unknown signal as a named command on a HAIR device."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    monitor: SignalMonitor = data["signal_monitor"]
    result = await monitor.assign_signal(
        msg["device_id"],
        msg["signal_fingerprint"],
        msg["hair_device_id"],
        msg["command_name"],
        msg.get("command_category", "custom"),
    )
    if not result["success"]:
        connection.send_error(
            msg["id"],
            result.get("code", "assign_failed"),
            result.get("error", "Assign failed"),
        )
        return
    connection.send_result(msg["id"], {
        "assigned": True,
        "command_id": result["command_id"],
    })


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/unknown/test",
    vol.Required("signal_fingerprint"): str,
    vol.Optional("emitter_entity_id"): str,
})
@websocket_api.async_response
async def ws_test_signal(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Send an unknown signal through an emitter for verification."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return

    emitter_id = msg.get("emitter_entity_id")
    if not emitter_id:
        # Default to the first emitter configured on any HAIR device.
        store = data["store"]
        for dev in store.get_all_devices():
            if dev.emitter_entity_ids:
                emitter_id = dev.emitter_entity_ids[0]
                break
    if not emitter_id:
        connection.send_error(msg["id"], "no_emitter", "No emitter entity configured")
        return

    monitor: SignalMonitor = data["signal_monitor"]
    result = await monitor.test_signal(
        msg["signal_fingerprint"], emitter_id
    )
    if not result["success"]:
        connection.send_error(
            msg["id"],
            result.get("code", "test_failed"),
            result.get("error", "Test failed"),
        )
        return
    connection.send_result(msg["id"], {"sent": True})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/unknown/rename",
    vol.Required("device_id"): str,
    vol.Required("label"): str,
})
@websocket_api.async_response
async def ws_rename_unknown(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Rename an unknown device with a user-friendly label."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    signal_store: SignalStore = data["signal_store"]
    device = signal_store.get_device(msg["device_id"])
    if device is None:
        connection.send_error(msg["id"], "not_found", "Unknown device not found")
        return
    label = msg["label"].strip()
    device.label = label if label else None
    await signal_store.async_save()
    connection.send_result(msg["id"], {"label": device.label})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/unknown/assign-new-device",
    vol.Required("device_id"): str,
    vol.Required("signal_fingerprint"): str,
    vol.Required("device_name"): str,
    vol.Required("device_type"): str,
    vol.Required("emitter_entity_ids"): [str],
    vol.Required("command_name"): str,
    vol.Optional("command_category", default="custom"): str,
})
@websocket_api.async_response
async def ws_assign_new_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Create a new HAIR device and assign an unknown signal atomically."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    monitor: SignalMonitor = data["signal_monitor"]
    result = await monitor.assign_to_new_device(
        msg["device_id"],
        msg["signal_fingerprint"],
        msg["device_name"],
        msg["device_type"],
        list(msg["emitter_entity_ids"]),
        msg["command_name"],
        msg.get("command_category", "custom"),
    )
    if not result["success"]:
        connection.send_error(
            msg["id"],
            result.get("code", "assign_failed"),
            result.get("error", "Assign failed"),
        )
        return

    # Register HA device + entities now that both stores are persisted.
    device_mgr = data["device_manager"]
    new_device = result["device"]
    device_mgr._register_ha_device(new_device)
    await device_mgr._entity_factory.async_create_entities(new_device)

    connection.send_result(msg["id"], {
        "assigned": True,
        "device_id": result["device_id"],
        "command_id": result["command_id"],
    })


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/unknown/signal/delete",
    vol.Required("device_id"): str,
    vol.Required("signal_fingerprint"): str,
})
@websocket_api.async_response
async def ws_delete_signal(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a single unknown signal."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    monitor: SignalMonitor = data["signal_monitor"]
    result = await monitor.delete_signal(
        msg["device_id"], msg["signal_fingerprint"]
    )
    if not result["success"]:
        connection.send_error(
            msg["id"],
            result.get("code", "delete_failed"),
            result.get("error", "Delete failed"),
        )
        return
    connection.send_result(msg["id"], {
        "deleted": True,
        "device_removed": result.get("device_removed", False),
    })


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/unknown/clear",
})
@websocket_api.async_response
async def ws_clear_unknowns(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Wipe all unknown signals."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    monitor: SignalMonitor = data["signal_monitor"]
    monitor.clear_all()
    connection.send_result(msg["id"], {"cleared": True})


# --- Action Mapping ---


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/device/action-options",
    vol.Required("device_type"): str,
})
@websocket_api.async_response
async def ws_get_action_options(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the canonical action options for a device type."""
    options = get_action_options(msg["device_type"])
    connection.send_result(msg["id"], options)


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/device/update-mapping",
    vol.Required("device_id"): str,
    vol.Required("command_name"): str,
    vol.Optional("action_key"): vol.Any(str, None),
})
@websocket_api.async_response
async def ws_update_mapping(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Set or clear the action mapping for a command on a device.

    If ``action_key`` is provided, maps the command to that action.
    If ``action_key`` is None or absent, clears any existing mapping
    for that command.
    """
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    manager: DeviceManager = data["device_manager"]
    device = manager.get_device(msg["device_id"])
    if device is None:
        connection.send_error(msg["id"], "not_found", "Device not found")
        return

    command_name = msg["command_name"]
    action_key = msg.get("action_key")
    mapping = device.entity_config.command_mapping

    # Clear any existing mapping that points to this command.
    for key, value in list(mapping.items()):
        if value.casefold() == command_name.casefold():
            del mapping[key]

    # If a new action_key is provided, also clear whatever was
    # previously mapped to that key (reassignment).
    if action_key:
        mapping.pop(action_key, None)
        mapping[action_key] = command_name

    await manager.async_update_device(device)
    connection.send_result(msg["id"], {
        "mapping": dict(mapping),
    })


# --- Triggers ---


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/triggers",
})
@websocket_api.async_response
async def ws_get_triggers(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return all triggers."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_result(msg["id"], [])
        return
    store = data["store"]
    triggers = store.get_all_triggers()
    connection.send_result(msg["id"], [t.to_dict() for t in triggers])


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/trigger/create",
    vol.Required("name"): str,
    vol.Optional("signal_fingerprint", default=""): str,
    vol.Optional("protocol"): vol.Any(str, None),
    vol.Optional("code"): vol.Any(str, None),
    vol.Optional("min_hits", default=1): int,
    vol.Optional("source_device_id"): vol.Any(str, None),
    vol.Optional("source_command_id"): vol.Any(str, None),
})
@websocket_api.async_response
async def ws_create_trigger(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Create a new trigger."""
    from .event_parser import EventParser

    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    store = data["store"]

    sig_fp = msg.get("signal_fingerprint", "")
    protocol = msg.get("protocol")
    code = msg.get("code")

    # Auto-compute fingerprint from protocol+code when not provided.
    if not sig_fp and (protocol or code):
        sig_fp = EventParser.signal_fingerprint(protocol, code, None)

    # If a source command was given, derive fingerprint from that command.
    if not sig_fp and msg.get("source_command_id") and msg.get("source_device_id"):
        dm = data.get("device_manager")
        if dm:
            device = dm.get_device(msg["source_device_id"])
            if device:
                cmd = device.get_command(msg["source_command_id"])
                if cmd:
                    sig_fp = EventParser.signal_fingerprint(
                        cmd.protocol, cmd.code, cmd.raw_timings,
                    )
                    if not protocol:
                        protocol = cmd.protocol
                    if not code:
                        code = cmd.code

    if not sig_fp:
        connection.send_error(
            msg["id"], "missing_fingerprint",
            "Cannot compute signal fingerprint. Provide signal_fingerprint, "
            "protocol+code, or source_device_id+source_command_id."
        )
        return

    # Reject duplicate fingerprint.
    existing = store.get_trigger_by_fingerprint(sig_fp)
    if existing is not None:
        connection.send_error(
            msg["id"], "duplicate",
            f"A trigger already exists for this signal: {existing.name}"
        )
        return

    trigger = IRTrigger(
        name=msg["name"],
        signal_fingerprint=sig_fp,
        protocol=protocol,
        code=code,
        min_hits=msg.get("min_hits", 1),
        source_device_id=msg.get("source_device_id"),
        source_command_id=msg.get("source_command_id"),
    )
    store.add_trigger(trigger)
    await store.async_save()

    # Create the event entity.
    from .event import sync_trigger_entities

    entry_id = data["config_entry"].entry_id
    sync_trigger_entities(hass, entry_id, trigger=trigger)

    connection.send_result(msg["id"], trigger.to_dict())


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/trigger/update",
    vol.Required("trigger_id"): str,
    vol.Optional("name"): str,
    vol.Optional("min_hits"): int,
    vol.Optional("enabled"): bool,
})
@websocket_api.async_response
async def ws_update_trigger(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Update a trigger's name, min_hits, or enabled state."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    store = data["store"]
    trigger = store.get_trigger(msg["trigger_id"])
    if trigger is None:
        connection.send_error(msg["id"], "not_found", "Trigger not found")
        return

    from datetime import UTC, datetime

    if "name" in msg:
        trigger.name = msg["name"]
    if "min_hits" in msg:
        trigger.min_hits = max(1, msg["min_hits"])
    if "enabled" in msg:
        trigger.enabled = msg["enabled"]
    trigger.updated_at = datetime.now(UTC).isoformat()

    store.update_trigger(trigger)
    await store.async_save()

    # Update event entity name if changed.
    entities = data.get("_trigger_entities", {})
    entity = entities.get(trigger.id)
    if entity is not None:
        entity.update_trigger(trigger)

    connection.send_result(msg["id"], trigger.to_dict())


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/trigger/delete",
    vol.Required("trigger_id"): str,
})
@websocket_api.async_response
async def ws_delete_trigger(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a trigger."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return
    store = data["store"]
    removed = store.remove_trigger(msg["trigger_id"])
    if not removed:
        connection.send_error(msg["id"], "not_found", "Trigger not found")
        return
    await store.async_save()

    # Remove the event entity.
    from .event import sync_trigger_entities

    entry_id = data["config_entry"].entry_id
    sync_trigger_entities(hass, entry_id, removed_id=msg["trigger_id"])

    connection.send_result(msg["id"], {"removed": True})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{WS_PREFIX}/trigger/subscribe",
})
@websocket_api.async_response
async def ws_subscribe_triggers(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe to real-time trigger fire events (for card glow)."""
    data = _get_first_entry_data(hass)
    if data is None:
        connection.send_error(msg["id"], "not_configured", "HAIR not configured")
        return

    trigger_manager: TriggerManager = data["trigger_manager"]

    @callback
    def _on_trigger_fired(event_data: dict[str, Any]) -> None:
        connection.send_event(msg["id"], {
            "type": "trigger_fired",
            **event_data,
        })

    trigger_manager.subscribe(_on_trigger_fired)

    @callback
    def _on_disconnect() -> None:
        trigger_manager.unsubscribe(_on_trigger_fired)

    connection.subscriptions[msg["id"]] = _on_disconnect
    connection.send_result(msg["id"], {"subscribed": True})
