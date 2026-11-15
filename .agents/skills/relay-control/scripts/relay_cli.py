#!/usr/bin/env python3
"""Zero-dependency USB serial CLI for esp32-usb-relay4."""

import argparse
import glob
import os
import select
import sys
import termios
import time
import tty


def find_default_port() -> str | None:
    patterns = ["/dev/cu.usbmodem*", "/dev/ttyACM*", "/dev/ttyUSB*"]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


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


def read_response(fd: int, timeout: float = 0.25, expected_lines: int = 1) -> str:
    out = bytearray()
    end = time.time() + timeout
    while time.time() < end:
        remaining = max(0.01, end - time.time())
        ready, _, _ = select.select([fd], [], [], remaining)
        if ready:
            try:
                chunk = os.read(fd, 1024)
                if chunk:
                    out.extend(chunk)
                    if out.count(b"\n") >= expected_lines:
                        break
            except BlockingIOError:
                pass
    return out.decode(errors="replace").strip()


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

    port = args.port or find_default_port()
    if not port:
        print("ERR: No USB serial port found (/dev/cu.usbmodem* or /dev/ttyACM*).", file=sys.stderr)
        return 1

    had_error = False
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        configure_serial(fd, args.baud)
        # Drain any initial boot message if the port open triggered output
        init_msg = read_response(fd, timeout=0.1)
        if init_msg:
            print(f"[init] {init_msg}")

        for idx, cmd in enumerate(args.commands):
            clean_cmd = cmd.strip()
            expected_lines = 2 if clean_cmd.upper() == "DIAG" else 1
            os.write(fd, (clean_cmd + "\n").encode())
            resp = read_response(fd, timeout=args.timeout, expected_lines=expected_lines)
            print(f"> {clean_cmd}\n< {resp}")
            if not resp or resp.startswith("ERR "):
                had_error = True
            if args.wait > 0 and idx + 1 < len(args.commands):
                time.sleep(args.wait)
    finally:
        os.close(fd)

    return 2 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
