"""Regression tests for the relay CLI over a local pseudo-terminal."""

import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import termios
import unittest


CLI = (
    Path(__file__).resolve().parents[1]
    / ".agents/skills/relay-control/scripts/relay_cli.py"
)
RELAY_INFO = b"INFO esp32-usb-relay4 PROTOCOL 1"
LIVE = b"LIVE 2=0 3=1 4=1 10=1"
DIAG = b"DIAG 0=0 1=0 2=- 3=0 4=0 5=0 6=0 7=0 8=1 9=1 10=0 20=0 21=0"


class RelayCLITest(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.cli = [sys.executable, str(CLI), "--port", os.ttyname(self.slave), "-t", "1"]

    def start_cli(self, *commands):
        return subprocess.Popen(
            [*self.cli, *commands], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

    def read_request(self, command):
        frame = bytearray()
        while not frame.endswith(b"\n"):
            self.assertTrue(select.select([self.master], [], [], 3)[0], frame)
            chunk = os.read(self.master, 1024)
            self.assertTrue(chunk)
            frame.extend(chunk)
        self.assertRegex(frame[:35], rb"^\x18@[0-9a-f]{32} $")
        self.assertEqual(frame[35:], command + b"\n")
        return bytes(frame[1:35])

    def send_reply(self, header, payload):
        os.write(self.master, header + payload + b"\r\n")

    def verify_device(self):
        self.send_reply(self.read_request(b"INFO"), RELAY_INFO)

    def assert_no_pending_request(self):
        self.assertFalse(select.select([self.master], [], [], 0)[0])

    def test_stops_batch_on_device_error(self):
        with self.start_cli("TEST 5", "ALL ON") as process:
            self.verify_device()
            header = self.read_request(b"TEST 5")
            self.send_reply(header, b"ERR INVALID CHARACTER")
            out, err = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 2)
            self.assertIn("ERR INVALID CHARACTER", out)
            self.assertEqual(err, "")
            self.assert_no_pending_request()

    def test_competing_cli_fails_and_port_is_reusable(self):
        with self.start_cli("STATUS") as first:
            self.verify_device()
            header = self.read_request(b"STATUS")
            second = subprocess.run(
                [*self.cli, "ALL ON"], capture_output=True, text=True, timeout=3
            )
            self.assertEqual(second.returncode, 1, second.stderr)
            self.assert_no_pending_request()
            self.send_reply(header, b"STATUS 1:OFF 2:OFF 3:OFF 4:OFF")
            first.communicate(timeout=3)
            self.assertEqual(first.returncode, 0)

        with self.start_cli("STATUS") as third:
            self.verify_device()
            header = self.read_request(b"STATUS")
            self.send_reply(header, b"STATUS 1:OFF 2:OFF 3:OFF 4:OFF")
            third.communicate(timeout=3)
            self.assertEqual(third.returncode, 0)

    def test_sets_serial_mode_and_restores_settings_on_success_or_timeout(self):
        flow = termios.IXON | termios.IXOFF | termios.IXANY
        extra_control = termios.CSTOPB | getattr(termios, "CRTSCTS", 0)
        original = termios.tcgetattr(self.slave)
        original[0] |= flow
        original[2] |= extra_control
        original[3] |= termios.ECHO | termios.ICANON
        original[4] = original[5] = termios.B9600
        termios.tcsetattr(self.slave, termios.TCSANOW, original)
        original = termios.tcgetattr(self.slave)
        # BSD can set PENDIN when switching back to canonical input.
        pending_input = getattr(termios, "PENDIN", 0)
        original[3] &= ~pending_input

        for should_reply in (True, False):
            with self.subTest(should_reply=should_reply), self.start_cli("STATUS") as process:
                self.verify_device()
                header = self.read_request(b"STATUS")
                active = termios.tcgetattr(self.slave)
                self.assertEqual(active[0] & flow, 0)
                self.assertEqual(active[2] & extra_control, 0)
                self.assertEqual(active[2] & termios.CSIZE, termios.CS8)
                self.assertEqual(active[3] & (termios.ECHO | termios.ICANON), 0)
                self.assertEqual(active[4:6], [termios.B115200, termios.B115200])
                if should_reply:
                    self.send_reply(header, b"STATUS 1:OFF 2:OFF 3:OFF 4:OFF")
                _, err = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0 if should_reply else 2, err)
                restored = termios.tcgetattr(self.slave)
                restored[3] &= ~pending_input
                self.assertEqual(restored, original)

    def test_rejects_wrong_device_or_protocol_before_sending_commands(self):
        for reply in (b"INFO other-device PROTOCOL 1", b"INFO esp32-usb-relay4 PROTOCOL 2",
                      b"ERR UNKNOWN COMMAND"):
            with self.subTest(reply=reply), self.start_cli("ALL ON") as process:
                self.send_reply(self.read_request(b"INFO"), reply)
                out, err = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 1)
                self.assertEqual(out, "")
                self.assertIn("Incompatible relay device", err)
                self.assert_no_pending_request()

    def test_rejects_stale_or_untagged_device_info(self):
        with self.start_cli("--startup-timeout", "0.15", "ALL ON") as process:
            header = self.read_request(b"INFO")
            stale = b"@0" + header[2:] if header[1:2] != b"0" else b"@1" + header[2:]
            self.send_reply(stale, RELAY_INFO)
            self.send_reply(b"", RELAY_INFO)
            out, err = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(out, "")
            self.assertIn("Could not verify relay device", err)
            self.assert_no_pending_request()

    def test_retries_startup_probe_and_ignores_first_probe_reply(self):
        with self.start_cli("ON 1") as process:
            first = self.read_request(b"INFO")
            os.write(self.master, first + b"INFO incomplete")
            second = self.read_request(b"INFO")
            self.assertNotEqual(first, second)
            self.send_reply(b"", b"READY esp32-usb-relay4")
            self.send_reply(first, RELAY_INFO)
            self.send_reply(second, RELAY_INFO)
            self.send_reply(self.read_request(b"ON 1"), b"OK ON 1")
            out, err = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, err)
            self.assertIn("OK ON 1", out)

    def test_startup_deadline_stops_unanswered_probes(self):
        with self.start_cli("--startup-timeout", "0.45", "ALL ON") as process:
            first = self.read_request(b"INFO")
            self.assertNotEqual(first, self.read_request(b"INFO"))
            _, err = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 1)
            self.assertIn("Could not verify relay device", err)
            self.assert_no_pending_request()

    def test_does_not_retry_control_command_that_times_out(self):
        with self.start_cli("ON 1", "STATUS") as process:
            self.verify_device()
            self.read_request(b"ON 1")
            _, err = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 2)
            self.assertIn("ON 1: Timed out", err)
            self.assert_no_pending_request()

    def test_invalid_later_command_prevents_any_serial_requests(self):
        for invalid in ("OFF 5", "ALL OF", "TEST 2", "TEST 11", "TEST +5",
                        "ON " + " " * 60 + "1", "ON １", "STATUS\n"):
            with self.subTest(command=invalid):
                result = subprocess.run(
                    [*self.cli, "ON 1", invalid], capture_output=True, text=True, timeout=3
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("command 2", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assert_no_pending_request()

    def test_validation_allows_firmware_whitespace_and_numeric_pin_forms(self):
        longest = "on " + "\t" * 59 + "1"
        with self.start_cli(f" {longest} ", "test  005") as process:
            self.verify_device()
            self.send_reply(self.read_request(longest.encode()), b"OK ON 1")
            self.send_reply(
                self.read_request(b"test  005"),
                b"TEST 5 PP_LOW=0 PP_HIGH=1 OD_LOW=0 OD_HIGH=1",
            )
            out, err = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, err)
            self.assertIn("TEST 5 PP_LOW=", out)

    def test_rejects_malformed_replies_before_sending_next_command(self):
        cases = (
            ("STATUS", [b"STATUS 1:ON 2:OFF 3:OFF"]),
            ("STATUS", [b"STATUS 1:ON 2:OFF 2:OFF 4:OFF"]),
            ("STATUS", [b"STATUS 1:ON 2:OFF 3:OFF 4:UNKNOWN"]),
            ("DIAG", [b"LIVE 2=0 3=1 4=1"]),
            ("DIAG", [LIVE, DIAG.replace(b"5=0", b"5=-")]),
            ("TEST 5", [b"TEST 5 PP_LOW=0 PP_HIGH=1 OD_LOW=0"]),
        )
        for command, replies in cases:
            with self.subTest(command=command, replies=replies):
                with self.start_cli(command, "ALL ON") as process:
                    self.verify_device()
                    header = self.read_request(command.encode())
                    for reply in replies:
                        self.send_reply(header, reply)
                    out, err = process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 2)
                    self.assertEqual(out, "")
                    self.assertIn("Unexpected response", err)
                    self.assert_no_pending_request()

    def test_accepts_complete_diag_sent_in_chunks(self):
        with self.start_cli("DIAG") as process:
            self.verify_device()
            header = self.read_request(b"DIAG")
            self.send_reply(header, LIVE)
            os.write(self.master, header + DIAG[:24])
            os.write(self.master, DIAG[24:] + b"\r\n")
            out, err = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, err)
            self.assertIn(DIAG.decode(), out)

    def test_rejects_overlong_line_with_or_without_newline(self):
        for terminator in (b"", b"\n"):
            with self.subTest(terminator=terminator), self.start_cli("STATUS", "ALL ON") as process:
                self.verify_device()
                header = self.read_request(b"STATUS")
                os.write(self.master, header + b"STATUS " + b"x" * 256 + terminator)
                _, err = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 2)
                self.assertIn("Response line exceeds 256 bytes", err)
                self.assert_no_pending_request()
