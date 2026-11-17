"""Regression tests for the relay CLI over a local pseudo-terminal."""

import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import unittest


CLI = (
    Path(__file__).resolve().parents[1]
    / ".agents/skills/relay-control/scripts/relay_cli.py"
)


class RelayCLITest(unittest.TestCase):
    def setUp(self):
        self.master, slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, slave)
        self.cli = [sys.executable, str(CLI), "--port", os.ttyname(slave), "-t", "1"]

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

    def assert_no_pending_request(self):
        self.assertFalse(select.select([self.master], [], [], 0)[0])

    def test_stops_batch_on_device_error(self):
        with self.start_cli("TEST 2", "ALL ON") as process:
            header = self.read_request(b"TEST 2")
            self.send_reply(header, b"ERR RELAY PIN")
            out, err = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 2)
            self.assertIn("ERR RELAY PIN", out)
            self.assertEqual(err, "")
            self.assert_no_pending_request()

    def test_competing_cli_fails_and_port_is_reusable(self):
        with self.start_cli("STATUS") as first:
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
            header = self.read_request(b"STATUS")
            self.send_reply(header, b"STATUS 1:OFF 2:OFF 3:OFF 4:OFF")
            third.communicate(timeout=3)
            self.assertEqual(third.returncode, 0)
