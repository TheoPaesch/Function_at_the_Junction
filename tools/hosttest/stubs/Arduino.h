// Host stand-in for the Arduino core, just enough to compile and run sketch.ino
// on a laptop. Time is fake and only moves when the sketch waits or touches a
// pin, so a 60 s dispense runs in milliseconds.
//
// It also models the hardware we do not have yet:
//   - an HX711 on D4/D5 (optional), shifting out real 24-bit readings;
//   - water that lands on that scale while the valve pin D7 is HIGH.
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string>
#include <utility>
#include <vector>

typedef enum { LOW = 0, HIGH = 1 } PinStatus;
typedef enum { INPUT = 0, OUTPUT = 1, INPUT_PULLUP = 2, INPUT_PULLDOWN = 3 } PinMode;

enum digitalPins {
  D0, D1, D2, D3, D4, D5, D6, D7, D8, D9, D10, D11, D12, D13, D14, D15, D16, D17, D18, D19, D20, D21,
  LED_BUILTIN, LED3_R, LED3_G, LED3_B, LED4_R, LED4_G, LED4_B, HOST_PIN_COUNT
};

// ---- String: the few operations the sketch uses ------------------------------

class String {
 public:
  String() {}
  String(const char *text) : value_(text ? text : "") {}
  String(const String &other) : value_(other.value_) {}
  String &operator=(const String &other) { value_ = other.value_; return *this; }
  String &operator+=(const char *text) { value_ += (text ? text : ""); return *this; }
  String &operator+=(const String &other) { value_ += other.value_; return *this; }
  bool operator==(const char *text) const { return value_ == (text ? text : ""); }
  bool operator==(const String &other) const { return value_ == other.value_; }
  const char *c_str() const { return value_.c_str(); }
  unsigned int length() const { return (unsigned int)value_.size(); }

 private:
  std::string value_;
};

// ---- the simulated world -----------------------------------------------------

struct HostEnv {
  uint64_t nowUs = 5000000;           // fake clock

  PinMode pinModes[HOST_PIN_COUNT];
  PinStatus pinOut[HOST_PIN_COUNT];
  bool pinConfigured[HOST_PIN_COUNT];
  int interruptsOff = 0;
  int valveEdges = 0;                 // how often D7 changed

  // Modulinos
  bool knobPresent = true;
  bool buzzerPresent = true;
  int16_t knobPos = 0;
  bool knobPressed = false;
  bool knobFailing = false;           // unplugged at runtime: reads take 100 ms and return 0
  std::vector<std::pair<int, int> > tones;
  int i2cTransfers = 0;

  // LED matrix
  uint8_t frame[104];
  int draws = 0;
  int grayscaleBits = 0;
  bool matrixBegun = false;

  // HX711 model (hxPresent = false leaves D4 floating high)
  bool hxPresent = false;
  bool hxStuckLow = false;            // wiring fault: DT held low, not an HX711
  double hxCountsPerGram = 415.0;
  long hxOffset = -83211;             // raw counts with nothing on the plate
  double plateG = 0.0;                // what is physically on the real scale
  uint64_t hxReadyAtUs = 0;
  int hxPulse = 0;                    // clock pulses seen in the current read
  long hxLatched = 0;
  PinStatus hxData = HIGH;
  uint64_t sckHighAtUs = 0;
  uint64_t worstSckHighUs = 0;
  int hxReads = 0;

  // real water through the real valve (only used together with the HX711 model)
  double realFlowGps = 40.0;
  double realLagS = 0.25;
  double realTailS = 0.30;
  bool streamOn = false;
  uint64_t streamStartUs = 0;
  uint64_t streamEndUs = 0;
  bool streamEnds = false;
  int tiltLevel = 1;                  // D6 reads HIGH (pull-up) unless tilted

  HostEnv();
  void advance(uint64_t us);
};

extern HostEnv host;

// ---- core functions ----------------------------------------------------------

void pinMode(int pin, PinMode mode);
void digitalWrite(int pin, PinStatus level);
PinStatus digitalRead(int pin);
unsigned long millis();
unsigned long micros();
void delay(unsigned long ms);
void delayMicroseconds(unsigned int us);
void noInterrupts();
void interrupts();

class TwoWire {};
extern TwoWire Wire1;
