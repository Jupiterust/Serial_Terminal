from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from collections.abc import Callable
from typing import Protocol


SOH = 0x01
STX = 0x02
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRCCHR = ord("C")
CPMEOF = 0x1A

PACKET_128 = 128
PACKET_1K = 1024


class TransferError(RuntimeError):
    pass


class TransferCancelled(TransferError):
    pass


class TransferState(str, Enum):
    WAIT_RECEIVER = "wait_receiver"
    METADATA = "metadata"
    DATA = "data"
    EOT = "eot"
    DONE = "done"


class SerialLike(Protocol):
    timeout: float | None

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def flush(self) -> None: ...
    def reset_input_buffer(self) -> None: ...


@dataclass
class TransferOptions:
    timeout: float = 3.0
    retries: int = 5
    block_size: int = PACKET_1K
    packet_delay: float = 0.0


@dataclass
class TransferProgress:
    state: TransferState
    filename: str
    packet_no: int
    bytes_sent: int
    total_bytes: int
    retries: int
    elapsed: float

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 100.0
        return min(100.0, self.bytes_sent * 100.0 / self.total_bytes)

    @property
    def rate_kib(self) -> float:
        if self.elapsed <= 0:
            return 0.0
        return self.bytes_sent / 1024.0 / self.elapsed

    @property
    def eta_seconds(self) -> float | None:
        if self.bytes_sent <= 0 or self.elapsed <= 0:
            return None
        remaining = self.total_bytes - self.bytes_sent
        return max(0.0, remaining / (self.bytes_sent / self.elapsed))


