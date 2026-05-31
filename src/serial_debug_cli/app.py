from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.shortcuts import clear

from .codec import CodecError, StreamDecoder, bytes_to_hex, encode_text, parse_hex
from .config import SerialConfig
from .logger import TimestampLogger
from .serial_worker import SerialWorker, list_serial_ports
from .stm32_isp import BOARD_PRESETS, LinePolarity, Stm32Isp, Stm32IspError, Stm32Progress, Stm32ResetConfig
from .terminal import Terminal
from .transfer import ModemSender, TransferError, TransferOptions, TransferProgress


TERMINATORS = {
    "crlf": b"\r\n",
    "lf": b"\n",
    "none": b"",
}


@dataclass
class RuntimeState:
    encoding: str = "utf-8"
    hex_mode: bool = False
    terminator_name: str = "crlf"
    running: bool = True

    @property
    def terminator(self) -> bytes:
        return TERMINATORS[self.terminator_name]


class SerialDebugApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.config = SerialConfig(
            port=args.port,
            baudrate=args.baudrate,
            bytesize=args.bytesize,
            parity=args.parity,
            stopbits=args.stopbits,
        )
        self.state = RuntimeState(encoding=args.encoding, hex_mode=args.hex)
        self.rx_decoder = StreamDecoder(args.encoding)
        self.transfer_options = TransferOptions(
            timeout=args.transfer_timeout,
            retries=args.transfer_retries,
            packet_delay=args.packet_delay / 1000.0,
        )
        self.verbose_protocol = args.verbose_protocol
        self.demo = args.demo
        self.raw_stream = args.raw_stream
        self.reset_config = self._build_reset_config(args)
        self.logger = TimestampLogger()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.rx_line_buffer = ""
        self.rx_line_timestamp = ""
        self.rx_flush_handle: asyncio.TimerHandle | None = None
        self.worker = SerialWorker(self._rx_from_thread, self._serial_error_from_thread)

        history_dir = Path.home() / ".serial_debug_cli"
        history_dir.mkdir(parents=True, exist_ok=True)
        self.terminal = Terminal(
            history_file=str(history_dir / "history"),
            on_toggle_hex=self.toggle_hex,
            on_toggle_terminator=self.toggle_terminator,
            on_exit=self.request_exit,
        )

    async def run(self) -> int:
        self.loop = asyncio.get_running_loop()
        self.print_banner()
        if self.demo:
            self.worker.open_mock()
            self.ok("Demo mode connected to internal mock MCU state machine")
        else:
            self.print_ports()

        if self.config.port and not self.demo:
            self.try_open()

        while self.state.running:
            try:
                line = await self.terminal.prompt(self.prompt_message())
            except (EOFError, KeyboardInterrupt):
                break

            line = line.strip()
            if not line:
                continue
            if line.startswith(":"):
                await self.handle_command(line)
            else:
                self.send_line(line)

        self.shutdown()
        return 0

    def print_banner(self) -> None:
        self.info("Serial Debug CLI Tool")
        self.info("Type :help for commands. Ctrl+C or :quit exits cleanly.")
        if self.verbose_protocol:
            self.info("Protocol verbose logging is enabled; use :log start PATH to persist low-level frames.")

    def print_ports(self) -> None:
        ports = list_serial_ports()
        if not ports:
            self.warn("No serial ports detected. Use :ports to rescan or :open PORT later.")
            return
        self.info("Available serial ports:")
        for index, (device, desc) in enumerate(ports, start=1):
            self.out(f"  {index}. {device}  {desc}")

    def try_open(self) -> None:
        try:
            self.worker.open(self.config)
            self.rx_decoder.reset()
            self.ok(f"Opened {self.config.summary()}")
        except Exception as exc:
            self.error(str(exc))

    def prompt_message(self) -> str:
        mode = "HEX" if self.state.hex_mode else self.state.encoding.upper()
        if self.demo:
            connection = "DEMO"
        else:
            connection = f"{self.worker.port or 'closed'} {self.config.baudrate}"
        term = self.state.terminator_name.upper()
        return (
            f"<ansigreen>[{connection}]</ansigreen> "
            f"<ansiyellow>{mode}</ansiyellow> "
            f"<ansiblue>{term}</ansiblue> &gt;&gt;&gt; "
        )

    def send_line(self, line: str) -> None:
        try:
            if self.state.hex_mode:
                payload = parse_hex(line)
                log_text = bytes_to_hex(payload)
            else:
                payload = encode_text(line, self.state.encoding) + self.state.terminator
                log_text = line
            if not payload:
                return
            self.worker.write(payload)
            self.logger.write_tx(log_text)
        except (CodecError, RuntimeError) as exc:
            self.error(str(exc))

    async def handle_command(self, line: str) -> None:
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            self.error(f"bad command: {exc}")
            return
        if not parts:
            return

        command = parts[0][1:].lower()
        args = parts[1:]

        handlers = {
            "help": self.cmd_help,
            "ports": self.cmd_ports,
            "open": self.cmd_open,
            "close": self.cmd_close,
            "config": self.cmd_config,
            "baud": self.cmd_baud,
            "encoding": self.cmd_encoding,
            "enc": self.cmd_encoding,
            "hex": self.cmd_hex,
            "ascii": self.cmd_ascii,
            "term": self.cmd_term,
            "log": self.cmd_log,
            "ymodem": self.cmd_ymodem,
            "xmodem": self.cmd_xmodem,
            "stm32-reset": self.cmd_stm32_reset,
            "stm32-sync": self.cmd_stm32_sync,
            "stm32-flash": self.cmd_stm32_flash,
            "clear": self.cmd_clear,
            "quit": self.cmd_quit,
            "q": self.cmd_quit,
        }

        handler = handlers.get(command)
        if not handler:
            self.error(f"unknown command: {command}")
            return
        result = handler(args)
        if asyncio.iscoroutine(result):
            await result

    def cmd_help(self, args: list[str]) -> None:
        self.out(
            """
Commands:
  :ports                      Rescan serial ports
  :open PORT|INDEX            Open port by name or list index
  :close                      Close current port
  :config                     Show current serial config
  :baud 115200                Set baudrate and reopen if needed
  :encoding utf-8|gbk|ascii   Switch text encoding
  :hex / :ascii               Switch send/display mode
  :term crlf|lf|none          Switch line ending for ASCII sends
  :log start PATH             Save TX/RX log with timestamps
  :log stop                   Stop logging
  :ymodem FILE                Send firmware with Ymodem-1K CRC
  :xmodem FILE                Send firmware with Xmodem-1K CRC
  :stm32-reset                DTR=BOOT0, RTS=RESET pulse with configured polarity
  :stm32-sync                 Send STM32 AN3155 0x7F sync
  :stm32-flash ADDR FILE      Mass erase and write .bin to address
  :clear                      Clear terminal
  :quit                       Exit cleanly
Shortcuts: Ctrl+H hex/ascii, Ctrl+L clear, Ctrl+E terminator, Ctrl+C exit.
""".strip()
        )

    def cmd_ports(self, args: list[str]) -> None:
        self.print_ports()

    def cmd_open(self, args: list[str]) -> None:
        if not args:
            self.error("usage: :open PORT|INDEX")
            return
        target = args[0]
        ports = list_serial_ports()
        if target.isdigit():
            index = int(target)
            if index < 1 or index > len(ports):
                self.error(f"port index out of range: {index}")
                return
            target = ports[index - 1][0]
        self.config.port = target
        self.try_open()

    def cmd_close(self, args: list[str]) -> None:
        self.worker.close()
        self.ok("Serial port closed")

    def cmd_config(self, args: list[str]) -> None:
        log = str(self.logger.path) if self.logger.active else "off"
        self.out(
            f"{self.config.summary()} encoding={self.state.encoding} "
            f"mode={'HEX' if self.state.hex_mode else 'ASCII'} "
            f"terminator={self.state.terminator_name} log={log}"
        )

    def cmd_baud(self, args: list[str]) -> None:
        if not args or not args[0].isdigit():
            self.error("usage: :baud 115200")
            return
        was_open = self.worker.is_open
        self.config.baudrate = int(args[0])
        self.ok(f"Baudrate set to {self.config.baudrate}")
        if was_open:
            self.try_open()

    def cmd_encoding(self, args: list[str]) -> None:
        if not args:
            self.error("usage: :encoding utf-8|gbk|gb2312|ascii")
            return
        try:
            encode_text("", args[0])
        except CodecError as exc:
            self.error(str(exc))
            return
        self.state.encoding = args[0]
        self.rx_decoder.set_encoding(args[0])
        self.ok(f"Encoding set to {self.state.encoding}")

    def cmd_hex(self, args: list[str]) -> None:
        self.state.hex_mode = True
        self.ok("HEX mode enabled")

    def cmd_ascii(self, args: list[str]) -> None:
        self.state.hex_mode = False
        self.ok("ASCII mode enabled")

    def cmd_term(self, args: list[str]) -> None:
        if not args or args[0].lower() not in TERMINATORS:
            self.error("usage: :term crlf|lf|none")
            return
        self.state.terminator_name = args[0].lower()
        self.ok(f"Terminator set to {self.state.terminator_name}")

    def cmd_log(self, args: list[str]) -> None:
        if not args:
            state = str(self.logger.path) if self.logger.active else "off"
            self.out(f"log: {state}")
            return
        action = args[0].lower()
        if action == "start":
            path = args[1] if len(args) > 1 else self.default_log_path()
            try:
                self.logger.start(path)
                self.ok(f"Logging to {self.logger.path}")
            except OSError as exc:
                self.error(f"cannot start log: {exc}")
        elif action == "stop":
            self.logger.stop()
            self.ok("Logging stopped")
        else:
            self.error("usage: :log start [PATH] | :log stop")

    async def cmd_ymodem(self, args: list[str]) -> None:
        if not args:
            self.error("usage: :ymodem FILE")
            return
        await self._run_modem_transfer("ymodem", args[0])

    async def cmd_xmodem(self, args: list[str]) -> None:
        if not args:
            self.error("usage: :xmodem FILE")
            return
        await self._run_modem_transfer("xmodem", args[0])

    def cmd_stm32_reset(self, args: list[str]) -> None:
        if not self.worker.is_open:
            self.error("serial port is not open")
            return
        try:
            with self.worker.exclusive_session(restore_lines=False) as ser:
                isp = self._make_stm32_isp(ser)
                try:
                    isp.enter_bootloader()
                finally:
                    isp.release_lines()
            self.ok(
                "STM32 reset pulse done "
                f"(DTR={self.reset_config.dtr_polarity.value}, RTS={self.reset_config.rts_polarity.value})"
            )
        except (RuntimeError, Stm32IspError) as exc:
            self.error(str(exc))

    def cmd_stm32_sync(self, args: list[str]) -> None:
        if not self.worker.is_open:
            self.error("serial port is not open")
            return
        try:
            with self.worker.exclusive_session(restore_lines=False) as ser:
                isp = self._make_stm32_isp(ser)
                try:
                    isp.sync()
                    chip_id = isp.get_id()
                finally:
                    isp.release_lines()
            self.ok(f"STM32 bootloader ACK, chip id: {chip_id.hex(' ').upper()}")
        except (RuntimeError, Stm32IspError) as exc:
            self.error(str(exc))

    async def cmd_stm32_flash(self, args: list[str]) -> None:
        if len(args) < 2:
            self.error("usage: :stm32-flash ADDRESS FILE")
            return
        if not self.worker.is_open:
            self.error("serial port is not open")
            return
        try:
            address = int(args[0], 0)
        except ValueError:
            self.error(f"bad address: {args[0]}")
            return
        path = args[1]
        try:
            await asyncio.to_thread(self._stm32_flash_blocking, address, path)
            self.ok("STM32 flash complete")
        except (RuntimeError, OSError, Stm32IspError) as exc:
            self.error(str(exc))

    async def _run_modem_transfer(self, protocol: str, path: str) -> None:
        if not self.worker.is_open:
            self.error("serial port is not open")
            return
        try:
            await asyncio.to_thread(self._modem_transfer_blocking, protocol, path)
            self.ok(f"{protocol.upper()} transfer complete")
        except (RuntimeError, OSError, TransferError) as exc:
            self.error(str(exc))

    def _modem_transfer_blocking(self, protocol: str, path: str) -> None:
        self.info(f"Starting {protocol.upper()} transfer. Waiting for receiver 'C'...")
        with self.worker.exclusive_session(release_lines=True) as ser:
            sender = ModemSender(
                ser,
                options=self.transfer_options,
                progress=self._render_transfer_progress,
                protocol_log=self._protocol_log if self.verbose_protocol else None,
            )
            if protocol == "ymodem":
                sender.send_ymodem(path)
            else:
                sender.send_xmodem(path)
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _stm32_flash_blocking(self, address: int, path: str) -> None:
        self.info("Starting STM32 ISP flash: sync, mass erase, write memory...")
        with self.worker.exclusive_session(restore_lines=False) as ser:
            isp = self._make_stm32_isp(ser)
            try:
                isp.sync()
                isp.mass_erase()
                isp.write_memory_file(address, path, progress=self._render_stm32_progress)
            finally:
                isp.release_lines()
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _render_transfer_progress(self, progress: TransferProgress) -> None:
        width = 28
        filled = int(width * progress.percent / 100)
        bar = "#" * filled + "-" * (width - filled)
        eta = "--" if progress.eta_seconds is None else self._format_seconds(progress.eta_seconds)
        sys.stdout.write(
            "\r"
            f"[{bar}] {progress.percent:6.2f}% "
            f"{progress.bytes_sent}/{progress.total_bytes} B "
            f"{progress.rate_kib:7.1f} KiB/s "
            f"eta {eta} pkt {progress.packet_no} retry {progress.retries} "
            f"{progress.state.value}   "
        )
        sys.stdout.flush()

    def _render_stm32_progress(self, progress: Stm32Progress) -> None:
        width = 28
        filled = int(width * progress.percent / 100)
        bar = "#" * filled + "-" * (width - filled)
        sys.stdout.write(
            "\r"
            f"[{bar}] {progress.percent:6.2f}% "
            f"{progress.bytes_written}/{progress.total_bytes} B "
            f"addr 0x{progress.address:08X}   "
        )
        sys.stdout.flush()

    def cmd_clear(self, args: list[str]) -> None:
        clear()

    def cmd_quit(self, args: list[str]) -> None:
        self.request_exit()

    def toggle_hex(self) -> None:
        self.state.hex_mode = not self.state.hex_mode
        self.ok(f"{'HEX' if self.state.hex_mode else 'ASCII'} mode")

    def toggle_terminator(self) -> None:
        names = list(TERMINATORS)
        current = names.index(self.state.terminator_name)
        self.state.terminator_name = names[(current + 1) % len(names)]
        self.ok(f"Terminator: {self.state.terminator_name}")

    def request_exit(self) -> None:
        self.state.running = False

    def shutdown(self) -> None:
        self._flush_rx_line_buffer()
        self.worker.close()
        self.logger.stop()
        self.ok("Bye")

    def _rx_from_thread(self, data: bytes) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._handle_rx, data)

    def _serial_error_from_thread(self, exc: Exception) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._handle_serial_error, exc)

    def _handle_serial_error(self, exc: Exception) -> None:
        self.error(f"serial disconnected or failed: {exc}")
        self.warn("Port is closed. Fix the cable/device and use :open PORT to reconnect.")

    def _handle_rx(self, data: bytes) -> None:
        if self.state.hex_mode:
            self._flush_rx_line_buffer()
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            payload = bytes_to_hex(data)
            self.logger.write_rx(payload)
            self.out(f"[{timestamp}] RX {payload}", style="ansicyan")
            return

        payload = self.rx_decoder.decode(data)
        if not payload:
            return
        self.logger.write_rx(payload)
        if self.raw_stream:
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self.out(f"[{timestamp}] RX {payload}", style="ansicyan")
        else:
            self._buffer_rx_text(payload)

    def _buffer_rx_text(self, payload: str) -> None:
        if not self.rx_line_buffer:
            self.rx_line_timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.rx_line_buffer += payload

        parts = self.rx_line_buffer.split("\n")
        complete_lines = parts[:-1]
        self.rx_line_buffer = parts[-1]

        for line in complete_lines:
            self._print_rx_line(line.rstrip("\r"))
            self.rx_line_timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        if self.rx_line_buffer:
            self._schedule_rx_line_flush()
        else:
            self._cancel_rx_line_flush()

    def _schedule_rx_line_flush(self) -> None:
        self._cancel_rx_line_flush()
        if self.loop:
            self.rx_flush_handle = self.loop.call_later(0.05, self._flush_rx_line_buffer)

    def _cancel_rx_line_flush(self) -> None:
        if self.rx_flush_handle:
            self.rx_flush_handle.cancel()
            self.rx_flush_handle = None

    def _flush_rx_line_buffer(self) -> None:
        self._cancel_rx_line_flush()
        if not self.rx_line_buffer:
            return
        self._print_rx_line(self.rx_line_buffer)
        self.rx_line_buffer = ""

    def _print_rx_line(self, line: str) -> None:
        timestamp = self.rx_line_timestamp or datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.out(f"[{timestamp}] RX {line}", style="ansicyan")

    def default_log_path(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(Path.cwd() / "logs" / f"serial_{stamp}.log")

    def _make_stm32_isp(self, ser) -> Stm32Isp:
        return Stm32Isp(
            ser,
            timeout=self.transfer_options.timeout,
            reset_config=self.reset_config,
            packet_delay=self.transfer_options.packet_delay,
            protocol_log=self._protocol_log if self.verbose_protocol else None,
        )

    def _protocol_log(self, direction: str, payload: bytes, note: str) -> None:
        self.logger.write_protocol(direction, payload, note)

    def _build_reset_config(self, args: argparse.Namespace) -> Stm32ResetConfig:
        base = BOARD_PRESETS[args.board]
        return Stm32ResetConfig(
            dtr_polarity=LinePolarity(args.dtr_polarity) if args.dtr_polarity else base.dtr_polarity,
            rts_polarity=LinePolarity(args.rts_polarity) if args.rts_polarity else base.rts_polarity,
            boot0_asserted=base.boot0_asserted,
            reset_asserted=base.reset_asserted,
            boot0_release=base.boot0_release,
            reset_release=base.reset_release,
            pulse_seconds=args.reset_pulse / 1000.0,
            settle_seconds=args.reset_settle / 1000.0,
        )

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        seconds = int(seconds)
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{sec:02d}"
        return f"{minutes:02d}:{sec:02d}"

    def out(self, message: str, style: str | None = None) -> None:
        safe_message = self._terminal_safe_text(message)
        if style:
            print_formatted_text(HTML(f"<{style}>{self._escape_xml_text(safe_message)}</{style}>"))
        else:
            print_formatted_text(safe_message)

    def info(self, message: str) -> None:
        self.out(message, "ansiblue")

    def ok(self, message: str) -> None:
        self.out(message, "ansigreen")

    def warn(self, message: str) -> None:
        self.out(message, "ansiyellow")

    def error(self, message: str) -> None:
        self.out(message, "ansired")

    @staticmethod
    def _escape_xml_text(message: str) -> str:
        return message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @classmethod
    def _terminal_safe_text(cls, message: str) -> str:
        """Make arbitrary serial text safe for terminal rendering and XML HTML().

        prompt_toolkit.HTML() is parsed as XML. XML 1.0 rejects most C0 control
        characters and surrogate code points, so raw serial garbage can crash
        minidom before anything reaches the screen. Keep printable text and
        render unsafe control characters visibly as escape tokens.
        """

        return "".join(cls._safe_char(ch) for ch in message)

    @staticmethod
    def _safe_char(ch: str) -> str:
        code = ord(ch)
        if ch in {"\n", "\r", "\t"}:
            return ch
        if code < 0x20 or 0x7F <= code <= 0x9F:
            return f"\\x{code:02X}"
        if 0xD800 <= code <= 0xDFFF or code in {0xFFFE, 0xFFFF}:
            return f"\\u{code:04X}"
        return ch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cross-platform serial debug terminal")
    parser.add_argument("--port", help="Serial port, e.g. COM3 or /dev/ttyUSB0")
    parser.add_argument("--baudrate", "--baud", type=int, default=115200)
    parser.add_argument("--bytesize", type=int, choices=[5, 6, 7, 8], default=8)
    parser.add_argument("--parity", choices=["N", "E", "O", "M", "S"], default="N")
    parser.add_argument("--stopbits", type=float, choices=[1, 1.5, 2], default=1)
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--hex", action="store_true", help="Start in HEX mode")
    parser.add_argument("--demo", action="store_true", help="Use an internal mock MCU instead of a real serial port")
    parser.add_argument("--raw-stream", action="store_true", help="Print each Rx chunk immediately instead of line-buffering")
    parser.add_argument("--transfer-timeout", type=float, default=3.0, help="Transfer ACK timeout in seconds")
    parser.add_argument("--transfer-retries", type=int, default=5, help="Retries per transfer packet")
    parser.add_argument("--packet-delay", type=float, default=0.0, help="Delay between protocol packets in milliseconds")
    parser.add_argument("--verbose-protocol", action="store_true", help="Write low-level Tx/Rx protocol frames to log")
    parser.add_argument("--board", choices=sorted(BOARD_PRESETS), default="generic", help="STM32 reset wiring preset")
    parser.add_argument("--dtr-polarity", choices=["normal", "inverted"], help="Override BOOT0/DTR polarity")
    parser.add_argument("--rts-polarity", choices=["normal", "inverted"], help="Override RESET/RTS polarity")
    parser.add_argument("--reset-pulse", type=float, default=100.0, help="RESET pulse width in milliseconds")
    parser.add_argument("--reset-settle", type=float, default=200.0, help="Bootloader settle time in milliseconds")
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = SerialDebugApp(args)
    return await app.run()
