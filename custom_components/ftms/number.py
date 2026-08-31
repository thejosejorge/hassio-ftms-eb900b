"""FTMS integration number platform."""

import asyncio
import dataclasses as dc
import logging
import time

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
EB900B_UART_NOTIFY_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"

# QZ/ChangYow keeps the proprietary session alive on a ~300 ms cycle.
EB900B_KEEPALIVE_SECONDS = 0.30

# Allow the physical resistance roughly 2.5 s to reach the requested target.
# If it did not, resend the resistance command once.
EB900B_VERIFY_SECONDS = 2.50

# How long to prefer fresh proprietary UART resistance telemetry over FTMS.
EB900B_UART_FRESH_SECONDS = 1.50

EB900B_KEEPALIVE_PACKET = bytes.fromhex("f0 ac 9c")
EB900B_STATUS_PACKET_PREFIXES = (b"\xf0\xbc", b"\xf0\xdb", b"\xf0\xdd")
EB900B_INIT_PACKETS = (
    bytes.fromhex("f0 c8 01 b9"),
    bytes.fromhex("f0 c9 b9"),
    bytes.fromhex("f0 a3 93"),
    bytes.fromhex("f0 a4 94"),
    bytes.fromhex("f0 a5 95"),
    bytes.fromhex("f0 ab 9b"),
    bytes.fromhex("f0 c4 03 b7"),
    bytes.fromhex(
        "f0 ad ff ff ff ff ff ff ff ff "
        "ff ff ff ff ff ff ff ff 01 ff ff ff 8b"
    ),
    bytes.fromhex(
        "f0 cb 02 00 08 ff ff ff ff ff "
        "ff ff ff ff ff ff ff ff 01 00 00 01 ff ff ff ff b6"
    ),
    bytes.fromhex(
        "f0 ad ff ff 00 05 ff ff ff ff "
        "ff ff ff 00 00 ff ff ff 01 ff ff ff 94"
    ),
)

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
    the standard FTMS Control Point command with INVALID_PARAMETER.

    Resistance control therefore uses the proprietary Domyos/ChangYow UART
    service.  A QZ-style initialisation plus keep-alive makes the physical
    resistance react in roughly 1-2 seconds instead of the ~8 second behaviour
    seen when an isolated F0 AD resistance packet is sent.
    """

    def _eb900b_find_uart_characteristics(self):
        """Return the proprietary UART write and notify characteristics."""
        cli = self.ftms._cli
        write_char = None
        notify_char = None

        for service in cli.services:
            for char in service.characteristics:
                uuid = str(char.uuid).lower()

                if uuid == EB900B_UART_WRITE_UUID:
                    write_char = char
                elif uuid == EB900B_UART_NOTIFY_UUID:
                    notify_char = char

        return write_char, notify_char

    @callback
    def _eb900b_uart_notify(self, sender, data) -> None:
        """Handle proprietary Domyos UART resistance notifications."""
        del sender
        payload = bytes(data)

        # Some bikes split a 26-byte status packet into 20 + 6 bytes.
        partial = getattr(self, "_eb900b_uart_partial", b"")

        if partial:
            payload = partial + payload
            self._eb900b_uart_partial = b""
        elif len(payload) == 20 and payload[:2] in EB900B_STATUS_PACKET_PREFIXES:
            self._eb900b_uart_partial = payload
            return

        if len(payload) != 26:
            return

        # QZ decodes resistance from byte 14 in the proprietary status packet.
        resistance = payload[14]
        if 1 <= resistance <= 15:
            self._eb900b_uart_actual_resistance = resistance
            self._eb900b_uart_actual_time = time.monotonic()

    async def _async_eb900b_write_packet(
        self,
        write_char,
        packet: bytes,
    ) -> None:
        """Write one logical proprietary packet, splitting it at 20 bytes."""
        cli = self.ftms._cli
        controller = self.ftms._controller

        async with controller._write_lock:
            for offset in range(0, len(packet), 20):
                chunk = packet[offset : offset + 20]

                await cli.write_gatt_char(
                    write_char,
                    chunk,
                    response=False,
                )

                if offset + 20 < len(packet):
                    await asyncio.sleep(0.03)

    async def _async_eb900b_keepalive(self, cli, write_char) -> None:
        """Keep the proprietary ChangYow session alive while connected."""
        try:
            while True:
                await asyncio.sleep(EB900B_KEEPALIVE_SECONDS)

                if not getattr(cli, "is_connected", True):
                    raise RuntimeError("BLE client disconnected")

                await self._async_eb900b_write_packet(
                    write_char, EB900B_KEEPALIVE_PACKET
                )

        except asyncio.CancelledError:
            raise
        except Exception as err:  # Connection loss should simply reset session state.
            _LOGGER.debug("EB900 B UART keep-alive stopped: %s", err)
        finally:
            self._eb900b_qz_initialized = False
            self._eb900b_notify_started = False
            self._eb900b_keepalive_task = None

    async def _async_eb900b_ensure_session(self) -> None:
        """Ensure the proprietary UART session is initialised and kept alive."""
        if getattr(self, "_eb900b_qz_initialized", False):
            task = getattr(self, "_eb900b_keepalive_task", None)
            if task is not None and not task.done():
                return

        session_lock = getattr(self, "_eb900b_session_lock", None)
        if session_lock is None:
            self._eb900b_session_lock = asyncio.Lock()
            session_lock = self._eb900b_session_lock

        async with session_lock:
            if getattr(self, "_eb900b_qz_initialized", False):
                task = getattr(self, "_eb900b_keepalive_task", None)
                if task is not None and not task.done():
                    return

            cli = self.ftms._cli
            write_char, notify_char = self._eb900b_find_uart_characteristics()

            if write_char is None:
                raise RuntimeError(
                    "EB900 B proprietary UART write characteristic not available"
                )

            if notify_char is None:
                raise RuntimeError(
                    "EB900 B proprietary UART notify characteristic not available"
                )

            # A previous BLE connection may have died while these flags stayed
            # on the entity. Start fresh whenever we need to build the session.
            self._eb900b_qz_initialized = False
            self._eb900b_uart_partial = b""

            if not getattr(self, "_eb900b_notify_started", False):
                await cli.start_notify(notify_char, self._eb900b_uart_notify)
                self._eb900b_notify_started = True
                await asyncio.sleep(0.20)

            # btinit_changyow(false) from QZ, expressed as logical packets.
            _LOGGER.debug("EB900 B QZ-style UART initialisation starting")

            for packet in EB900B_INIT_PACKETS:
                await self._async_eb900b_write_packet(write_char, packet)
                await asyncio.sleep(0.30)

            self._eb900b_qz_initialized = True

            _LOGGER.debug("EB900 B QZ-style UART initialisation complete")

            task = getattr(self, "_eb900b_keepalive_task", None)
            if task is None or task.done():
                self._eb900b_keepalive_task = self.hass.async_create_task(
                    self._async_eb900b_keepalive(cli, write_char)
                )

            # Let at least one keep-alive packet establish the normal running
            # state before a resistance command is sent immediately after init.
            await asyncio.sleep(0.35)

    async def _async_eb900b_prepare_session(self) -> None:
        """Prepare the UART session in the background after the bike connects."""
        try:
            await self._async_eb900b_ensure_session()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # A transient connection/setup failure should not break the FTMS
            # integration. The next resistance command will try again.
            _LOGGER.debug("EB900 B UART background preparation deferred: %s", err)

    @callback
    def _eb900b_schedule_prepare_session(self) -> None:
        """Schedule proprietary session setup without blocking coordinator updates."""
        if self.key != c.TARGET_RESISTANCE or self.hass is None:
            return

        if getattr(self, "_eb900b_qz_initialized", False):
            return

        task = getattr(self, "_eb900b_prepare_task", None)
        if task is not None and not task.done():
            return

        self._eb900b_prepare_task = self.hass.async_create_task(
            self._async_eb900b_prepare_session()
        )

    def _eb900b_build_resistance_packet(self, level: int) -> bytes:
        """Build the QZ/ChangYow F0 AD forceResistance packet."""
        packet = bytearray([
            0xF0, 0xAD,
            0xFF, 0xFF, 0xFF, 0xFF,
            0xFF, 0xFF, 0xFF, 0xFF,
            0xFF,  # resistance -> index 10
            0xFF, 0xFF, 0xFF, 0xFF,
            0xFF, 0xFF,
            0x00, 0x01,
            0xFF, 0xFF, 0xFF,
            0x00,  # checksum -> index 22
        ])

        packet[10] = level
        packet[22] = sum(packet[:22]) & 0xFF
        return bytes(packet)

    async def _async_eb900b_send_resistance_command(self, level: int) -> None:
        """Send one proprietary resistance command without verification."""
        await self._async_eb900b_ensure_session()

        write_char, _ = self._eb900b_find_uart_characteristics()
        if write_char is None:
            raise RuntimeError(
                "EB900 B proprietary UART write characteristic not available"
            )

        packet = self._eb900b_build_resistance_packet(level)

        await self._async_eb900b_write_packet(write_char, packet)

    def _eb900b_current_actual_resistance(self):
        """Return the freshest known physical resistance."""
        now = time.monotonic()
        uart_time = getattr(self, "_eb900b_uart_actual_time", None)
        uart_value = getattr(self, "_eb900b_uart_actual_resistance", None)

        if (
            uart_time is not None
            and uart_value is not None
            and now - uart_time <= EB900B_UART_FRESH_SECONDS
        ):
            return int(uart_value)

        ftms_value = getattr(self, "_eb900b_ftms_actual_resistance", None)
        if ftms_value is not None:
            return int(ftms_value)

        return None

    @callback
    def _eb900b_finish_target(self, level: int) -> None:
        """Mark a pending resistance target as reached."""
        if getattr(self, "_eb900b_target_resistance", None) != level:
            return

        self._eb900b_target_resistance = None
        self._eb900b_target_deadline = 0.0
        self._attr_native_value = float(level)
        self.async_write_ha_state()

    async def _async_eb900b_verify_target(self, level: int) -> None:
        """Verify the resistance target and perform at most one retry."""
        try:
            await asyncio.sleep(EB900B_VERIFY_SECONDS)

            if getattr(self, "_eb900b_target_resistance", None) != level:
                return

            actual = self._eb900b_current_actual_resistance()
            if actual == level:
                self._eb900b_finish_target(level)
                return

            _LOGGER.debug(
                "EB900 B resistance R%s not reached after %.1fs (actual=%s); retrying once",
                level,
                EB900B_VERIFY_SECONDS,
                actual,
            )

            await self._async_eb900b_send_resistance_command(level)
            await asyncio.sleep(EB900B_VERIFY_SECONDS)

            if getattr(self, "_eb900b_target_resistance", None) != level:
                return

            actual = self._eb900b_current_actual_resistance()
            if actual == level:
                self._eb900b_finish_target(level)
                return

            # One retry was already attempted. Give control of the slider back
            # to the real resistance instead of leaving a stale requested value.
            _LOGGER.warning(
                "EB900 B failed to reach resistance R%s after one retry; actual=%s",
                level,
                actual,
            )

            self._eb900b_target_resistance = None
            self._eb900b_target_deadline = 0.0

            if actual is not None:
                self._attr_native_value = float(actual)
                self.async_write_ha_state()

        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "Error verifying EB900 B resistance target R%s",
                level,
            )

    async def _async_eb900b_set_resistance(self, level: int) -> None:
        """Set EB900 B resistance with QZ-style UART control and one retry."""
        level = max(1, min(15, int(level)))

        await self._async_eb900b_ensure_session()

        # From this point the number entity represents the requested target.
        # The real resistance remains available separately through
        # sensor.indoor_bike_resistance_level.
        self._eb900b_target_resistance = level
        self._eb900b_target_deadline = (
            time.monotonic() + (EB900B_VERIFY_SECONDS * 2) + 1.0
        )

        await self._async_eb900b_send_resistance_command(level)

        old_task = getattr(self, "_eb900b_verify_task", None)
        if old_task is not None and not old_task.done():
            old_task.cancel()

        self._eb900b_verify_task = self.hass.async_create_task(
            self._async_eb900b_verify_target(level)
        )

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value from HA."""
        if self.key == c.TARGET_RESISTANCE:
            level = int(round(value))

            try:
                await self._async_eb900b_set_resistance(level)

            except Exception:
                self._eb900b_target_resistance = None
                self._eb900b_target_deadline = 0.0
                _LOGGER.exception(
                    "Failed to set EB900 B resistance using proprietary command: level=%s",
                    level,
                )
                raise

            # Keep the slider at the requested target while the physical
            # resistance moves towards it.
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

        if (value := e.event_data.get(key)) is None:
            return

        if self.key == c.TARGET_RESISTANCE:
            actual = int(round(value))
            self._eb900b_ftms_actual_resistance = actual

            # Every fresh bike/setup update is also an opportunity to prepare
            # the proprietary session before the first automated resistance
            # change. This avoids doing the full init in the middle of a sprint.
            self._eb900b_schedule_prepare_session()

            target = getattr(self, "_eb900b_target_resistance", None)

            if target is not None:
                if actual == target:
                    self._eb900b_finish_target(target)
                    return

                # Ignore intermediate physical values while a target is active;
                # otherwise the HA number slider visibly jumps during the
                # 1-2 second transition. The real value is still exposed by
                # sensor.indoor_bike_resistance_level.
                if time.monotonic() < getattr(
                    self,
                    "_eb900b_target_deadline",
                    0.0,
                ):
                    return

                # Safety fallback: target state should not remain pending
                # forever even if the verification task was interrupted.
                self._eb900b_target_resistance = None
                self._eb900b_target_deadline = 0.0

            self._attr_native_value = float(actual)
            self.async_write_ha_state()
            return

        self._attr_native_value = value
        self.async_write_ha_state()
