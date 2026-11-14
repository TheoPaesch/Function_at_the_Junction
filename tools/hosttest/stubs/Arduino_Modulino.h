// Host stand-in for Arduino_Modulino: a Knob and a Buzzer that may be missing.
#pragma once

#include "Arduino.h"

class ModulinoClass {
 public:
  bool begun = false;
  void begin(TwoWire &wire) {
    (void)wire;
    begun = true;
  }
};

class ModulinoKnob {
 public:
  bool begin() { return host.knobPresent; }
  int16_t get() {
    host.i2cTransfers++;
    host.advance(400);            // an I2C read is not free
    return host.knobPos;
  }
  bool isPressed() {
    host.i2cTransfers++;
    host.advance(400);
    return host.knobPressed;
  }
};

class ModulinoBuzzer {
 public:
  bool begin() { return host.buzzerPresent; }
  void tone(int hz, int ms) {
    host.i2cTransfers++;
    host.advance(800);
    host.tones.push_back(std::make_pair(hz, ms));
  }
};

extern ModulinoClass Modulino;
