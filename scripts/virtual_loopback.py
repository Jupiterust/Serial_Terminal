#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import select
import sys
import threading
import time


def run_posix(interval: float) -> None:
    import pty
    import tty

    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    tty.setraw(master_fd)
    tty.setraw(slave_fd)

    stop = threading.Event()

    def periodic_mcu_tx() -> None:
        messages = [
            "虚拟 MCU: 中文 UTF-8 测试，输入会被回显。\r\n".encode("utf-8"),
            b"\xAA\x55\x00\xFF HEX-BYTES\r\n",
            ("LONG-" + "0123456789ABCDEF" * 64 + "\r\n").encode("ascii"),
        ]
        index = 0
        while not stop.is_set():
            os.write(master_fd, messages[index % len(messages)])
            index += 1
            time.sleep(interval)

    thread = threading.Thread(target=periodic_mcu_tx, daemon=True)
    thread.start()

    print("Virtual serial loopback is running.")
    print(f"Connect the CLI with: serial-debug --port {slave_name} --baudrate 115200")
    print("Press Ctrl+C here to stop the simulator.")

    try:
        while True:
            readable, _, _ = select.select([master_fd], [], [], 0.2)
            if master_fd not in readable:
                continue
            data = os.read(master_fd, 4096)
            if not data:
                break
            os.write(master_fd, b"loopback> " + data)
            sys.stdout.buffer.write(b"CLI -> MCU: " + data)
            sys.stdout.buffer.flush()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        os.close(master_fd)
        os.close(slave_fd)


def run_windows() -> None:
    print("Windows does not provide POSIX pty pairs.")
    print("Use one of these options:")
    print("  1. Run: serial-debug --demo")
    print("  2. Install com0com, create a pair such as COM10 <-> COM11,")
    print("     then run this simulator on one side with a small pyserial bridge.")
    print("The built-in --demo mode is fully in-process and requires no driver.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Virtual MCU loopback simulator for Serial Debug CLI")
    parser.add_argument("--interval", type=float, default=1.5, help="Periodic MCU message interval in seconds")
    args = parser.parse_args()

    if os.name == "nt":
        run_windows()
    else:
        run_posix(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
