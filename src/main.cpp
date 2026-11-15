#include <Arduino.h>
#include <cctype>
#include <cstring>

namespace {

constexpr size_t kRelayCount = 4;

// Relay channels 1..4 mapped to GPIO2, GPIO3, GPIO4, GPIO10.
constexpr uint8_t kRelayPins[kRelayCount] = {2, 3, 4, 10};

// The 5V opto-isolated module does not trigger reliably from a 3.3V HIGH
// (H jumper): the input LED and optocoupler LED are in series and need more
// than 3.3V can supply. Use the L jumper instead: the GPIO sinks current to
// turn a relay ON and is left floating (open-drain) to turn it OFF, so the
// 3.3V pin never has to fight the module's 5V rail.
constexpr bool kRelayActiveLow = true;
constexpr uint8_t kRelayPinMode = kRelayActiveLow ? OUTPUT_OPEN_DRAIN : OUTPUT;

constexpr size_t kCmdBufSize = 64;

bool relayStates[kRelayCount] = {false, false, false, false};
char cmdBuf[kCmdBufSize];
size_t cmdLen = 0;

uint8_t logicLevel(bool on) {
  const bool high = kRelayActiveLow ? !on : on;
  return high ? HIGH : LOW;
}

void setRelay(size_t index, bool on) {
  relayStates[index] = on;
  digitalWrite(kRelayPins[index], logicLevel(on));
}

void setAllRelays(bool on) {
  for (size_t i = 0; i < kRelayCount; ++i) {
    setRelay(i, on);
  }
}

void printStatus() {
  Serial.print("STATUS");
  for (size_t i = 0; i < kRelayCount; ++i) {
    Serial.print(' ');
    Serial.print(i + 1);
    Serial.print(':');
    Serial.print(relayStates[i] ? "ON" : "OFF");
  }
  Serial.println();
}

void printHelp() {
  Serial.println("COMMANDS: ON <1-4> | OFF <1-4> | ALL ON | ALL OFF | STATUS | DIAG | HELP");
}

// Wiring check: releases each usable GPIO, applies the weak internal pulldown
// and reads it. A pin wired to a powered module input (L jumper) is pulled up
// through the module and reads 1; an unconnected pin reads 0.
// GPIO8/9 usually read 1 because of on-board pullups.
void printDiag() {
  static const uint8_t pins[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 21};
  // Actual pin levels while the relay outputs are still being driven.
  Serial.print("LIVE");
  for (size_t i = 0; i < kRelayCount; ++i) {
    Serial.print(' ');
    Serial.print(kRelayPins[i]);
    Serial.print('=');
    Serial.print(digitalRead(kRelayPins[i]));
  }
  Serial.println();
  Serial.print("DIAG");
  for (const uint8_t pin : pins) {
    pinMode(pin, INPUT_PULLDOWN);
    delay(5);
    const int level = digitalRead(pin);
    pinMode(pin, INPUT);
    Serial.print(' ');
    Serial.print(pin);
    Serial.print('=');
    Serial.print(level);
  }
  Serial.println();
  for (size_t i = 0; i < kRelayCount; ++i) {
    digitalWrite(kRelayPins[i], logicLevel(relayStates[i]));
    pinMode(kRelayPins[i], kRelayPinMode);
  }
}

// Output check: drives a pin push-pull and open-drain, reading back each level.
void printPinTest(uint8_t pin) {
  Serial.print("TEST ");
  Serial.print(pin);
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delay(5);
  Serial.print(" PP_LOW=");
  Serial.print(digitalRead(pin));
  digitalWrite(pin, HIGH);
  delay(5);
  Serial.print(" PP_HIGH=");
  Serial.print(digitalRead(pin));
  pinMode(pin, OUTPUT_OPEN_DRAIN);
  digitalWrite(pin, LOW);
  delay(5);
  Serial.print(" OD_LOW=");
  Serial.print(digitalRead(pin));
  digitalWrite(pin, HIGH);
  delay(5);
  Serial.print(" OD_HIGH=");
  Serial.println(digitalRead(pin));
  pinMode(pin, INPUT);
  for (size_t i = 0; i < kRelayCount; ++i) {
    digitalWrite(kRelayPins[i], logicLevel(relayStates[i]));
    pinMode(kRelayPins[i], kRelayPinMode);
  }
}

void normalizeCommand(char *buf) {
  // Trim leading whitespace
  size_t start = 0;
  while (buf[start] != '\0' && std::isspace(static_cast<unsigned char>(buf[start]))) {
    ++start;
  }
  if (start > 0) {
    std::memmove(buf, buf + start, std::strlen(buf + start) + 1);
  }

  // Trim trailing whitespace
  size_t len = std::strlen(buf);
  while (len > 0 && std::isspace(static_cast<unsigned char>(buf[len - 1]))) {
    buf[--len] = '\0';
  }

  // Uppercase in-place
  for (size_t i = 0; i < len; ++i) {
    buf[i] = static_cast<char>(std::toupper(static_cast<unsigned char>(buf[i])));
  }
}

bool parseChannel(const char *arg, size_t *outIndex) {
  if (arg == nullptr) {
    return false;
  }
  while (*arg != '\0' && std::isspace(static_cast<unsigned char>(*arg))) {
    ++arg;
  }
  if (arg[0] < '1' || arg[0] > static_cast<char>('0' + kRelayCount) || arg[1] != '\0') {
    return false;
  }
  *outIndex = static_cast<size_t>(arg[0] - '1');
  return true;
}

void handleCommand(char *line) {
  normalizeCommand(line);
  if (line[0] == '\0') {
    return;
  }

  if (std::strcmp(line, "STATUS") == 0) {
    printStatus();
    return;
  }
  if (std::strcmp(line, "ALL ON") == 0) {
    setAllRelays(true);
    Serial.println("OK ALL ON");
    return;
  }
  if (std::strcmp(line, "ALL OFF") == 0) {
    setAllRelays(false);
    Serial.println("OK ALL OFF");
    return;
  }
  if (std::strncmp(line, "TEST ", 5) == 0) {
    printPinTest(static_cast<uint8_t>(atoi(line + 5)));
    return;
  }
  if (std::strcmp(line, "DIAG") == 0) {
    printDiag();
    return;
  }
  if (std::strcmp(line, "HELP") == 0 || std::strcmp(line, "?") == 0) {
    printHelp();
    return;
  }

  if (std::strncmp(line, "ON ", 3) == 0) {
    size_t idx = 0;
    if (!parseChannel(line + 3, &idx)) {
      Serial.println("ERR INVALID CHANNEL (expected 1-4)");
      return;
    }
    setRelay(idx, true);
    Serial.print("OK ON ");
    Serial.println(idx + 1);
    return;
  }

  if (std::strncmp(line, "OFF ", 4) == 0) {
    size_t idx = 0;
    if (!parseChannel(line + 4, &idx)) {
      Serial.println("ERR INVALID CHANNEL (expected 1-4)");
      return;
    }
    setRelay(idx, false);
    Serial.print("OK OFF ");
    Serial.println(idx + 1);
    return;
  }

  Serial.println("ERR UNKNOWN COMMAND");
}

}  // namespace

void setup() {
  for (size_t i = 0; i < kRelayCount; ++i) {
    // Latch the inactive level before enabling the output driver so relays
    // stay OFF across a reset. digitalWrite() is ignored until the pin has been
    // claimed with pinMode(), so claim it as a plain input first.
    pinMode(kRelayPins[i], INPUT);
    digitalWrite(kRelayPins[i], logicLevel(false));
    pinMode(kRelayPins[i], kRelayPinMode);
  }

  Serial.begin(115200);
  const unsigned long waitStart = millis();
  while (!Serial && (millis() - waitStart < 1500)) {
    delay(10);
  }
  Serial.println("READY esp32-usb-relay4");
}

void loop() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\r') {
      continue;
    }
    if (c == '\n') {
      cmdBuf[cmdLen] = '\0';
      handleCommand(cmdBuf);
      cmdLen = 0;
      continue;
    }
    if (cmdLen + 1 < kCmdBufSize) {
      cmdBuf[cmdLen++] = c;
    } else {
      // Discard overflowed line until next newline
      cmdLen = 0;
      Serial.println("ERR COMMAND TOO LONG");
      while (Serial.available() > 0 && Serial.read() != '\n') {
      }
    }
  }
}
