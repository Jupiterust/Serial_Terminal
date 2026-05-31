from __future__ import annotations

from dataclasses import dataclass

import serial


@dataclass
class SerialConfig:
    port: str | None = None
    baudrate: int = 115200
    bytesize: int = serial.EIGHTBITS
    parity: str = serial.PARITY_NONE
    stopbits: float = serial.STOPBITS_ONE
    timeout: float = 0.05

    def as_serial_kwargs(self) -> dict:
        if not self.port:
            raise ValueError("serial port is not configured")
        return {
            "port": self.port,
            "baudrate": self.baudrate,
            "bytesize": self.bytesize,
            "parity": self.parity,
            "stopbits": self.stopbits,
            "timeout": self.timeout,
        }

    def summary(self) -> str:
        return (
            f"port={self.port or '-'} baud={self.baudrate} data={self.bytesize} "
            f"parity={self.parity} stop={self.stopbits}"
        )
