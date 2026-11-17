#!/usr/bin/env python3
"""Zero-dependency USB serial CLI for esp32-usb-relay4."""

import argparse
from contextlib import contextmanager, suppress
import fcntl
import glob
import math
import os
import re
import secrets
import select
import sys
import termios
import time
import tty


CANCEL_FRAME = "\x18"
EXPECTED_DEVICE_INFO = "INFO esp32-usb-relay4 PROTOCOL 1"
COMMAND_MAX_LENGTH = 63
MAX_RESPONSE_LINE_BYTES = 256
# GPIO assignments in protocol 1.
RELAY_PINS = (2, 3, 4, 10)
TEST_PINS = {0, 1, 5, 6, 7, 8, 9, 20, 21}
STATUS_RESPONSE = "STATUS" + "".join(
    f" {channel}:(?:ON|OFF)" for channel in range(1, len(RELAY_PINS) + 1)
)
LIVE_RESPONSE = "LIVE" + "".join(f" {pin}=[01]" for pin in RELAY_PINS)
DIAG_RESPONSE = "DIAG" + "".join(
    f" {pin}=" + ("[01-]" if pin in RELAY_PINS else "[01]")
    for pin in sorted(set(RELAY_PINS) | TEST_PINS)
)
PIN_TEST_FIELDS = " ".join(
    f"{field}=[01]" for field in ("PP_LOW", "PP_HIGH", "OD_LOW", "OD_HIGH")
)


def find_default_port() -> str:
    patterns = ("/dev/cu.usbmodem*", "/dev/ttyACM*", "/dev/ttyUSB*")
    ports = sorted(path for pattern in patterns for path in glob.glob(pattern))
    if not ports:
        raise ValueError("No USB serial port found. Specify a device with --port.")
    if len(ports) > 1:
        raise ValueError(f"Multiple USB serial ports found. Use --port: {', '.join(ports)}")
    return ports[0]


def configure_serial(fd: int, baud: int) -> None:
    baud_map = {
        9600: termios.B9600,
        19200: termios.B19200,
        38400: termios.B38400,
        57600: termios.B57600,
        115200: termios.B115200,
    }
    if baud not in baud_map:
        raise ValueError(f"Unsupported baud rate: {baud}")

    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[4] = attrs[5] = baud_map[baud]
    attrs[2] |= termios.CLOCAL | termios.CREAD
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


@contextmanager
def exclusive_serial(port: str, baud: int):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        # Cover simultaneous CLI opens before the kernel's TTY flag is set.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        try:
            configure_serial(fd, baud)
            yield fd
        finally:
            # The port may already have disappeared due to a disconnect.
            with suppress(OSError):
                fcntl.ioctl(fd, termios.TIOCNXCL)
    finally:
        os.close(fd)


def write_command(fd: int, command: str, deadline: float) -> None:
    pending = memoryview((CANCEL_FRAME + command + "\n").encode())
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
            raise TimeoutError("Timed out writing command")
        try:
            written = os.write(fd, pending)
        except BlockingIOError:
            continue
        if written == 0:
            raise OSError("Serial port stopped accepting data")
        pending = pending[written:]


def response_patterns(command: str) -> tuple[str, ...]:
    command = command.upper()
    if command == "INFO":
        return (r"INFO \S+ PROTOCOL [0-9]+",)
    if command == "DIAG":
        return (LIVE_RESPONSE, DIAG_RESPONSE)
    if command == "STATUS":
        return (STATUS_RESPONSE,)
    if command in ("HELP", "?"):
        return ("COMMANDS: .+",)
    if command in ("ALL ON", "ALL OFF"):
        return (f"OK {command}",)
    verb, _, arg = command.partition(" ")
    arg = arg.lstrip(" \t\v\f")
    if verb in ("ON", "OFF") and arg in ("1", "2", "3", "4"):
        return (f"OK {verb} {arg}",)
    if verb == "TEST" and arg.isdecimal():
        pin = int(arg)
        if pin in TEST_PINS:
            return (f"TEST {pin} {PIN_TEST_FIELDS}",)
    return ()


