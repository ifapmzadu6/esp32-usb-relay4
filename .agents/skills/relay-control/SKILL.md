---
name: relay-control
description: >-
  Use this skill when the user asks to check, toggle, diagnose, or test the
  4-channel USB relay module (esp32-usb-relay4) over USB serial, or to build
  and upload its ESP32-C3 PlatformIO firmware.
---

# ESP32 USB Relay 4-Channel Control

Controls and diagnoses the `esp32-usb-relay4` ESP32-C3 firmware over USB CDC serial (`115200 bps`).

## Hardware & Pin Summary

- **Board**: `esp32-c3-devkitm-1` (`platformio.ini` env: `esp32-c3-mini-1`)
- **Relay Channels 1–4**: `GPIO2`, `GPIO3`, `GPIO4`, `GPIO10`
- **Drive Mode**: Active-Low + `OUTPUT_OPEN_DRAIN` (designed for 5V opto-isolated relay modules with the `L` jumper)

## Controlling Relays via Serial

Use the zero-dependency helper script [relay_cli.py](./scripts/relay_cli.py) (no `pyserial` required). It auto-detects `/dev/cu.usbmodem*` (macOS) or `/dev/ttyACM*` (Linux).

The CLI requires firmware that echoes request IDs. If a control command times out, run `STATUS` to check whether it took effect.

### Common Commands

```bash
# Check current relay states
python3 .agents/skills/relay-control/scripts/relay_cli.py STATUS

# Turn individual channels ON or OFF (1-4)
python3 .agents/skills/relay-control/scripts/relay_cli.py "ON 1" "ON 2" STATUS

# Turn all channels ON or OFF
python3 .agents/skills/relay-control/scripts/relay_cli.py "ALL OFF" STATUS

# Sequence with delay between commands (e.g. 0.5s)
python3 .agents/skills/relay-control/scripts/relay_cli.py -w 0.5 "ON 1" "ON 2" "ON 3" "ON 4" "ALL OFF"
```

### Diagnostics & Pin Testing

```bash
# Check live relay levels and probe pullups (energized relay pins show "-" in DIAG)
python3 .agents/skills/relay-control/scripts/relay_cli.py DIAG

# Test Push-Pull and Open-Drain drive/readback on an unused, disconnected GPIO
python3 .agents/skills/relay-control/scripts/relay_cli.py "TEST 5"
```

## Verification

- Every control command should return `OK ...` (`OK ON <n>`, `OK OFF <n>`, `OK ALL ON`, `OK ALL OFF`).
- Always follow state-changing sequences with `STATUS` to confirm the final state (`STATUS 1:<ON|OFF> 2:<ON|OFF> 3:<ON|OFF> 4:<ON|OFF>`).

## Building & Uploading Firmware

If `src/main.cpp` or `platformio.ini` is modified:

```bash
pio run
pio run -t upload
```
