#include <Arduino.h>
#include <atomic>
#include <cctype>
#include <cstdlib>
#include <cstring>

namespace {

constexpr size_t kRelayCount = 4;

// Relay channels 1..4 mapped to GPIO2, GPIO3, GPIO4, GPIO10.
constexpr uint8_t kRelayPins[kRelayCount] = {2, 3, 4, 10};

// User-accessible GPIOs on ESP32-C3 (excludes USB D+/D- and flash pins).
constexpr uint8_t kUsablePins[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 21};

// The 5V opto-isolated module does not trigger reliably from a 3.3V HIGH
// (H jumper): the input LED and optocoupler LED are in series and need more
// than 3.3V can supply. Use the L jumper instead: the GPIO sinks current to
// turn a relay ON and is left floating (open-drain) to turn it OFF, so the
// 3.3V pin never has to fight the module's 5V rail.
constexpr bool kRelayActiveLow = true;
constexpr uint8_t kRelayPinMode = kRelayActiveLow ? OUTPUT_OPEN_DRAIN : OUTPUT;

constexpr size_t kCommandMaxLength = 63;
constexpr size_t kRequestIdLength = 32;
constexpr size_t kRequestHeaderLength = kRequestIdLength + 2;  // '@' and space
constexpr char kCancelFrame = '\x18';  // ASCII CAN
constexpr unsigned long kFrameIdleTimeoutMs = 2000;

enum class FrameState { Normal, Tagged, Discard };

bool relayStates[kRelayCount] = {false, false, false, false};
char cmdBuf[kRequestHeaderLength + kCommandMaxLength + 1];
size_t cmdLen = 0;
FrameState frameState = FrameState::Normal;
bool awaitingLineFeed = false;
unsigned long lastCommandByteAt = 0;
std::atomic<bool> usbResetPending{false};

void onUsbReset(void *, esp_event_base_t, int32_t, void *) {
  usbResetPending.store(true);
}

bool hasPendingFrame() {
  return cmdLen > 0 || frameState == FrameState::Tagged || awaitingLineFeed;
}

void discardFrame() {
  cmdLen = 0;
  frameState = FrameState::Discard;
  awaitingLineFeed = false;
}

void discardPendingInput() {
  const int queued = Serial.available();
  if (hasPendingFrame() || queued > 0) {
    discardFrame();
  }
  for (int i = 0; i < queued; ++i) {
    Serial.read();
  }
}

bool hasRequestHeader(const char *line, size_t length) {
  if (length < kRequestHeaderLength || line[0] != '@' ||
      line[kRequestHeaderLength - 1] != ' ') {
    return false;
  }
  for (size_t i = 1; i <= kRequestIdLength; ++i) {
    if (!std::isxdigit(static_cast<unsigned char>(line[i]))) {
      return false;
    }
  }
  return true;
}

void printReplyPrefix(const char *header) {
  if (header != nullptr) {
    Serial.write(reinterpret_cast<const uint8_t *>(header), kRequestHeaderLength);
  }
}

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
  Serial.println("COMMANDS: ON <1-4> | OFF <1-4> | ALL ON | ALL OFF | STATUS | DIAG | TEST <pin> | INFO | HELP");
}

size_t relayIndexForPin(uint8_t pin) {
  for (size_t i = 0; i < kRelayCount; ++i) {
    if (kRelayPins[i] == pin) {
      return i;
    }
  }
  return kRelayCount;
}

