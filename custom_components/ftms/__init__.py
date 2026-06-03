"""The FTMS integration."""

from functools import wraps
import asyncio
import logging
from datetime import timedelta

import pyftms
import pyftms.client.client as pyftms_client
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_SENSORS,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .coordinator import DataCoordinator
from .models import FtmsData

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)

RECONNECT_INTERVAL = timedelta(seconds=10)

type FtmsConfigEntry = ConfigEntry[FtmsData]

EB900B_UART_SERVICE_UUID = "49535343-FE7D-4AE5-8FA9-9FAFD205E455"
EB900B_VENDOR_SERVICE_UUID = "02F00000-0000-0000-0000-00000000FE00"

async def async_unload_entry(hass: HomeAssistant, entry: FtmsConfigEntry) -> bool:
    """Unload a config entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.ftms.disconnect()
        bluetooth.async_rediscover_address(hass, entry.runtime_data.ftms.address)

    return unload_ok

def _patch_pyftms_ble_services() -> None:
    """Patch pyftms BLE connection to also discover EB900 B vendor services.

    pyftms normally limits service discovery to FTMS and Device Information.
    EB900 B resistance control uses a proprietary UART-like service, so that
    service must also be discovered by Bleak.
    """
    if getattr(pyftms_client, "_eb900b_services_patch_applied", False):
        return

    original_establish_connection = pyftms_client.establish_connection

    @wraps(original_establish_connection)
    async def _establish_connection_with_eb900b_services(*args, **kwargs):
        services = kwargs.get("services")

        if services is not None:
            merged_services = list(services)
            existing = {str(service).lower() for service in merged_services}

            for service in (
                EB900B_UART_SERVICE_UUID,
                EB900B_VENDOR_SERVICE_UUID,
            ):
                if service.lower() not in existing:
                    merged_services.append(service)

            kwargs["services"] = merged_services

        return await original_establish_connection(*args, **kwargs)

    pyftms_client.establish_connection = _establish_connection_with_eb900b_services
    pyftms_client._eb900b_services_patch_applied = True

async def async_setup_entry(hass: HomeAssistant, entry: FtmsConfigEntry) -> bool:
    """Set up device from a config entry."""
    _patch_pyftms_ble_services()

    address: str = entry.data[CONF_ADDRESS]

    if not (srv_info := bluetooth.async_last_service_info(hass, address)):
        raise ConfigEntryNotReady(translation_key="device_not_found")

    def _on_disconnect(ftms_: pyftms.FitnessMachine) -> None:
        """Disconnect handler.

        Do not reload the config entry when a self-powered machine turns off.
        Mark the entities unavailable and let the reconnect loop bring it back.
        """
        _LOGGER.debug(
            "FTMS device %s disconnected; marking entities unavailable",
            ftms_.address,
        )

        hass.loop.call_soon_threadsafe(
            coordinator.async_set_update_error,
            ConnectionError("FTMS device temporarily unavailable"),
        )

    try:
        ftms = pyftms.get_client(
            srv_info.device,
            srv_info.advertisement,
            on_disconnect=_on_disconnect,
        )

    except pyftms.NotFitnessMachineError:
        raise ConfigEntryNotReady(translation_key="ftms_error")

    coordinator = DataCoordinator(hass, ftms)

    connect_task: asyncio.Task | None = None

    @callback
    def _cancel_connect_task() -> None:
        """Cancel pending reconnect task."""
        nonlocal connect_task

        if connect_task is not None:
            connect_task.cancel()
            connect_task = None

    entry.async_on_unload(_cancel_connect_task)

    async def _async_try_reconnect() -> None:
        """Try to reconnect to the FTMS device."""
        nonlocal connect_task
    
        try:
            if ftms.is_connected:
                return
    
            _LOGGER.debug("Checking if FTMS device %s is available", address)
    
            bluetooth.async_rediscover_address(hass, address)
    
            srv_info = bluetooth.async_last_service_info(hass, address)
            if srv_info is None:
                _LOGGER.debug(
                    "FTMS device %s has no recent BLE advertisement yet",
                    address,
                )
                coordinator.async_set_update_error(
                    ConnectionError("FTMS device not advertising")
                )
                return
    
            ftms.set_ble_device_and_advertisement_data(
                srv_info.device,
                srv_info.advertisement,
            )
    
            _LOGGER.debug("Trying to reconnect to FTMS device %s", address)
            await ftms.connect()
            
            _LOGGER.debug("Reconnected to FTMS device %s", address)
            
            # Clear the coordinator error immediately after a successful reconnect.
            # Without this, entities may remain unavailable until the next FTMS event.
            coordinator.async_set_updated_data(coordinator.data)
    
        except BleakError as exc:
            _LOGGER.debug(
                "FTMS reconnect failed for %s; keeping entities unavailable",
                address,
                exc_info=exc,
            )
            coordinator.async_set_update_error(exc)
    
        except Exception as exc:
            _LOGGER.debug(
                "Unexpected FTMS reconnect error for %s",
                address,
                exc_info=exc,
            )
            coordinator.async_set_update_error(exc)
    
        finally:
            connect_task = None
    
    
    def _schedule_reconnect() -> None:
        """Schedule a reconnect attempt from the Home Assistant event loop."""
        hass.loop.call_soon_threadsafe(_async_schedule_reconnect)
        
    @callback
    def _async_schedule_reconnect() -> None:
        """Schedule a reconnect attempt.
    
        This runs inside the Home Assistant event loop.
        """
        nonlocal connect_task
    
        if ftms.is_connected:
            return
    
        if connect_task is not None:
            return
    
        connect_task = hass.async_create_task(_async_try_reconnect())

    try:
        await ftms.connect()

    except BleakError as exc:
        raise ConfigEntryNotReady(translation_key="connection_failed") from exc

    assert ftms.machine_type.name

    _LOGGER.debug("Device Information: %s", ftms.device_info)
    _LOGGER.debug("Machine type: %s", ftms.machine_type.name)
    _LOGGER.debug("Available sensors: %s", ftms.available_properties)
    _LOGGER.debug("Supported settings: %s", ftms.supported_settings)
    _LOGGER.debug("Supported ranges: %s", ftms.supported_ranges)

    unique_id = "".join(
        x for x in ftms.device_info.get("serial_number", address) if x.isalnum()
    ).lower()

    _LOGGER.debug("Registered new FTMS device. UniqueID is '%s'.", unique_id)

    device_info = dr.DeviceInfo(
        connections={(dr.CONNECTION_BLUETOOTH, ftms.address)},
        identifiers={(DOMAIN, unique_id)},
        translation_key=ftms.machine_type.name.lower(),
        **ftms.device_info,
    )

    entry.runtime_data = FtmsData(
        entry_id=entry.entry_id,
        unique_id=unique_id,
        device_info=device_info,
        ftms=ftms,
        coordinator=coordinator,
        sensors=entry.options[CONF_SENSORS],
    )

    @callback
    def _async_on_ble_event(
        srv_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Update from a BLE callback and reconnect if needed."""
        ftms.set_ble_device_and_advertisement_data(
            srv_info.device,
            srv_info.advertisement,
        )
    
        _LOGGER.debug("BLE advertisement received from FTMS device %s", address)
    
        _schedule_reconnect()
        
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_on_ble_event,
            BluetoothCallbackMatcher(address=address),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )
    
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            lambda now: _schedule_reconnect(),
            RECONNECT_INTERVAL,
        )
    )

    # Platforms initialization
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_entry_update_handler))

    async def _async_hass_stop_handler(event: Event) -> None:
        """Close the connection."""

        await ftms.disconnect()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_hass_stop_handler)
    )

    return True


async def _async_entry_update_handler(
    hass: HomeAssistant, entry: FtmsConfigEntry
) -> None:
    """Options update handler."""

    if entry.options[CONF_SENSORS] != entry.runtime_data.sensors:
        hass.config_entries.async_schedule_reload(entry.entry_id)