def validate_commands(commands: list[str]) -> list[str]:
    validated = []
    for index, raw in enumerate(commands, start=1):
        command = raw.strip()
        if not command or any(c in raw for c in ("\r", "\n", "\0", CANCEL_FRAME)):
            raise ValueError(f"command {index} must be a nonempty line without CR, LF, NUL, or CAN")
        if not command.isascii() or len(command) > COMMAND_MAX_LENGTH:
            raise ValueError(f"command {index} must fit in {COMMAND_MAX_LENGTH} ASCII bytes")
        if not response_patterns(command):
            raise ValueError(f"invalid command {index}: {command!r}")
        validated.append(command)
    return validated


def read_response(
    fd: int, buffer: bytearray, deadline: float, header: str, command: str
) -> str:
    expected = response_patterns(command)
    prefix = header.encode("ascii")
    lines = []
    while True:
        newline = buffer.find(b"\n")
        if (newline if newline >= 0 else len(buffer)) > MAX_RESPONSE_LINE_BYTES:
            raise ValueError(f"Response line exceeds {MAX_RESPONSE_LINE_BYTES} bytes")
        if newline < 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError("Timed out waiting for a complete response with the request ID")
            try:
                chunk = os.read(fd, 1024)
            except BlockingIOError:
                continue
            if not chunk:
                raise OSError("Serial port closed while waiting for a response")
            buffer.extend(chunk)
            continue

        raw_line = buffer[:newline]
        del buffer[:newline + 1]
        if not raw_line.startswith(prefix):
            continue
        line = raw_line[len(prefix):].removesuffix(b"\r").decode(errors="replace")
        if not line.isascii() or not line.isprintable():
            raise ValueError(f"Unexpected response: {line!r}")
        if not lines and re.fullmatch("ERR .+", line):
            return line
        if len(lines) == len(expected) or not re.fullmatch(expected[len(lines)], line):
            raise ValueError(f"Unexpected response: {line!r}")
        lines.append(line)
        if len(lines) == len(expected):
            return "\n".join(lines)


def exchange(fd: int, buffer: bytearray, command: str, timeout: float) -> str:
    header = f"@{secrets.token_hex(16)} "
    deadline = time.monotonic() + timeout
    write_command(fd, header + command, deadline)
    return read_response(fd, buffer, deadline, header, command)


def run_commands(fd: int, commands: list[str], timeout: float, wait: float) -> int:
    buffer = bytearray()
    try:
        device_info = exchange(fd, buffer, "INFO", timeout)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not verify relay device: {exc}") from exc
    if device_info != EXPECTED_DEVICE_INFO:
        raise ValueError(f"Incompatible relay device response: {device_info}")

    for idx, command in enumerate(commands):
        try:
            resp = exchange(fd, buffer, command, timeout)
        except (OSError, ValueError) as exc:
            print(f"ERR: {command}: {exc}", file=sys.stderr)
            return 2
        print(f"> {command}\n< {resp}")
        if resp.startswith("ERR "):
            return 2
        if wait > 0 and idx + 1 < len(commands):
            time.sleep(wait)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send serial commands to esp32-usb-relay4."
    )
    parser.add_argument(
        "commands",
        nargs="+",
        help='Commands to send (e.g. "STATUS" "ON 1" "ALL OFF")',
    )
    parser.add_argument(
        "-p", "--port", help="Serial device path (auto-detected if omitted)"
    )
    parser.add_argument(
        "-b", "--baud", type=int, default=115200, help="Baud rate (default: 115200)"
    )
    parser.add_argument(
        "-w",
        "--wait",
        type=float,
        default=0.0,
        help="Delay in seconds between commands (default: 0)",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=0.35,
        help="Response timeout in seconds per command (default: 0.35)",
    )
    args = parser.parse_args()

    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and greater than zero")
    if not math.isfinite(args.wait) or args.wait < 0:
        parser.error("--wait must be finite and nonnegative")
    try:
        commands = validate_commands(args.commands)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        port = args.port or find_default_port()
        with exclusive_serial(port, args.baud) as fd:
            return run_commands(fd, commands, args.timeout, args.wait)
    except (OSError, ValueError, termios.error) as exc:
        print(f"ERR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
