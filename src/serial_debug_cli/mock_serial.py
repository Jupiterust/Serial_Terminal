from __future__ import annotations

import queue
import threading
import time


class MockSerial:
    """In-process serial-like device for --demo mode.

    The CLI talks to this object through the same read/write API it uses for
    PySerial. A small MCU thread periodically injects text/HEX/long payloads and
    echoes everything the terminal sends.
    """

    port = "mock://demo"

    def __init__(self, timeout: float | None = 0.05) -> None:
        self.timeout = timeout
        self.is_open = True
        self.dtr = False
        self.rts = False
        self._rx = queue.Queue()
        self._tx = queue.Queue()
        self._closed = threading.Event()
        self._mcu = threading.Thread(target=self._mcu_loop, name="mock-mcu", daemon=True)
        self._mcu.start()

    @property
    def in_waiting(self) -> int:
        return self._rx.qsize()

    def read(self, size: int = 1) -> bytes:
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        out = bytearray()
        while len(out) < size and not self._closed.is_set():
            timeout = None
            if deadline is not None:
                timeout = max(0.0, deadline - time.monotonic())
                if timeout == 0.0 and out:
                    break
            try:
                out.append(self._rx.get(timeout=timeout))
            except queue.Empty:
                break
        return bytes(out)

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise OSError("mock serial port is closed")
        for byte in data:
            self._tx.put(byte)
        return len(data)

    def flush(self) -> None:
        return

    def reset_input_buffer(self) -> None:
        while True:
            try:
                self._rx.get_nowait()
            except queue.Empty:
                return

    def reset_output_buffer(self) -> None:
        while True:
            try:
                self._tx.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self.is_open = False
        self._closed.set()

    def inject_rx(self, data: bytes) -> None:
        for byte in data:
            self._rx.put(byte)

    def _mcu_loop(self) -> None:
        messages = [
            "Mock MCU: 你好，串口终端。\r\n".encode("utf-8"),
            b"Mock MCU HEX bytes: \xAA\x55\x00\xFF\r\n",
            ("Mock MCU long line: " + "0123456789ABCDEF" * 20 + "\r\n").encode("ascii"),
        ]
        index = 0
        next_tick = time.monotonic() + 0.5
        pending = bytearray()
        while not self._closed.is_set():
            try:
                while True:
                    pending.append(self._tx.get_nowait())
            except queue.Empty:
                pass
            if pending:
                self.inject_rx(b"echo> " + bytes(pending))
                pending.clear()
            if time.monotonic() >= next_tick:
                self.inject_rx(messages[index % len(messages)])
                index += 1
                next_tick = time.monotonic() + 1.5
            time.sleep(0.01)