// Wiring check: applies the weak internal pulldown to each pin that isn't
// driving an active relay. A powered module input (L jumper) pulls it high.
// GPIO8/9 usually read 1 because of on-board pullups.
void printDiag(const char *header) {
  // Actual pin levels while the relay outputs are still being driven.
  Serial.print("LIVE");
  for (size_t i = 0; i < kRelayCount; ++i) {
    Serial.print(' ');
    Serial.print(kRelayPins[i]);
    Serial.print('=');
    Serial.print(digitalRead(kRelayPins[i]));
  }
  Serial.println();
  printReplyPrefix(header);
  Serial.print("DIAG");
  for (const uint8_t pin : kUsablePins) {
    Serial.print(' ');
    Serial.print(pin);
    Serial.print('=');
    const size_t relayIndex = relayIndexForPin(pin);
    if (relayIndex < kRelayCount && relayStates[relayIndex]) {
      Serial.print('-');
      continue;
    }

    pinMode(pin, INPUT_PULLDOWN);
    delay(5);
    const int level = digitalRead(pin);
    if (relayIndex < kRelayCount) {
      // The output latch retains the inactive level while in input mode.
      pinMode(pin, kRelayPinMode);
    } else {
      pinMode(pin, INPUT);
    }
    Serial.print(level);
  }
  Serial.println();
}

// Output check: drives a pin push-pull and open-drain, reading back each level.
void printPinTest(uint8_t pin) {
  pinMode(pin, INPUT);
  digitalWrite(pin, LOW);
  pinMode(pin, OUTPUT);
  delay(5);
  const int pushPullLow = digitalRead(pin);
  digitalWrite(pin, HIGH);
  delay(5);
  const int pushPullHigh = digitalRead(pin);
  pinMode(pin, OUTPUT_OPEN_DRAIN);
  digitalWrite(pin, LOW);
  delay(5);
  const int openDrainLow = digitalRead(pin);
  digitalWrite(pin, HIGH);
  delay(5);
  const int openDrainHigh = digitalRead(pin);
  pinMode(pin, INPUT);
  Serial.print("TEST ");
  Serial.print(pin);
  Serial.print(" PP_LOW=");
  Serial.print(pushPullLow);
  Serial.print(" PP_HIGH=");
  Serial.print(pushPullHigh);
  Serial.print(" OD_LOW=");
  Serial.print(openDrainLow);
  Serial.print(" OD_HIGH=");
  Serial.println(openDrainHigh);
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

const char *skipWhitespace(const char *s) {
  while (*s != '\0' && std::isspace(static_cast<unsigned char>(*s))) {
    ++s;
  }
  return s;
}

bool parseChannel(const char *arg, size_t *outIndex) {
  if (arg == nullptr) {
    return false;
  }
  arg = skipWhitespace(arg);
  if (arg[0] < '1' || arg[0] > static_cast<char>('0' + kRelayCount) || arg[1] != '\0') {
    return false;
  }
  *outIndex = static_cast<size_t>(arg[0] - '1');
  return true;
}

bool parseUsablePin(const char *arg, uint8_t *outPin) {
  if (arg == nullptr) {
    return false;
  }
  arg = skipWhitespace(arg);
  if (!std::isdigit(static_cast<unsigned char>(*arg))) {
    return false;
  }
  char *end = nullptr;
  const long val = std::strtol(arg, &end, 10);
  if (end == arg || *end != '\0' || val < 0 || val > 255) {
    return false;
  }
  for (const uint8_t pin : kUsablePins) {
    if (pin == static_cast<uint8_t>(val)) {
      *outPin = pin;
      return true;
    }
  }
  return false;
}

void handleChannelCommand(const char *arg, bool on) {
  size_t idx = 0;
  if (!parseChannel(arg, &idx)) {
    Serial.println("ERR INVALID CHANNEL (expected 1-4)");
    return;
  }
  setRelay(idx, on);
  Serial.print(on ? "OK ON " : "OK OFF ");
  Serial.println(idx + 1);
}

void handleCommand(const char *line, const char *header) {
  if (std::strcmp(line, "INFO") == 0) {
    Serial.println("INFO esp32-usb-relay4 PROTOCOL 1");
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
    uint8_t pin = 0;
    if (!parseUsablePin(line + 5, &pin)) {
      Serial.println("ERR INVALID PIN");
      return;
    }
    if (relayIndexForPin(pin) < kRelayCount) {
      Serial.println("ERR RELAY PIN");
      return;
    }
    printPinTest(pin);
    return;
  }
  if (std::strcmp(line, "DIAG") == 0) {
    printDiag(header);
    return;
  }
  if (std::strcmp(line, "HELP") == 0 || std::strcmp(line, "?") == 0) {
    printHelp();
    return;
  }
  if (std::strncmp(line, "ON ", 3) == 0) {
    handleChannelCommand(line + 3, true);
    return;
  }
  if (std::strncmp(line, "OFF ", 4) == 0) {
    handleChannelCommand(line + 4, false);
    return;
  }

  Serial.println("ERR UNKNOWN COMMAND");
}

void handleFrame(char *frame, size_t length, bool requireTag) {
  const char *header = nullptr;
  char *command = frame;
  if (requireTag || frame[0] == '@') {
    if (!hasRequestHeader(frame, length)) {
      Serial.println("ERR INVALID REQUEST ID");
      return;
    }
    header = frame;
    command += kRequestHeaderLength;
  }

  normalizeCommand(command);
  if (header != nullptr || command[0] != '\0') {
    printReplyPrefix(header);
    handleCommand(command, header);
  }
}

}  // namespace

