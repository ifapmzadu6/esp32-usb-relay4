#include <Arduino.h>

#include <array>
#include <cassert>
#include <iostream>
#include <limits>

MockSerial Serial;

namespace {

struct Pin {
  uint8_t mode = INPUT;
  uint8_t level = LOW;
  unsigned modeChanges = 0;
};

std::array<Pin, 22> pins;
unsigned long clockMs = 0;

}  // namespace

int MockSerial::read() {
  if (input.empty()) {
    return -1;
  }
  const int byte = input.front();
  input.pop_front();
  return byte;
}

void MockSerial::onEvent(int32_t event, UsbEventHandler handler) {
  assert(event == ARDUINO_HW_CDC_BUS_RESET_EVENT);
  resetHandler = handler;
}

void MockSerial::write(const uint8_t *bytes, size_t length) {
  output.append(reinterpret_cast<const char *>(bytes), length);
}

void pinMode(uint8_t pin, uint8_t mode) {
  pins.at(pin).mode = mode;
  ++pins.at(pin).modeChanges;
}

void digitalWrite(uint8_t pin, uint8_t level) { pins.at(pin).level = level; }

int digitalRead(uint8_t pin) {
  const Pin &state = pins.at(pin);
  return state.mode == INPUT_PULLDOWN ? LOW : state.level;
}

unsigned long millis() { return clockMs; }
void delay(int milliseconds) { clockMs += milliseconds; }

void setup();
void loop();

namespace {

std::string feed(const std::string &bytes) {
  Serial.output.clear();
  Serial.input.insert(Serial.input.end(), bytes.begin(), bytes.end());
  loop();
  return Serial.output;
}

void assertAllOff() {
  for (const uint8_t pin : {2, 3, 4, 10}) {
    assert(pins.at(pin).mode == OUTPUT_OPEN_DRAIN && pins.at(pin).level == HIGH);
  }
}

void checkFraming() {
  const std::string tag = "@0123456789abcdef0123456789abcdef ";
  assertAllOff();

  assert(feed("ALL ON").empty());
  Serial.plugged = false;
  assert(feed("\nALL ON\n").empty());
  Serial.plugged = true;
  assert(feed("\n").empty());
  assertAllOff();

  assert(feed("ALL ON").empty());
  assert(Serial.resetHandler != nullptr);
  Serial.resetHandler(nullptr, nullptr, ARDUINO_HW_CDC_BUS_RESET_EVENT, nullptr);
  assert(feed("\n").empty());
  assertAllOff();

  clockMs = std::numeric_limits<unsigned long>::max() - 50;
  assert(feed("\x18" "ALL ").empty());
  clockMs += 2000;
  assert(feed("ON\n").empty());
  assertAllOff();

  assert(feed(tag + std::string("ALL ON\0\n", 8)) == tag + "ERR INVALID CHARACTER\n");
  assert(feed(tag + "ALL ON" + std::string(58, ' ') + "\n") ==
         tag + "ERR COMMAND TOO LONG\n");
  assertAllOff();

  assert(feed("ALL ON").empty());
  assert(feed("\x18" + tag + "ON 1\n") == tag + "OK ON 1\n");
  const unsigned relayModeChanges = pins.at(2).modeChanges;
  const std::string diag = feed(tag + "DIAG\n");
  assert(diag.find(tag + "LIVE 2=0 ") == 0);
  assert(diag.find("\n" + tag + "DIAG 0=0 1=0 2=- ") != std::string::npos);
  assert(pins.at(2).modeChanges == relayModeChanges);
  assert(pins.at(2).level == LOW);
  assert(feed(tag + "TEST 2\n") == tag + "ERR RELAY PIN\n");
  assert(pins.at(2).modeChanges == relayModeChanges);
  assert(feed("OFF 1\n") == "OK OFF 1\n");
  assertAllOff();
}

}  // namespace

int main(int argc, char *argv[]) {
  setup();
  if (argc == 2 && std::string(argv[1]) == "--check-framing") {
    checkFraming();
    return 0;
  }

  std::cout << Serial.output << std::flush;
  for (char byte; std::cin.get(byte);) {
    const auto reply = feed(std::string(1, byte));
    if (!reply.empty()) {
      std::cout << reply << std::flush;
    }
  }
}
