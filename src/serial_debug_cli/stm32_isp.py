from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


STM32_ACK = 0x79
STM32_NACK = 0x1F


class Stm32IspError(RuntimeError):
    pass


class LinePolarity(str, Enum):
    NORMAL = "normal"
    INVERTED = "inverted"


class SerialIspLike(Protocol):
    timeout: float | None
    dtr: bool
    rts: bool

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def flush(self) -> None: ...
    def reset_input_buffer(self) -> None: ...


@dataclass(frozen=True)
class Stm32ResetConfig:
    """Logical STM32 boot pins mapped to physical USB-UART modem lines."""

    dtr_polarity: LinePolarity = LinePolarity.NORMAL
    rts_polarity: LinePolarity = LinePolarity.NORMAL
    boot0_asserted: bool = True
    reset_asserted: bool = True
    boot0_release: bool = False
    reset_release: bool = False
    pulse_seconds: float = 0.1
    settle_seconds: float = 0.2

    def dtr_level(self, logical_asserted: bool) -> bool:
        return _apply_polarity(logical_asserted, self.dtr_polarity)

    def rts_level(self, logical_asserted: bool) -> bool:
        return _apply_polarity(logical_asserted, self.rts_polarity)


BOARD_PRESETS: dict[str, Stm32ResetConfig] = {
    "generic": Stm32ResetConfig(),
    # Common one-key-download circuits often invert one or both modem signals.
    # These presets are intentionally configurable from CLI because USB-UART
    # modules and transistor networks vary between board revisions.
    "atomic": Stm32ResetConfig(dtr_polarity=LinePolarity.INVERTED, rts_polarity=LinePolarity.INVERTED),
    "wildfire": Stm32ResetConfig(dtr_polarity=LinePolarity.NORMAL, rts_polarity=LinePolarity.INVERTED),
    "inverted": Stm32ResetConfig(dtr_polarity=LinePolarity.INVERTED, rts_polarity=LinePolarity.INVERTED),
}


@dataclass
class Stm32Progress:
    address: int
    bytes_written: int
    total_bytes: int

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 100.0
        return min(100.0, self.bytes_written * 100.0 / self.total_bytes)


def _apply_polarity(logical_asserted: bool, polarity: LinePolarity) -> bool:
    return not logical_asserted if polarity == LinePolarity.INVERTED else logical_asserted


class Stm32Isp:
    """Small STM32 system bootloader client for AN3155 UART mode."""

    def __init__(
        self,
        serial_port: SerialIspLike,
        timeout: float = 1.0,
        reset_config: Stm32ResetConfig | None = None,
        packet_delay: float = 0.0,
        protocol_log=None,
    ) -> None:
        self.serial = serial_port
        self.timeout = timeout
        self.reset_config = reset_config or Stm32ResetConfig()
        self.packet_delay = packet_delay
        self.protocol_log = protocol_log

    def enter_bootloader(self, config: Stm32ResetConfig | None = None) -> None:
        """Drive DTR as BOOT0 and RTS as RESET with configurable polarity."""

        cfg = config or self.reset_config
        self.serial.dtr = cfg.dtr_level(cfg.boot0_asserted)
        self.serial.rts = cfg.rts_level(cfg.reset_asserted)
        self.serial.flush()
        time.sleep(cfg.pulse_seconds)
        self.serial.rts = cfg.rts_level(cfg.reset_release)
        self.serial.flush()
        time.sleep(cfg.settle_seconds)
        self.flush_input()

    def release_lines(self, config: Stm32ResetConfig | None = None) -> None:
        cfg = config or self.reset_config
        try:
            self.serial.dtr = cfg.dtr_level(cfg.boot0_release)
            self.serial.rts = cfg.rts_level(cfg.reset_release)
            self.serial.flush()
        except Exception:
            pass

    def sync(self) -> None:
        self.flush_input()
        self.serial.timeout = self.timeout
        self._write(b"\x7F", "sync")
        self._expect_ack("STM32 bootloader sync failed")

    def get_commands(self) -> bytes:
        self._command(0x00)
        count = self._read_exact(1)[0] + 1
        version_and_commands = self._read_exact(count + 1)
        self._expect_ack("GET command was not acknowledged")
        return version_and_commands

    def get_id(self) -> bytes:
        self._command(0x02)
        count = self._read_exact(1)[0] + 1
        chip_id = self._read_exact(count)
        self._expect_ack("GET ID was not acknowledged")
        return chip_id

    def mass_erase(self) -> None:
        self._command(0x44)
        self._write(b"\xFF\xFF\x00", "mass-erase")
        self._expect_ack("mass erase failed")

    def write_memory_file(self, address: int, path: str | Path, progress=None) -> None:
        firmware = Path(path).expanduser().read_bytes()
        offset = 0
        while offset < len(firmware):
            chunk = firmware[offset : offset + 256]
            self.write_memory(address + offset, chunk)
            offset += len(chunk)
            if progress:
                progress(Stm32Progress(address + offset, offset, len(firmware)))
            if self.packet_delay > 0:
                time.sleep(self.packet_delay)

    def write_memory(self, address: int, data: bytes) -> None:
        if not 1 <= len(data) <= 256:
            raise Stm32IspError("write chunk must be 1..256 bytes")
        self._command(0x31)
        self._send_address(address)
        length = len(data) - 1
        checksum = length
        for byte in data:
            checksum ^= byte
        self._write(bytes([length]) + data + bytes([checksum & 0xFF]), f"write-memory 0x{address:08X}")
        self._expect_ack(f"write memory failed at 0x{address:08X}")

    def flush_input(self) -> None:
        try:
            self.serial.reset_input_buffer()
        except Exception:
            old_timeout = self.serial.timeout
            self.serial.timeout = 0
            try:
                while self.serial.read(4096):
                    pass
            finally:
                self.serial.timeout = old_timeout

    def _command(self, command: int) -> None:
        self._write(bytes([command, command ^ 0xFF]), f"command 0x{command:02X}")
        self._expect_ack(f"command 0x{command:02X} was rejected")

    def _send_address(self, address: int) -> None:
        raw = address.to_bytes(4, "big")
        checksum = raw[0] ^ raw[1] ^ raw[2] ^ raw[3]
        self._write(raw + bytes([checksum]), f"address 0x{address:08X}")
        self._expect_ack(f"address 0x{address:08X} was rejected")

    def _expect_ack(self, message: str) -> None:
        data = self._read_exact(1)
        if data[0] == STM32_ACK:
            return
        if data[0] == STM32_NACK:
            raise Stm32IspError(f"{message}: NACK")
        raise Stm32IspError(f"{message}: got 0x{data[0]:02X}")

    def _read_exact(self, size: int) -> bytes:
        self.serial.timeout = self.timeout
        data = self.serial.read(size)
        if data:
            self._log("Rx", data, "stm32")
        if len(data) != size:
            raise Stm32IspError(f"timeout reading {size} byte(s)")
        return data

    def _write(self, data: bytes, note: str) -> None:
        self.serial.write(data)
        self.serial.flush()
        self._log("Tx", data, note)
        if self.packet_delay > 0:
            time.sleep(self.packet_delay)

    def _log(self, direction: str, data: bytes, note: str) -> None:
        if self.protocol_log:
            self.protocol_log(direction, data, note)
