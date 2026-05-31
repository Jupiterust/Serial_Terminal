from __future__ import annotations

import threading
from contextlib import contextmanager
from collections.abc import Callable
from typing import Any, Iterator

import serial
from serial.tools import list_ports

from .config import SerialConfig
from .mock_serial import MockSerial


RxCallback = Callable[[bytes], None]
ErrorCallback = Callable[[Exception], None]


def list_serial_ports() -> list[tuple[str, str]]:
    return [(port.device, port.description) for port in list_ports.comports()]


class SerialWorker:
    """Owns the serial object and a blocking read thread.

    PySerial's blocking API is stable on Windows, macOS and Linux. Keeping that
    blocking read in a daemon thread avoids tying terminal rendering to device
    latency while still leaving writes simple and deterministic.
    """

    def __init__(self, on_rx: RxCallback, on_error: ErrorCallback) -> None:
        self._on_rx = on_rx
        self._on_error = on_error
        self._serial: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    @property
    def port(self) -> str | None:
        return self._serial.port if self._serial else None

    @property
    def serial(self) -> Any:
        if not self._serial or not self._serial.is_open:
            raise RuntimeError("serial port is not open")
        return self._serial

    def open(self, config: SerialConfig) -> None:
        self.close()
        try:
            ser = serial.Serial(**config.as_serial_kwargs())
        except serial.SerialException as exc:
            raise RuntimeError(f"failed to open {config.port}: {exc}") from exc

        self._serial = ser
        self._start_reader()

    def open_mock(self) -> None:
        self.close()
        self._serial = MockSerial()
        self._start_reader()

    def close(self) -> None:
        self._stop_reader()
        with self._lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception:
                    pass
        self._serial = None

    def write(self, payload: bytes) -> None:
        with self._lock:
            if not self._serial or not self._serial.is_open:
                raise RuntimeError("serial port is not open")
            try:
                self._serial.write(payload)
                self._serial.flush()
            except serial.SerialException as exc:
                raise RuntimeError(f"serial write failed: {exc}") from exc

    @contextmanager
    def exclusive_session(self, release_lines: bool = False, restore_lines: bool = True) -> Iterator[Any]:
        """Temporarily stop the Rx thread and hand the raw serial port to a protocol.

        Binary transfer protocols need to consume ACK/NAK/CAN bytes directly.
        If the normal terminal reader is still active, it can steal those bytes
        and corrupt the protocol state machine.
        """

        self._stop_reader()
        ser = self.serial
        old_timeout = ser.timeout
        old_dtr = getattr(ser, "dtr", False)
        old_rts = getattr(ser, "rts", False)
        try:
            self.flush_input(ser)
            yield ser
        finally:
            try:
                ser.timeout = old_timeout
                if release_lines:
                    # Put modem-control pins into inactive levels on every exit
                    # path so a failed flash attempt cannot hold RESET/BOOT0.
                    ser.dtr = False
                    ser.rts = False
                elif restore_lines:
                    ser.dtr = old_dtr
                    ser.rts = old_rts
            except Exception:
                pass
            if getattr(ser, "is_open", False):
                self._start_reader()

    @staticmethod
    def flush_input(ser: Any) -> None:
        try:
            ser.reset_input_buffer()
        except Exception:
            try:
                ser.timeout = 0
                while ser.read(4096):
                    pass
            finally:
                pass

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    ser = self._serial
                if not ser or not ser.is_open:
                    return
                waiting = getattr(ser, "in_waiting", 0)
                read_size = min(max(waiting, 1), 65536)
                data = ser.read(read_size)
                if data:
                    self._on_rx(data)
            except serial.SerialException as exc:
                if not self._stop.is_set():
                    self._mark_disconnected()
                    self._on_error(exc)
                return
            except OSError as exc:
                if not self._stop.is_set():
                    self._mark_disconnected()
                    self._on_error(exc)
                return
            except Exception as exc:
                if not self._stop.is_set():
                    self._mark_disconnected()
                    self._on_error(exc)
                return

    def _start_reader(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, name="serial-rx", daemon=True)
        self._thread.start()

    def _stop_reader(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _mark_disconnected(self) -> None:
        with self._lock:
            ser = self._serial
            self._serial = None
        if ser:
            try:
                ser.close()
            except Exception:
                pass
