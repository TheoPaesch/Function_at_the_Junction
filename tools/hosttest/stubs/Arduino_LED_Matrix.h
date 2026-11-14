// Host stand-in for the UNO Q LED matrix: keeps the last frame so tests can look at it.
#pragma once

#include "Arduino.h"

class Arduino_LED_Matrix {
 public:
  bool begin() {
    host.matrixBegun = true;
    return true;
  }
  void setGrayscaleBits(uint8_t bits) { host.grayscaleBits = bits; }
  void draw(uint8_t *pixels) {
    for (int i = 0; i < 104; i++) {
      host.frame[i] = pixels[i];
    }
    host.draws++;
  }
};