ProgressCallback = Callable[[TransferProgress], None]


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT used by X/Ymodem CRC mode: poly 0x1021, init 0x0000."""

    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


class ModemSender:
    def __init__(
        self,
        serial_port: SerialLike,
        options: TransferOptions | None = None,
        progress: ProgressCallback | None = None,
        protocol_log: Callable[[str, bytes, str], None] | None = None,
    ) -> None:
        self.serial = serial_port
        self.options = options or TransferOptions()
        self.progress = progress
        self.protocol_log = protocol_log
        self._start = 0.0

    def send_ymodem(self, path: str | Path) -> None:
        """Send a single file using standard Ymodem-1K with CRC16."""

        file_path = Path(path).expanduser()
        if not file_path.is_file():
            raise TransferError(f"file not found: {file_path}")
        total = file_path.stat().st_size
        self._start = time.monotonic()
        self._wait_receiver_ready()
        self._emit(TransferState.METADATA, file_path.name, 0, 0, total, 0)
        metadata = self._metadata_payload(file_path)
        self._send_packet(0, metadata, PACKET_128, file_path.name, 0, total, TransferState.METADATA, pad_byte=0)
        self._expect_byte(CRCCHR, "receiver did not request first data packet")

        sent = 0
        packet_no = 1
        with file_path.open("rb") as file_obj:
            while True:
                chunk = file_obj.read(self.options.block_size)
                if not chunk:
                    break
                self._send_packet(
                    packet_no,
                    chunk,
                    self.options.block_size,
                    file_path.name,
                    sent,
                    total,
                    TransferState.DATA,
                )
                sent += len(chunk)
                self._emit(TransferState.DATA, file_path.name, packet_no, sent, total, 0)
                packet_no = (packet_no + 1) & 0xFF

        self._finish_eot(file_path.name, total)
        self._expect_byte(CRCCHR, "receiver did not request final Ymodem block")
        self._send_packet(0, b"", PACKET_128, file_path.name, total, total, TransferState.DONE, pad_byte=0)
        self._emit(TransferState.DONE, file_path.name, packet_no, total, total, 0)

    def send_xmodem(self, path: str | Path) -> None:
        """Send a file with Xmodem-1K CRC mode.

        Xmodem has no filename metadata block, so the receiver must already know
        what to do with the incoming byte stream.
        """

        file_path = Path(path).expanduser()
        if not file_path.is_file():
            raise TransferError(f"file not found: {file_path}")
        total = file_path.stat().st_size
        self._start = time.monotonic()
        self._wait_receiver_ready()
        sent = 0
        packet_no = 1
        with file_path.open("rb") as file_obj:
            while True:
                chunk = file_obj.read(self.options.block_size)
                if not chunk:
                    break
                self._send_packet(
                    packet_no,
                    chunk,
                    self.options.block_size,
                    file_path.name,
                    sent,
                    total,
                    TransferState.DATA,
                )
                sent += len(chunk)
                self._emit(TransferState.DATA, file_path.name, packet_no, sent, total, 0)
                packet_no = (packet_no + 1) & 0xFF
        self._finish_eot(file_path.name, total)
        self._emit(TransferState.DONE, file_path.name, packet_no, total, total, 0)

    def _wait_receiver_ready(self) -> None:
        self._flush_input()
        deadline = time.monotonic() + self.options.timeout * max(1, self.options.retries)
        self.serial.timeout = min(self.options.timeout, 1.0)
        while time.monotonic() < deadline:
            byte = self.serial.read(1)
            if not byte:
                continue
            self._log("Rx", byte, "receiver-ready")
            value = byte[0]
            if value == CRCCHR:
                return
            if value == CAN:
                self._drain_cancel()
                raise TransferCancelled("receiver cancelled transfer")
            if value == NAK:
                raise TransferError("receiver requested checksum mode; CRC16 mode requires 'C'")
        raise TransferError("timed out waiting for receiver 'C'")

    def _send_packet(
        self,
        packet_no: int,
        data: bytes,
        packet_size: int,
        filename: str,
        bytes_sent: int,
        total: int,
        state: TransferState,
        pad_byte: int = CPMEOF,
    ) -> None:
        payload = data[:packet_size].ljust(packet_size, bytes([pad_byte]))
        header = bytes([STX if packet_size == PACKET_1K else SOH, packet_no & 0xFF, 0xFF - (packet_no & 0xFF)])
        crc = crc16_ccitt(payload).to_bytes(2, "big")
        frame = header + payload + crc

        for retry in range(self.options.retries + 1):
            self._emit(state, filename, packet_no, bytes_sent, total, retry)
            self._write(frame, f"packet={packet_no} retry={retry}")
            response = self._read_control()
            if response == ACK:
                return
            if response == NAK:
                continue
            if response == CAN:
                self._drain_cancel()
                raise TransferCancelled("receiver cancelled transfer")
            if response is None:
                continue
            raise TransferError(f"unexpected response 0x{response:02X} for packet {packet_no}")
        raise TransferError(f"packet {packet_no} failed after {self.options.retries} retries")

    def _finish_eot(self, filename: str, total: int) -> None:
        self._emit(TransferState.EOT, filename, 0, total, total, 0)
        for retry in range(self.options.retries + 1):
            self._write(bytes([EOT]), f"eot retry={retry}")
            response = self._read_control()
            if response == ACK:
                return
            if response == NAK:
                self._write(bytes([EOT]), f"eot retry={retry} second")
                second = self._read_control()
                if second == ACK:
                    return
            if response == CAN:
                self._drain_cancel()
                raise TransferCancelled("receiver cancelled transfer")
            self._emit(TransferState.EOT, filename, 0, total, total, retry)
        raise TransferError("EOT was not acknowledged")

    def _expect_byte(self, expected: int, message: str) -> None:
        actual = self._read_control()
        if actual == CAN:
            self._drain_cancel()
            raise TransferCancelled("receiver cancelled transfer")
        if actual != expected:
            got = "timeout" if actual is None else f"0x{actual:02X}"
            raise TransferError(f"{message}; got {got}")

    def _read_control(self) -> int | None:
        self.serial.timeout = self.options.timeout
        data = self.serial.read(1)
        if data:
            self._log("Rx", data, "control")
        return data[0] if data else None

    def _drain_cancel(self) -> None:
        self.serial.timeout = 0.05
        while self.serial.read(1):
            pass

    def _write(self, data: bytes, note: str) -> None:
        self.serial.write(data)
        self.serial.flush()
        self._log("Tx", data, note)
        if self.options.packet_delay > 0:
            time.sleep(self.options.packet_delay)

    def _flush_input(self) -> None:
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

    def _log(self, direction: str, data: bytes, note: str) -> None:
        if self.protocol_log:
            self.protocol_log(direction, data, note)

    def _metadata_payload(self, path: Path) -> bytes:
        size = path.stat().st_size
        metadata = f"{path.name}\0{size}".encode("ascii", errors="replace")
        if len(metadata) > PACKET_128:
            raise TransferError("Ymodem metadata is longer than 128 bytes; shorten the filename")
        return metadata

    def _emit(
        self,
        state: TransferState,
        filename: str,
        packet_no: int,
        bytes_sent: int,
        total: int,
        retries: int,
    ) -> None:
        if not self.progress:
            return
        self.progress(
            TransferProgress(
                state=state,
                filename=filename,
                packet_no=packet_no,
                bytes_sent=bytes_sent,
                total_bytes=total,
                retries=retries,
                elapsed=max(0.001, time.monotonic() - self._start),
            )
        )
