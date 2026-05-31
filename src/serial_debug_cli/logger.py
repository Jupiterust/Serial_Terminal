from __future__ import annotations

from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from threading import RLock, Thread

from .codec import bytes_to_hex


class TimestampLogger:
    def __init__(self) -> None:
        self._path: Path | None = None
        self._file = None
        self._lock = RLock()
        self._queue: Queue[tuple[str, str] | None] = Queue(maxsize=20000)
        self._thread: Thread | None = None
        self._dropped = 0

    @property
    def active(self) -> bool:
        return self._file is not None

    @property
    def path(self) -> Path | None:
        return self._path

    def start(self, path: str) -> None:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.stop()
            self._path = target
            self._file = target.open("a", encoding="utf-8", newline="")
            self._thread = Thread(target=self._write_loop, name="serial-log-writer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
        if thread:
            self._queue.put(None)
        if thread:
            thread.join(timeout=2.0)
        with self._lock:
            if self._file:
                if self._dropped:
                    timestamp = datetime.now().isoformat(timespec="milliseconds")
                    self._file.write(f"[{timestamp}] WARN dropped {self._dropped} log line(s)\n")
                    self._dropped = 0
                self._file.flush()
                self._file.close()
            self._file = None
            self._path = None
            self._thread = None

    def write_rx(self, payload: str) -> None:
        self._write("RX", payload)

    def write_tx(self, payload: str) -> None:
        self._write("TX", payload)

    def write_protocol(self, direction: str, payload: bytes, note: str = "") -> None:
        suffix = f" {note}" if note else ""
        self._write(direction, f"{bytes_to_hex(payload)}{suffix}")

    def _write(self, direction: str, payload: str) -> None:
        with self._lock:
            if not self._file:
                return
        timestamp = datetime.now().isoformat(timespec="milliseconds")
        for line in payload.splitlines() or [""]:
            try:
                self._queue.put_nowait((direction, f"[{timestamp}] {direction} {line}\n"))
            except Exception:
                with self._lock:
                    self._dropped += 1

    def _write_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            _, line = item
            with self._lock:
                if self._file:
                    self._file.write(line)
            self._queue.task_done()

        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            if item is not None:
                _, line = item
                with self._lock:
                    if self._file:
                        self._file.write(line)
            self._queue.task_done()
        with self._lock:
            if self._file:
                self._file.flush()
