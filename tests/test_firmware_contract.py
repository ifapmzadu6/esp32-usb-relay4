"""Exercise the real firmware parser and the CLI together on a host PTY."""

import os
from pathlib import Path
import pty
import subprocess
import sys
import tty
import unittest


CLI = (
    Path(__file__).resolve().parents[1]
    / ".agents/skills/relay-control/scripts/relay_cli.py"
)
MOCK_PATH = os.environ.get("RELAY_FIRMWARE_MOCK")


@unittest.skipUnless(MOCK_PATH, "set RELAY_FIRMWARE_MOCK to the compiled host firmware")
class FirmwareContractTest(unittest.TestCase):
    def setUp(self):
        self.firmware = Path(MOCK_PATH).resolve()

    def test_firmware_discards_abandoned_frames(self):
        subprocess.run([self.firmware, "--check-framing"], check=True, timeout=5)

    def test_cli_accepts_real_firmware_responses(self):
        commands = ["INFO", "HELP", "ALL ON", "ALL OFF"]
        expected_status = []
        expected_skipped_pins = []
        for mask in range(16):
            active = [bool(mask & (1 << bit)) for bit in range(4)]
            channels = [f"{'ON' if on else 'OFF'} {idx}" for idx, on in enumerate(active, 1)]
            commands.extend([*channels, "STATUS", "DIAG"])
            status = [f"{idx}:{'ON' if on else 'OFF'}" for idx, on in enumerate(active, 1)]
            expected_status.append("< STATUS " + " ".join(status))
            expected_skipped_pins.append({pin for pin, on in zip((2, 3, 4, 10), active) if on})
        test_pins = (0, 1, 5, 6, 7, 8, 9, 20, 21)
        commands.extend(f"TEST {pin}" for pin in test_pins)

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        tty.setraw(slave)
        with subprocess.Popen([self.firmware], stdin=master, stdout=master) as firmware:
            try:
                result = subprocess.run(
                    [sys.executable, CLI, "--port", os.ttyname(slave), *commands],
                    capture_output=True, text=True, timeout=15,
                )
            finally:
                if firmware.poll() is None:
                    firmware.terminate()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual([line for line in lines if line.startswith("< STATUS ")], expected_status)
        diag_lines = [line for line in lines if line.startswith("DIAG ")]
        skipped_pins = [
            {int(field[:-2]) for field in line.split()[1:] if field.endswith("=-")}
            for line in diag_lines
        ]
        self.assertEqual(skipped_pins, expected_skipped_pins)
        received_test_pins = [int(line.split()[2]) for line in lines if line.startswith("< TEST ")]
        self.assertEqual(received_test_pins, list(test_pins))
