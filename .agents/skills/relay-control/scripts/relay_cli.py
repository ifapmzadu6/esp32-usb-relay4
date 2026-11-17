#!/usr/bin/env python3
"""Zero-dependency USB serial CLI for esp32-usb-relay4."""

import argparse
import glob
import math
import os
import select
import sys
import termios
import time
import tty


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


def write_command(fd: int, command: str, deadline: float) -> None:
    pending = memoryview((command + "\n").encode())
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


def read_response(fd: int, buffer: bytearray, deadline: float, *, diag: bool) -> str:
    lines = []
    while True:
        newline = buffer.find(b"\n")
        if newline < 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError("Timed out waiting for a complete response")
            try:
                chunk = os.read(fd, 1024)
            except BlockingIOError:
                continue
            if not chunk:
                raise OSError("Serial port closed while waiting for a response")
            buffer.extend(chunk)
            continue

        line = buffer[:newline].decode(errors="replace").strip()
        del buffer[:newline + 1]
        if not line or line.startswith("READY "):
            continue
        if not lines and line.startswith("ERR "):
            return line
        if not diag:
            return line
        prefix = "DIAG " if lines else "LIVE "
        if not line.startswith(prefix):
            raise ValueError(f"Unexpected diagnostic response: {line}")
        lines.append(line)
        if len(lines) == 2:
            return "\n".join(lines)


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
    if any(not cmd.strip() or any(c in cmd for c in "\r\n\0") for cmd in args.commands):
        parser.error("each command must be a nonempty line without CR, LF, or NUL")

    try:
        port = args.port or find_default_port()
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            configure_serial(fd, args.baud)
        except (OSError, ValueError, termios.error):
            os.close(fd)
            raise
    except (OSError, ValueError, termios.error) as exc:
        print(f"ERR: {exc}", file=sys.stderr)
        return 1

    had_error = False
    buffer = bytearray()
    try:
        for idx, cmd in enumerate(args.commands):
            clean_cmd = cmd.strip()
            deadline = time.monotonic() + args.timeout
            try:
                write_command(fd, clean_cmd, deadline)
                resp = read_response(fd, buffer, deadline, diag=clean_cmd.upper() == "DIAG")
            except (OSError, ValueError) as exc:
                print(f"ERR: {clean_cmd}: {exc}", file=sys.stderr)
                return 2
            print(f"> {clean_cmd}\n< {resp}")
            if resp.startswith("ERR "):
                had_error = True
            if args.wait > 0 and idx + 1 < len(args.commands):
                time.sleep(args.wait)
    finally:
        os.close(fd)

    return 2 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
