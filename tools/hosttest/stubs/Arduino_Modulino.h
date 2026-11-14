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
  // Like the real library: a module that does not answer costs 100 ms, then
  // get() reports 0 and isPressed() keeps its last value.
  int16_t get() {
    host.i2cTransfers++;
    if (host.knobFailing) {
      host.advance(100000);
      return 0;
    }
    host.advance(400);            // an I2C read is not free
    pressed_ = host.knobPressed;
    return host.knobPos;
  }
  bool isPressed() {
    get();
    return pressed_;
  }

 private:
  bool pressed_ = false;
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