void setup() {
  for (size_t i = 0; i < kRelayCount; ++i) {
    // Latch the inactive level before enabling the output driver. Claim the
    // pin as a plain input first so Arduino accepts the digitalWrite().
    pinMode(kRelayPins[i], INPUT);
    digitalWrite(kRelayPins[i], logicLevel(false));
    pinMode(kRelayPins[i], kRelayPinMode);
  }

  Serial.onEvent(ARDUINO_HW_CDC_BUS_RESET_EVENT, onUsbReset);
  Serial.begin(115200);
  const unsigned long waitStart = millis();
  while (!Serial && (millis() - waitStart < 1500)) {
    delay(10);
  }
  Serial.println("READY esp32-usb-relay4");
}

void loop() {
  while (true) {
    const bool plugged = Serial.isPlugged();
    if (usbResetPending.exchange(false) || !plugged) {
      discardPendingInput();
    }
    if (!plugged) {
      return;
    }
    if (hasPendingFrame() && millis() - lastCommandByteAt >= kFrameIdleTimeoutMs) {
      discardFrame();
    }
    const int byte = Serial.read();
    if (byte < 0) {
      return;
    }
    const char c = static_cast<char>(byte);
    if (c == kCancelFrame) {
      cmdLen = 0;
      frameState = FrameState::Tagged;
      awaitingLineFeed = false;
      lastCommandByteAt = millis();
      continue;
    }
    if (c == '\n') {
      if (frameState != FrameState::Discard && cmdLen > 0) {
        cmdBuf[cmdLen] = '\0';
        handleFrame(cmdBuf, cmdLen, frameState == FrameState::Tagged);
      }
      cmdLen = 0;
      frameState = FrameState::Normal;
      awaitingLineFeed = false;
      continue;
    }
    if (frameState == FrameState::Discard) {
      continue;
    }
    if (c == '\r' && !awaitingLineFeed) {
      awaitingLineFeed = true;
      lastCommandByteAt = millis();
      continue;
    }
    const bool invalidCharacter = awaitingLineFeed || c == '\0';
    const size_t limit = kCommandMaxLength +
        (cmdLen > 0 && cmdBuf[0] == '@' ? kRequestHeaderLength : 0);
    if (!invalidCharacter && cmdLen < limit) {
      cmdBuf[cmdLen++] = c;
      lastCommandByteAt = millis();
      continue;
    }
    // Reject the whole line, keeping the request ID if the header was valid.
    printReplyPrefix(hasRequestHeader(cmdBuf, cmdLen) ? cmdBuf : nullptr);
    Serial.println(invalidCharacter ? "ERR INVALID CHARACTER" : "ERR COMMAND TOO LONG");
    discardFrame();
  }
}
