"""FTMS integration number platform."""

import asyncio
import dataclasses as dc
import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
)
from homeassistant.const import UnitOfPower, UnitOfSpeed
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyftms.client import const as c

from . import FtmsConfigEntry
from .entity import FtmsEntity

EB900B_UART_WRITE_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"

_LOGGER = logging.getLogger(__name__)

_NUMBERS_SENSORS_MAP = {
    c.TARGET_SPEED: c.SPEED_INSTANT,
    c.TARGET_INCLINATION: c.INCLINATION,
    c.TARGET_RESISTANCE: c.RESISTANCE_LEVEL,
    c.TARGET_POWER: c.POWER_INSTANT,
}

_SPEED = NumberEntityDescription(
    key=c.TARGET_SPEED,
    device_class=NumberDeviceClass.SPEED,
    native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
)

_INCLINATION = NumberEntityDescription(
    key=c.TARGET_INCLINATION,
    native_unit_of_measurement="%",
)

_RESISTANCE_LEVEL = NumberEntityDescription(
    key=c.TARGET_RESISTANCE,
)

_POWER = NumberEntityDescription(
    key=c.TARGET_POWER,
    device_class=NumberDeviceClass.POWER,
    native_unit_of_measurement=UnitOfPower.WATT,
)

_ENTITIES = (
    _RESISTANCE_LEVEL,
    _POWER,
    _SPEED,
    _INCLINATION,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FtmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a FTMS number entry."""

    entities, ranges_ = [], entry.runtime_data.ftms.supported_ranges

    for desc in _ENTITIES:
        if range_ := ranges_.get(desc.key):
            entities.append(
                FtmsNumberEntity(
                    entry=entry,
                    description=dc.replace(
                        desc,
                        native_min_value=range_.min_value,
                        native_max_value=range_.max_value,
                        native_step=range_.step,
                    ),
                )
            )

    async_add_entities(entities)


class FtmsNumberEntity(FtmsEntity, NumberEntity):
    """Representation of FTMS numbers.
    
    The EB900 B reports standard FTMS target resistance support, but rejects
    the standard FTMS control point command. Kinomap controls resistance through
    this proprietary UART-like characteristic instead.
    """
    async def _async_eb900b_set_resistance(self, level: int) -> None:
        """Set EB900 B resistance using the proprietary Domyos/Kinomap UART command."""
        level = max(1, min(15, int(level)))
    
        cli = self.ftms._cli
        controller = self.ftms._controller
    
        write_char = None
        for service in cli.services:
            for char in service.characteristics:
                if str(char.uuid).lower() == EB900B_UART_WRITE_UUID:
                    write_char = char
                    break
            if write_char is not None:
                break
    
        if write_char is None:
            raise RuntimeError(
                "EB900 B proprietary UART write characteristic not available"
            )
    
        if not hasattr(self, "_eb900b_sequence"):
            self._eb900b_sequence = 0x1C
        else:
            self._eb900b_sequence = (self._eb900b_sequence + 1) & 0xFF
    
        sequence = self._eb900b_sequence
    
        ad_without_checksum = (
            bytes([0xF0, 0xAD])
            + bytes([0xFF] * 8)
            + bytes([level])
            + bytes([0xFF] * 11)
        )
        ad_checksum = sum(ad_without_checksum) & 0xFF
        ad_full = ad_without_checksum + bytes([ad_checksum])
    
        ad_part_1 = ad_full[:20]
        ad_part_2 = ad_full[20:]
    
        first_payload = (
            bytes([0xF0, 0xCB, 0x03, 0x00, sequence])
            + bytes.fromhex("ff ff ff ff ff ff ff ff ff 01 00 3a 00 01 00")
        )
    
        second_payload_without_checksum = bytes([
            0x01,
            0x00,
            0x01,
            0x00,
            level,
            0x00,
        ])
    
        checksum = sum(first_payload + second_payload_without_checksum) & 0xFF
        second_payload = second_payload_without_checksum + bytes([checksum])
    
        _LOGGER.debug(
            "Setting EB900 B resistance using proprietary command: level=%s sequence=%s",
            level,
            sequence,
        )
    
        async with controller._write_lock:
            await cli.write_gatt_char(write_char, ad_part_1, response=True)
            await asyncio.sleep(0.03)
    
            await cli.write_gatt_char(write_char, ad_part_2, response=True)
            await asyncio.sleep(0.05)
    
            await cli.write_gatt_char(write_char, first_payload, response=True)
            await asyncio.sleep(0.05)
    
            await cli.write_gatt_char(write_char, second_payload, response=True)
        
    async def async_set_native_value(self, value: float) -> None:
        """Update the current value from HA."""
        if self.key == c.TARGET_RESISTANCE:
            level = int(round(value))
    
            try:
                await self._async_eb900b_set_resistance(level)
    
            except Exception:
                _LOGGER.exception(
                    "Failed to set EB900 B resistance using proprietary command: level=%s",
                    level,
                )
                raise
    
            self._attr_native_value = float(level)
            self.async_write_ha_state()
            return
    
        result = await self.ftms.set_setting(self.key, value)
    
        if str(result).lower().endswith("success"):
            self._attr_native_value = value
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        e, key = self.coordinator.data, self.key

        if e.event_id == "update":
            if (key := _NUMBERS_SENSORS_MAP.get(key)) is None:
                return

        elif e.event_id != "setup":
            return

        if (value := e.event_data.get(key)) is not None:
            self._attr_native_value = value
            self.async_write_ha_state()
