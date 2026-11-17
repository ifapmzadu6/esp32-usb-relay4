#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <string>

constexpr uint8_t INPUT = 1;
constexpr uint8_t INPUT_PULLDOWN = 9;
constexpr uint8_t OUTPUT = 3;
constexpr uint8_t OUTPUT_OPEN_DRAIN = 19;
constexpr uint8_t LOW = 0;
constexpr uint8_t HIGH = 1;

constexpr int32_t ARDUINO_HW_CDC_BUS_RESET_EVENT = 1;
using esp_event_base_t = const char *;
using UsbEventHandler = void (*)(void *, esp_event_base_t, int32_t, void *);

class MockSerial {
 public:
  std::deque<uint8_t> input;
  std::string output;
  bool plugged = true;
  UsbEventHandler resetHandler = nullptr;

  explicit operator bool() const { return plugged; }
  bool isPlugged() const { return plugged; }
  int available() const { return static_cast<int>(input.size()); }
  int read();
  void begin(unsigned long) {}
  void onEvent(int32_t event, UsbEventHandler handler);
  void write(const uint8_t *bytes, size_t length);
  void print(char value) { output += value; }
  void print(const char *value) { output += value; }

  template <class Number>
  void print(Number value) { output += std::to_string(value); }

  void println() { output += '\n'; }

  template <class Value>
  void println(Value value) {
    print(value);
    println();
  }
};

extern MockSerial Serial;
void pinMode(uint8_t pin, uint8_t mode);
void digitalWrite(uint8_t pin, uint8_t level);
int digitalRead(uint8_t pin);
unsigned long millis();
void delay(int milliseconds);
