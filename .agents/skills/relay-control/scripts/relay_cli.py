#!/usr/bin/env python3
"""Zero-dependency USB serial CLI for esp32-usb-relay4."""

import argparse
from contextlib import contextmanager, suppress
import fcntl
import glob
import math
import os
import secrets
import select
import sys
import termios
import time
import tty


CANCEL_FRAME = "\x18"


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


def expected_responses(command: str) -> tuple[str, ...]:
    command = command.upper()
    if command == "DIAG":
        return ("LIVE ", "DIAG ")
    if command == "STATUS":
        return ("STATUS ",)
    if command in ("HELP", "?"):
        return ("COMMANDS: ",)
    if command in ("ALL ON", "ALL OFF"):
        return (f"OK {command}",)
    verb, _, arg = command.partition(" ")
    if verb in ("ON", "OFF") and arg.lstrip() in ("1", "2", "3", "4"):
        return (f"OK {verb} {arg.lstrip()}",)
    if verb == "TEST":
        return ("TEST ",)
    return ()


def read_response(
    fd: int, buffer: bytearray, deadline: float, header: str, command: str
) -> str:
    expected = expected_responses(command)
    prefix = header.encode("ascii")
    lines = []
    while True:
        newline = buffer.find(b"\n")
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
        line = raw_line[len(prefix):].decode(errors="replace").strip()
        if not lines and line.startswith("ERR "):
            return line
        pattern = expected[len(lines)] if len(lines) < len(expected) else None
        if pattern is None or not (
            line.startswith(pattern) if pattern.endswith(" ") else line == pattern
        ):
            raise ValueError(f"Unexpected response: {line}")
        lines.append(line)
        if len(lines) == len(expected):
            return "\n".join(lines)


def run_commands(fd: int, commands: list[str], timeout: float, wait: float) -> int:
    buffer = bytearray()
    for idx, cmd in enumerate(commands):
        clean_cmd = cmd.strip()
        header = f"@{secrets.token_hex(16)} "
        deadline = time.monotonic() + timeout
        try:
            write_command(fd, header + clean_cmd, deadline)
            resp = read_response(fd, buffer, deadline, header, clean_cmd)
        except (OSError, ValueError) as exc:
            print(f"ERR: {clean_cmd}: {exc}", file=sys.stderr)
            return 2
        print(f"> {clean_cmd}\n< {resp}")
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
    if any(
        not cmd.strip() or any(c in cmd for c in ("\r", "\n", "\0", CANCEL_FRAME))
        for cmd in args.commands
    ):
        parser.error("each command must be a nonempty line without CR, LF, NUL, or CAN")

    try:
        port = args.port or find_default_port()
        with exclusive_serial(port, args.baud) as fd:
            return run_commands(fd, args.commands, args.timeout, args.wait)
    except (OSError, ValueError, termios.error) as exc:
        print(f"ERR: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
