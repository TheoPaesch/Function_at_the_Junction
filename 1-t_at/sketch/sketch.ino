/*
  Cooking water assistant: microcontroller side (Arduino UNO Q, STM32U585).

  The MCU owns the valve and the scale. Python (the Linux side) tells it how
  much water to dispense; this sketch opens the valve, watches the weight and
  closes the valve on time, or at once when something looks wrong.

  We have no load cell and no valve yet, so by default the sketch runs a
  VIRTUAL scale and a VIRTUAL valve behind the same interface:
    - an HX711 wired to D4 (DT) and D5 (SCK) is detected at boot and used;
    - the valve output on D7 (HIGH = open) is always driven, so a real valve
      driver works with no code change.

  Sections:
    1. Config and pins          6. LED matrix screens
    2. HX711 (real scale)       7. RGB LED
    3. Virtual scale            8. Modulinos (Knob, Buzzer)
    4. Valve and dispense loop  9. Bridge handlers
    5. Safety                  10. Status, setup, loop

  Rules followed everywhere: logs go to Monitor, never to the serial port;
  all timing uses millis(); Bridge handlers (rpc_*) only set variables or
  flags, the work happens in loop(); no maths-library calls and no 64-bit
  division, so the sketch needs nothing beyond the core and its libraries.
*/

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>
#include <Arduino_Modulino.h>

// ============================================================================
// 1. Config and pins
// ============================================================================

#define RGB_ACTIVE_LOW 1      // LED4 lights when its pin is LOW. One switch for the polarity.
#define USE_TILT 0            // 1: KY-017 tilt switch on D6 stops the water when the pot is lifted

const char *const FW_VERSION = "1.0";

const int PIN_HX_DT = D4;     // HX711 data (future load cell), 3.3 V
const int PIN_HX_SCK = D5;    // HX711 clock
const int PIN_TILT = D6;      // optional tilt switch to GND
const int PIN_VALVE = D7;     // valve driver, HIGH = open

// Timing and thresholds. python/board.py (SimLink) uses the same numbers;
// selftest.py checks that the two stay in step.
const uint32_t STATUS_PERIOD_MS = 200;
const uint32_t KNOB_PERIOD_MS = 30;
const uint32_t KNOB_SLOW_MS = 20;         // a read that takes this long has failed
const uint32_t KNOB_BACKOFF_MS = 3000;    // then leave the I2C bus alone for a while
const int KNOB_MAX_JUMP = 30;             // more detents than a hand can turn between two polls
const uint32_t MATRIX_PERIOD_MS = 80;
const uint32_t BOOT_REPEAT_MS = 2000;
const uint32_t HX_DETECT_MS = 600;

const int STABLE_SAMPLES = 10;            // one second at 10 samples per second
const uint32_t STABLE_SAMPLE_MS = 100;
const float STABLE_RANGE_G = 2.0f;
const int TARE_SAMPLES = 10;
const float HX_EMA = 0.5f;
const float CAL_DEFAULT = 420.0f;         // counts per gram until the scale is calibrated

const uint32_t SIM_SAMPLE_PERIOD_MS = 100;
const uint32_t SIM_START_LAG_MS = 300;    // valve open -> first water on the scale
const uint32_t SIM_TAIL_MS = 400;         // water still in flight after the valve closes
const float SIM_NOISE_G = 0.3f;
const float KNOB_STEP_G = 50.0f;
const float FLOW_GPS_DEFAULT = 30.0f;
const float FLOW_GPS_MAX = 200.0f;

const uint32_t FLOW_WINDOW_MS = 200;
const float FLOW_EMA = 0.4f;
const uint32_t EXTRAPOLATE_MAX_MS = 150;
const uint32_t SETTLE_MS = 1500;
const float LEAD_DEFAULT_S = 0.4f;
const float LEAD_MAX_S = 2.0f;
const float LEAD_MAX_STEP_S = 0.3f;
const float MAX_DISPENSE_ML = 4000.0f;

const float VESSEL_DROP_G = 150.0f;
const uint32_t NOFLOW_GRACE_MS = 3000;
const uint32_t NOFLOW_WINDOW_MS = 4000;
const int NOFLOW_SLOTS = 21;              // NOFLOW_WINDOW_MS / FLOW_WINDOW_MS + 1
const float NOFLOW_MIN_G = 5.0f;
const uint32_t PING_TIMEOUT_MS = 2000;
const float TIMEOUT_MIN_FLOW_GPS = 5.0f;
const uint32_t TIMEOUT_EXTRA_MS = 10000;
const uint32_t TIMEOUT_CAP_MS = 180000;
const uint32_t TILT_HOLD_MS = 300;
const uint32_t GREEN_MS = 3000;

const int MODE_IDLE = 0;
const int MODE_DISPENSING = 1;
const int MODE_SETTLING = 2;

const int SCREEN_IDLE = 0;
const int SCREEN_VESSEL = 1;
const int SCREEN_INGREDIENT = 2;
const int SCREEN_READY = 3;
const int SCREEN_DISPENSING = 4;
const int SCREEN_DONE = 5;
const int SCREEN_ERROR = 6;
const int SCREEN_THINKING = 7;

const int MATRIX_W = 13;
const int MATRIX_H = 8;

// ---- devices ---------------------------------------------------------------

Arduino_LED_Matrix matrix;
ModulinoKnob knob;
ModulinoBuzzer buzzer;
uint8_t frame[104];           // 13 columns x 8 rows, row-major: index = y * 13 + x

bool hasKnob = false;
bool hasBuzzer = false;
bool realScale = false;       // true when an HX711 answered at boot

// ---- state shared by the sections -----------------------------------------

// scale (both kinds)
float netG = 0.0f;            // latest reading, tared
uint32_t sampleMs = 0;        // when that reading was taken
float stableRing[STABLE_SAMPLES];
int stableCount = 0;
int stableIdx = 0;
uint32_t stableLastMs = 0;
bool taring = false;

// HX711
float hxFilt = 0.0f;          // filtered raw counts
float hxZeroRaw = 0.0f;       // raw counts at boot
float hxTareRaw = 0.0f;       // raw counts at the last tare
float calCountsPerG = 420.0f;
long hxMed[3];
int hxMedCount = 0;
int hxMedIdx = 0;
bool hxSeen = false;
int tareLeft = 0;
long tareBase = 0;
long tareSum = 0;

// virtual scale and virtual water stream
float simGross = 0.0f;        // grams on the virtual plate
float simNoise = 0.0f;
float simTareG = 0.0f;
float simFlowGps = 30.0f;
uint32_t simLastMs = 0;
uint32_t simSampleMs = 0;
bool streamOn = false;        // water is (or will be) arriving on the scale
bool streamEnds = false;
uint32_t streamStartMs = 0;
uint32_t streamEndMs = 0;
uint32_t rngState = 0x2545F491UL;

// valve and dispense loop
bool valveOpen = false;
int mode = 0;
float targetMl = 0.0f;
float startG = 0.0f;
float goalG = 0.0f;
float flowEst = 0.0f;         // g/s, moving average
float flowAtClose = 0.0f;
float dispensedG = 0.0f;
float leadS = 0.4f;           // close this many seconds of flow before the goal
uint32_t dispenseStartMs = 0;
uint32_t settleStartMs = 0;
uint32_t dispenseTimeoutMs = 0;
uint32_t lastPingMs = 0;
uint32_t flowWinMs = 0;
float flowWinNet = 0.0f;
float noflowRing[NOFLOW_SLOTS];
int noflowIdx = 0;

// requests set by the Bridge handlers, carried out by loop()
bool reqStop = false;
bool reqTare = false;
bool reqDispense = false;
float reqDispenseMl = 0.0f;

// tilt switch
bool tiltNormal = true;
bool tiltOff = false;
uint32_t tiltSinceMs = 0;

// outputs
int screen = 0;
uint32_t countdownStartMs = 0;
bool errorActive = false;
uint32_t greenUntilMs = 0;
bool greenOn = false;
int rgbShown = -1;
uint32_t matrixLastMs = 0;

// knob
int16_t knobLastPos = 0;      // last position we acted on
int16_t knobPrevRaw = 0;      // previous reading: a position counts once it was read twice
bool knobFault = false;       // the last read failed (module unplugged?)
uint32_t knobRetryMs = 0;
int knobTurnPending = 0;
uint32_t knobLastMs = 0;
uint32_t knobTurnSentMs = 0;
bool knobLastRaw = false;
bool knobDown = false;

// buzzer
int beepPattern = 0;
int beepIdx = 0;
bool beepActive = false;
uint32_t beepNextMs = 0;

// link to Python
bool pythonSynced = false;    // Python has asked for info(): stop repeating the boot event
uint32_t bootSentMs = 0;
uint32_t statusLastMs = 0;

// ---- small helpers ---------------------------------------------------------

// true when time a is later than time b (safe across the millis() wrap)
bool timeAfter(uint32_t a, uint32_t b) {
  return (int32_t)(a - b) > 0;
}

float clampF(float v, float lo, float hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

void sendEvent(const char *kind, float value) {
  String k(kind);
  Bridge.notify("mcu_event", k, value);
}

// ============================================================================
// 2. HX711 (real scale): bit-banged, non-blocking, no library
// ============================================================================

bool hxReady() {
  return digitalRead(PIN_HX_DT) == LOW;
}

// 24 data bits plus 1 extra clock pulse (channel A, gain 128). SCK must stay
// HIGH for less than 50 us or the chip powers down, hence no interrupts here.
long hxReadRaw() {
  uint32_t value = 0;
  noInterrupts();
  for (int i = 0; i < 24; i++) {
    digitalWrite(PIN_HX_SCK, HIGH);
    delayMicroseconds(1);
    value = (value << 1) | (digitalRead(PIN_HX_DT) == HIGH ? 1UL : 0UL);
    digitalWrite(PIN_HX_SCK, LOW);
    delayMicroseconds(1);
  }
  digitalWrite(PIN_HX_SCK, HIGH);
  delayMicroseconds(1);
  digitalWrite(PIN_HX_SCK, LOW);
  interrupts();
  if (value & 0x800000UL) {
    value |= 0xFF000000UL;      // sign-extend 24 bits
  }
  return (long)(int32_t)value;
}

long median3(long a, long b, long c) {
  if (a > b) { long t = a; a = b; b = t; }
  if (b > c) { long t = b; b = c; c = t; }
  if (a > b) { long t = a; a = b; b = t; }
  return b;
}

// SCK low, DT pulled up. An HX711 pulls DT low when a conversion is ready
// (within about 0.5 s of power-up); a missing chip leaves it high.
bool hxDetect() {
  pinMode(PIN_HX_SCK, OUTPUT);
  digitalWrite(PIN_HX_SCK, LOW);
  pinMode(PIN_HX_DT, INPUT_PULLUP);
  uint32_t t0 = millis();
  bool found = false;
  while (millis() - t0 < HX_DETECT_MS) {
    if (hxReady()) {
      found = true;
      break;
    }
    delay(1);
  }
  if (!found) {
    return false;
  }
  hxReadRaw();
  delayMicroseconds(10);
  // After a read the chip releases DT (high) until the next conversion.
  // A line that is still low is a wiring fault, not an HX711.
  return digitalRead(PIN_HX_DT) == HIGH;
}

// ============================================================================
// 3. Scale: one interface, real or virtual
// ============================================================================

float scaleGrossG() {
  if (realScale) {
    return (hxFilt - hxZeroRaw) / calCountsPerG;
  }
  return simGross + simNoise;
}

float scaleNetNowG() {
  if (realScale) {
    return (hxFilt - hxTareRaw) / calCountsPerG;
  }
  return simGross + simNoise - simTareG;
}

// max - min under 2 g over the last second; never while a tare is running
bool scaleStable() {
  if (taring || stableCount < STABLE_SAMPLES) {
    return false;
  }
  float lo = stableRing[0];
  float hi = stableRing[0];
  for (int i = 1; i < STABLE_SAMPLES; i++) {
    if (stableRing[i] < lo) lo = stableRing[i];
    if (stableRing[i] > hi) hi = stableRing[i];
  }
  return (hi - lo) < STABLE_RANGE_G;
}

// Flow estimate: moving average of weight gain per second over windows of
// about 200 ms, measured between sample timestamps.
void flowOnSample(float net, uint32_t now) {
  if (mode != MODE_DISPENSING || now - flowWinMs < FLOW_WINDOW_MS) {
    return;
  }
  float inst = (net - flowWinNet) * 1000.0f / (float)(now - flowWinMs);
  if (inst < 0.0f) {
    inst = 0.0f;
  }
  if (flowEst <= 0.01f) {
    if (inst > 2.0f) {
      flowEst = inst;
    }
  } else {
    flowEst = (1.0f - FLOW_EMA) * flowEst + FLOW_EMA * inst;
  }
  flowWinMs = now;
  flowWinNet = net;
  noflowRing[noflowIdx] = net;
  noflowIdx = (noflowIdx + 1) % NOFLOW_SLOTS;  // now noflowRing[noflowIdx] is the oldest: 4 s ago
}

// Every new reading, real or virtual, goes through here.
void scaleOnSample(uint32_t now) {
  netG = scaleNetNowG();
  sampleMs = now;
  if (stableCount == 0 || now - stableLastMs >= STABLE_SAMPLE_MS) {
    stableLastMs = now;
    stableRing[stableIdx] = scaleGrossG();
    stableIdx = (stableIdx + 1) % STABLE_SAMPLES;
    if (stableCount < STABLE_SAMPLES) {
      stableCount++;
    }
  }
  flowOnSample(netG, now);
}

void hxUpdate(uint32_t now) {
  if (!hxReady()) {
    return;                                    // read only when DT is low
  }
  long raw = hxReadRaw();
  hxMed[hxMedIdx] = raw;
  hxMedIdx = (hxMedIdx + 1) % 3;
  if (hxMedCount < 3) {
    hxMedCount++;
  }
  long m = raw;
  if (hxMedCount == 3) {
    m = median3(hxMed[0], hxMed[1], hxMed[2]);
  }
  if (!hxSeen) {
    hxSeen = true;
    hxFilt = (float)m;
    hxZeroRaw = hxFilt;
    hxTareRaw = hxFilt;                        // the scale starts at 0 g
  } else {
    hxFilt += HX_EMA * ((float)m - hxFilt);
  }
  if (taring) {                                // average about 10 samples, without blocking
    tareSum += (m - tareBase);
    tareLeft--;
    if (tareLeft <= 0) {
      hxTareRaw = (float)tareBase + (float)tareSum / (float)TARE_SAMPLES;
      taring = false;
    }
  }
  scaleOnSample(now);
}

uint32_t rngNext() {
  rngState ^= rngState << 13;
  rngState ^= rngState >> 17;
  rngState ^= rngState << 5;
  return rngState;
}

// Virtual water: while the valve is open the plate gains simFlowGps after a
// start lag; after closing, the water in flight keeps arriving for a moment.
void simUpdate(uint32_t now) {
  uint32_t t0 = simLastMs;
  simLastMs = now;
  if (streamOn) {
    uint32_t a = timeAfter(streamStartMs, t0) ? streamStartMs : t0;
    uint32_t b = now;
    if (streamEnds && timeAfter(now, streamEndMs)) {
      b = streamEndMs;
    }
    if (timeAfter(b, a)) {
      simGross += simFlowGps * (float)(b - a) / 1000.0f;
    }
    if (streamEnds && !timeAfter(streamEndMs, now)) {
      streamOn = false;
    }
  }
  if (now - simSampleMs >= SIM_SAMPLE_PERIOD_MS) {
    simSampleMs = now;
    simNoise = ((float)(rngNext() % 601UL) - 300.0f) / 1000.0f;   // +-0.3 g
    scaleOnSample(now);
  }
}

void scaleUpdate(uint32_t now) {
  if (realScale) {
    hxUpdate(now);
  } else {
    simUpdate(now);
  }
}

void scaleTare() {
  if (realScale) {
    taring = true;
    tareLeft = TARE_SAMPLES;
    tareSum = 0;
    tareBase = (long)hxFilt;
  } else {
    simTareG = simGross;                       // the offset is the current gross weight
    netG = scaleNetNowG();
  }
}

// ============================================================================
// 4. Valve and dispense loop
// ============================================================================

void valveSet(bool open, uint32_t now) {
  if (open == valveOpen) {
    return;
  }
  valveOpen = open;
  digitalWrite(PIN_VALVE, open ? HIGH : LOW);
  if (open) {
    streamOn = true;
    streamEnds = false;
    streamStartMs = now + SIM_START_LAG_MS;
  } else {
    streamEnds = true;
    if (timeAfter(streamStartMs, now)) {
      streamEndMs = streamStartMs;             // closed before any water came
    } else {
      streamEndMs = now + SIM_TAIL_MS;         // water in flight
    }
  }
}

void beginDispense(float ml, uint32_t now) {
  targetMl = ml;
  startG = netG;
  goalG = startG + ml;
  dispenseStartMs = now;
  float limit = ml / TIMEOUT_MIN_FLOW_GPS * 1000.0f + (float)TIMEOUT_EXTRA_MS;   // ml / 5 g/s + 10 s
  if (limit > (float)TIMEOUT_CAP_MS) {
    limit = (float)TIMEOUT_CAP_MS;
  }
  dispenseTimeoutMs = (uint32_t)limit;
  lastPingMs = now;
  flowEst = 0.0f;
  flowAtClose = 0.0f;
  flowWinMs = now;
  flowWinNet = startG;
  for (int i = 0; i < NOFLOW_SLOTS; i++) {
    noflowRing[i] = startG;
  }
  noflowIdx = 0;
  dispensedG = 0.0f;
  reqStop = false;
  errorActive = false;
  greenOn = false;
  screen = SCREEN_DISPENSING;
  mode = MODE_DISPENSING;
  valveSet(true, now);
}

// Normal end: the valve has been closed for 1.5 s, measure what really went in.
void finishDispense(uint32_t now) {
  float actual = netG - startG;
  dispensedG = actual;
  mode = MODE_IDLE;
  greenOn = true;
  greenUntilMs = now + GREEN_MS;
  sendEvent("done", actual);
  // Learn the lead time: an overshoot means "close earlier next time".
  float flow = flowAtClose;
  if (flow < 1.0f) {
    flow = 1.0f;
  }
  float change = clampF(0.5f * (actual - targetMl) / flow, -LEAD_MAX_STEP_S, LEAD_MAX_STEP_S);
  leadS = clampF(leadS + change, 0.0f, LEAD_MAX_S);
  sendEvent("lead_s", leadS);
}

// ============================================================================
// 5. Safety: close the valve at once and tell Python why
// ============================================================================

void beepStart(int pattern, uint32_t now) {
  beepPattern = pattern;
  beepIdx = 0;
  beepActive = true;
  beepNextMs = now;
}

void safetyStop(const char *code, uint32_t now) {
  valveSet(false, now);
  mode = MODE_IDLE;
  float gained = netG - startG;
  dispensedG = gained > 0.0f ? gained : 0.0f;
  errorActive = true;
  greenOn = false;
  screen = SCREEN_ERROR;
  beepStart(4, now);
  String kind("error_");
  kind += code;
  Bridge.notify("mcu_event", kind, dispensedG);
}

bool tiltTripped(uint32_t now) {
#if USE_TILT
  bool off = (digitalRead(PIN_TILT) == HIGH) != tiltNormal;
  if (!off) {
    tiltOff = false;
    return false;
  }
  if (!tiltOff) {
    tiltOff = true;
    tiltSinceMs = now;
  }
  return now - tiltSinceMs > TILT_HOLD_MS;
#else
  (void)now;
  return false;
#endif
}

void dispenseUpdate(uint32_t now) {
  if (mode == MODE_DISPENSING) {
    uint32_t openFor = now - dispenseStartMs;
    float gainedInWindow = netG - noflowRing[noflowIdx];     // the oldest slot: 4 s ago
    if (reqStop) {
      safetyStop("stopped", now);
    } else if (openFor > dispenseTimeoutMs) {
      safetyStop("timeout", now);
    } else if (now - lastPingMs > PING_TIMEOUT_MS) {
      safetyStop("link_lost", now);
    } else if (netG < startG - VESSEL_DROP_G) {
      safetyStop("vessel_removed", now);
    } else if (openFor >= NOFLOW_GRACE_MS && gainedInWindow < NOFLOW_MIN_G) {
      safetyStop("no_flow", now);
    } else if (tiltTripped(now)) {
      safetyStop("tilt", now);
    } else {
      // Between two samples, assume the water kept coming at the measured rate.
      float estimate = netG;
      if (flowEst > 0.0f) {
        uint32_t age = now - sampleMs;
        if (age > EXTRAPOLATE_MAX_MS) {
          age = EXTRAPOLATE_MAX_MS;
        }
        estimate += flowEst * (float)age / 1000.0f;
      }
      if (estimate + flowEst * leadS >= goalG) {
        valveSet(false, now);
        flowAtClose = flowEst;
        mode = MODE_SETTLING;
        settleStartMs = now;
      }
    }
  } else if (mode == MODE_SETTLING) {
    if (reqStop) {
      safetyStop("stopped", now);
    } else if (netG < startG - VESSEL_DROP_G) {
      safetyStop("vessel_removed", now);
    } else if (now - settleStartMs >= SETTLE_MS) {
      finishDispense(now);
    }
  }
  reqStop = false;
}

// ============================================================================
// 6. LED matrix screens (13 x 8, brightness 0..7, no scrolling text)
// ============================================================================

const int ICON_DROP = 0;
const int ICON_POT = 1;
const int ICON_POT_OPEN = 2;
const int ICON_CHECK = 3;
const int ICON_CROSS = 4;

const char ICONS[5][8][14] = {
  { "......#......",
    "......#......",
    ".....###.....",
    "....#####....",
    "...#######...",
    "...#######...",
    "...#######...",
    "....#####...." },
  { ".............",
    ".............",
    "##.........##",
    ".###########.",
    ".#.........#.",
    ".#.........#.",
    ".#.........#.",
    "..#########.." },
  { ".............",
    ".............",
    ".............",
    "##.........##",
    ".#.........#.",
    ".#.........#.",
    ".#.........#.",
    "..#########.." },
  { ".............",
    "...........#.",
    "..........##.",
    ".........##..",
    ".##.....##...",
    "..##...##....",
    "...##.##.....",
    "....###......" },
  { "..##.....##..",
    "...##...##...",
    "....##.##....",
    ".....###.....",
    ".....###.....",
    "....##.##....",
    "...##...##...",
    "..##.....##.." }
};

// 3 x 5 digits for the 3-2-1 countdown (index 0 is unused)
const char DIGITS[4][5][4] = {
  { "...", "...", "...", "...", "..." },
  { ".#.", "##.", ".#.", ".#.", "###" },
  { "###", "..#", "###", "#..", "###" },
  { "###", "..#", "###", "..#", "###" }
};

const uint8_t RING_X[8] = { 6, 9, 10, 9, 6, 3, 2, 3 };
const uint8_t RING_Y[8] = { 0, 1, 3, 6, 7, 6, 4, 1 };
const uint8_t RING_TAIL[8] = { 7, 4, 2, 1, 1, 1, 1, 1 };
const uint8_t WAVE[4] = { 6, 4, 2, 4 };

void frameClear() {
  for (int i = 0; i < 104; i++) {
    frame[i] = 0;
  }
}

void frameSet(int x, int y, uint8_t level) {
  if (x >= 0 && x < MATRIX_W && y >= 0 && y < MATRIX_H) {
    frame[y * MATRIX_W + x] = level;
  }
}

void drawIcon(int icon, uint8_t level) {
  for (int y = 0; y < MATRIX_H; y++) {
    for (int x = 0; x < MATRIX_W; x++) {
      frame[y * MATRIX_W + x] = (ICONS[icon][y][x] == '#') ? level : 0;
    }
  }
}

// idle: a water drop with a gentle pulse
void drawIdle(uint32_t now) {
  uint32_t phase = now % 2400UL;
  uint32_t ramp = phase < 1200UL ? phase : 2400UL - phase;      // 0..1200..0
  drawIcon(ICON_DROP, (uint8_t)(3UL + (ramp * 4UL) / 1200UL));
}

// vessel: a blinking pot outline
void drawVessel(uint32_t now) {
  drawIcon(ICON_POT, (now % 900UL) < 600UL ? 7 : 2);
}

// ingredient: a pot with dots falling into it
void drawIngredient(uint32_t now) {
  drawIcon(ICON_POT_OPEN, 5);
  for (int x = 3; x <= 9; x++) {
    frameSet(x, 6, 2);                                           // what is already inside
  }
  uint32_t step = now / 130UL;
  for (int i = 0; i < 3; i++) {
    int y = (int)((step + (uint32_t)i * 2UL) % 7UL);
    if (y <= 5) {
      frameSet(4 + i * 2, y, 7);
    }
  }
}

// ready: 3-2-1, one digit per second, with a bar that runs out every second
void drawReady(uint32_t now) {
  frameClear();
  uint32_t elapsed = now - countdownStartMs;
  int digit = 3 - (int)(elapsed / 1000UL);
  if (digit < 1) {
    digit = 1;
  }
  for (int y = 0; y < 5; y++) {
    for (int x = 0; x < 3; x++) {
      if (DIGITS[digit][y][x] == '#') {
        frameSet(5 + x, 1 + y, 7);
      }
    }
  }
  int bar = MATRIX_W;
  if (elapsed < 3000UL) {
    bar = MATRIX_W - (int)(((elapsed % 1000UL) * (uint32_t)MATRIX_W) / 1000UL);
  } else {
    bar = 0;
  }
  for (int x = 0; x < bar; x++) {
    frameSet(x, 7, 3);
  }
}

// dispensing: the water level rises with the progress; a small wave on top
void drawDispensing(uint32_t now) {
  frameClear();
  float done = (mode == MODE_IDLE) ? dispensedG : (netG - startG);
  float progress = targetMl > 0.0f ? clampF(done / targetMl, 0.0f, 1.0f) : 0.0f;
  int full = (int)(progress * (float)MATRIX_H);                  // full rows, 0..8
  int surface = MATRIX_H - 1 - full;                             // row of the water surface
  if (surface < 0) {
    surface = 0;
  }
  uint32_t shift = now / 160UL;
  for (int y = 0; y < MATRIX_H; y++) {
    for (int x = 0; x < MATRIX_W; x++) {
      if (y > surface) {
        frameSet(x, y, 3);
      } else if (y == surface) {
        frameSet(x, y, WAVE[((uint32_t)x + shift) % 4UL]);
      }
    }
  }
  if (valveOpen) {                                               // the stream from the tap
    uint32_t flicker = now / 90UL;
    for (int y = 0; y < surface; y++) {
      frameSet(6, y, ((flicker + (uint32_t)y) % 3UL) == 0UL ? 3 : 6);
    }
  }
}

// error: a blinking X
void drawError(uint32_t now) {
  drawIcon(ICON_CROSS, (now % 1000UL) < 650UL ? 7 : 3);
}

// thinking: dots chasing each other round a ring
void drawThinking(uint32_t now) {
  frameClear();
  int head = (int)((now / 110UL) % 8UL);
  for (int i = 0; i < 8; i++) {
    int behind = (head - i + 8) % 8;
    frameSet(RING_X[i], RING_Y[i], RING_TAIL[behind]);
  }
}

void matrixUpdate(uint32_t now) {
  if (now - matrixLastMs < MATRIX_PERIOD_MS) {
    return;
  }
  matrixLastMs = now;
  int shown = valveOpen ? SCREEN_DISPENSING : screen;            // water always wins while it runs
  if (shown == SCREEN_IDLE) {
    drawIdle(now);
  } else if (shown == SCREEN_VESSEL) {
    drawVessel(now);
  } else if (shown == SCREEN_INGREDIENT) {
    drawIngredient(now);
  } else if (shown == SCREEN_READY) {
    drawReady(now);
  } else if (shown == SCREEN_DISPENSING) {
    drawDispensing(now);
  } else if (shown == SCREEN_DONE) {
    drawIcon(ICON_CHECK, 7);
  } else if (shown == SCREEN_ERROR) {
    drawError(now);
  } else {
    drawThinking(now);
  }
  matrix.draw(frame);
}

// ============================================================================
// 7. RGB LED 4: blue = valve open, green = done (3 s), red = error
// ============================================================================

void rgbWrite(bool r, bool g, bool b) {
#if RGB_ACTIVE_LOW
  digitalWrite(LED4_R, r ? LOW : HIGH);
  digitalWrite(LED4_G, g ? LOW : HIGH);
  digitalWrite(LED4_B, b ? LOW : HIGH);
#else
  digitalWrite(LED4_R, r ? HIGH : LOW);
  digitalWrite(LED4_G, g ? HIGH : LOW);
  digitalWrite(LED4_B, b ? HIGH : LOW);
#endif
}

void rgbBegin() {
  pinMode(LED4_R, OUTPUT);
  pinMode(LED4_G, OUTPUT);
  pinMode(LED4_B, OUTPUT);
  rgbWrite(false, false, false);
  rgbShown = 0;
}

void rgbUpdate(uint32_t now) {
  if (greenOn && !timeAfter(greenUntilMs, now)) {
    greenOn = false;
  }
  int want = 0;                    // 0 off, 1 blue, 2 red, 3 green
  if (valveOpen) {
    want = 1;
  } else if (errorActive) {
    want = 2;
  } else if (greenOn) {
    want = 3;
  }
  if (want != rgbShown) {
    rgbShown = want;
    rgbWrite(want == 2, want == 3, want == 1);
  }
}

// ============================================================================
// 8. Modulinos: Knob and Buzzer (either may be missing)
// ============================================================================

// 1 click (step), 2 start (two rising tones), 3 done (three rising tones), 4 error (low, long)
const uint16_t BEEP_HZ[4][3] = { { 1760, 0, 0 }, { 880, 1320, 0 }, { 1047, 1319, 1568 }, { 196, 0, 0 } };
const uint16_t BEEP_MS[4][3] = { { 40, 0, 0 }, { 110, 160, 0 }, { 110, 110, 220 }, { 600, 0, 0 } };

void beepUpdate(uint32_t now) {
  if (!beepActive || timeAfter(beepNextMs, now)) {
    return;
  }
  if (beepPattern < 1 || beepPattern > 4 || beepIdx >= 3 || BEEP_MS[beepPattern - 1][beepIdx] == 0) {
    beepActive = false;
    return;
  }
  int hz = BEEP_HZ[beepPattern - 1][beepIdx];
  int ms = BEEP_MS[beepPattern - 1][beepIdx];
  if (hasBuzzer) {
    buzzer.tone(hz, ms);           // plays on the module, returns at once
  }
  beepNextMs = now + (uint32_t)ms + 30UL;
  beepIdx++;
}

// The Modulino library waits up to 100 ms for a module that does not answer
// and then reports position 0. A read that slow is a failed read: ignore it
// and stay off the bus for a while, and completely while the valve is open,
// so a loose Knob cable can never stall the dispense loop.
bool knobReadFailed(uint32_t startedMs) {
  if (millis() - startedMs <= KNOB_SLOW_MS) {
    return false;
  }
  knobFault = true;
  knobRetryMs = millis() + KNOB_BACKOFF_MS;
  return true;
}

void knobUpdate(uint32_t now) {
  if (!hasKnob || now - knobLastMs < KNOB_PERIOD_MS) {
    return;                        // about every 30 ms: each poll costs an I2C transfer
  }
  if (knobFault && (valveOpen || timeAfter(knobRetryMs, now))) {
    return;
  }
  knobLastMs = now;

  uint32_t started = millis();
  int16_t pos = knob.get();
  if (knobReadFailed(started)) {
    return;
  }
  bool raw = knob.isPressed();
  if (knobReadFailed(started)) {
    return;
  }
  knobFault = false;

  if (pos == knobPrevRaw && pos != knobLastPos) {  // the same position twice in a row
    int diff = (int16_t)(pos - knobLastPos);
    knobLastPos = pos;
    if (diff > KNOB_MAX_JUMP || diff < -KNOB_MAX_JUMP) {
      diff = 0;                    // a glitch or a re-plugged module: resynchronise, do not act
    }
    if (diff != 0) {
      if (!realScale) {            // virtual mode: each detent adds or removes 50 g
        simGross += KNOB_STEP_G * (float)diff;
        if (simGross < 0.0f) {
          simGross = 0.0f;
        }
      }
      knobTurnPending += diff;
    }
  }
  knobPrevRaw = pos;
  if (knobTurnPending != 0 && now - knobTurnSentMs >= 100UL) {
    sendEvent("knob_turn", (float)knobTurnPending);
    knobTurnPending = 0;
    knobTurnSentMs = now;
  }

  if (raw == knobLastRaw && raw != knobDown) {     // two equal polls in a row: debounced
    knobDown = raw;
    if (knobDown) {
      sendEvent("knob_press", 1.0f);
      if (mode == MODE_DISPENSING) {
        reqStop = true;            // while water runs the Knob is a stop button
      }
    }
  }
  knobLastRaw = raw;
}

// ============================================================================
// 9. Bridge handlers. Registered with provide_safe, so they run on the loop
//    thread between loop() passes. They only set variables or flags.
// ============================================================================

String infoText() {
  String s("fw=");
  s += FW_VERSION;
  s += ";scale=";
  s += realScale ? "hx711" : "virtual";
  s += ";knob=";
  s += hasKnob ? "1" : "0";
  s += ";buzzer=";
  s += hasBuzzer ? "1" : "0";
  s += ";tilt=";
  s += USE_TILT ? "1" : "0";
  return s;
}

String rpc_info() {
  pythonSynced = true;
  return infoText();
}

bool rpc_tare() {
  if (mode != MODE_IDLE || reqDispense) {
    return false;
  }
  reqTare = true;
  return true;
}

bool rpc_dispense(float ml) {
  if (mode != MODE_IDLE || reqDispense) {
    return false;                  // already dispensing
  }
  if (!(ml > 0.0f) || ml > MAX_DISPENSE_ML) {
    return false;
  }
  if (netG < -VESSEL_DROP_G) {
    return false;                  // the vessel left the plate after the tare
  }
  reqDispenseMl = ml;
  reqDispense = true;
  return true;
}

bool rpc_stop() {
  reqDispense = false;
  if (mode != MODE_IDLE) {
    reqStop = true;
  }
  return true;
}

bool rpc_ping() {
  lastPingMs = millis();
  return true;
}

bool rpc_sim_add(float g) {
  if (realScale) {
    return false;
  }
  simGross += g;
  if (simGross < 0.0f) {
    simGross = 0.0f;
  }
  return true;
}

bool rpc_sim_set(float g) {
  if (realScale) {
    return false;
  }
  simGross = g > 0.0f ? g : 0.0f;
  return true;
}

bool rpc_show(int id) {
  if (id < SCREEN_IDLE || id > SCREEN_THINKING) {
    return false;
  }
  screen = id;
  errorActive = (id == SCREEN_ERROR);          // red while in error, until the next show
  if (id == SCREEN_READY) {
    countdownStartMs = millis();               // one call starts the 3-2-1 animation
  }
  return true;
}

bool rpc_beep(int pattern) {
  if (pattern < 1 || pattern > 4) {
    return false;
  }
  beepStart(pattern, millis());
  return true;
}

bool rpc_set_param(String name, float value) {
  if (name == "flow_gps") {
    simFlowGps = clampF(value, 0.0f, FLOW_GPS_MAX);
  } else if (name == "lead_s") {
    leadS = clampF(value, 0.0f, LEAD_MAX_S);
  } else if (name == "cal") {
    if (value > 0.01f || value < -0.01f) {
      calCountsPerG = value;
    }
  } else {
    return false;
  }
  return true;
}

// Real scale only: with known_g on the tared plate, returns counts per gram.
float rpc_calibrate(float known_g) {
  if (!realScale || taring || !(known_g > 0.0f)) {
    return 0.0f;
  }
  float factor = (hxFilt - hxTareRaw) / known_g;
  if (factor < 0.01f && factor > -0.01f) {
    return 0.0f;
  }
  calCountsPerG = factor;
  return factor;
}

void logProvide(const char *name, bool ok) {
  if (!ok) {
    Monitor.print("provide_safe failed: ");
    Monitor.println(name);
  }
}

void provideAll() {
  logProvide("info", Bridge.provide_safe("info", rpc_info));
  logProvide("tare", Bridge.provide_safe("tare", rpc_tare));
  logProvide("dispense", Bridge.provide_safe("dispense", rpc_dispense));
  logProvide("stop", Bridge.provide_safe("stop", rpc_stop));
  logProvide("ping", Bridge.provide_safe("ping", rpc_ping));
  logProvide("sim_add", Bridge.provide_safe("sim_add", rpc_sim_add));
  logProvide("sim_set", Bridge.provide_safe("sim_set", rpc_sim_set));
  logProvide("show", Bridge.provide_safe("show", rpc_show));
  logProvide("beep", Bridge.provide_safe("beep", rpc_beep));
  logProvide("set_param", Bridge.provide_safe("set_param", rpc_set_param));
  logProvide("calibrate", Bridge.provide_safe("calibrate", rpc_calibrate));
}

// ============================================================================
// 10. Status, setup, loop
// ============================================================================

void requestsUpdate(uint32_t now) {
  if (reqTare) {
    reqTare = false;
    if (mode == MODE_IDLE) {
      scaleTare();
    }
  }
  if (reqDispense) {
    reqDispense = false;
    if (mode == MODE_IDLE) {
      beginDispense(reqDispenseMl, now);
    }
  }
}

void statusUpdate(uint32_t now) {
  if (!pythonSynced && now - bootSentMs >= BOOT_REPEAT_MS) {
    bootSentMs = now;
    sendEvent("boot", 0.0f);                   // again, until Python has asked for info()
  }
  if (now - statusLastMs < STATUS_PERIOD_MS) {
    return;
  }
  statusLastMs = now;
  if (mode != MODE_IDLE) {
    float gained = netG - startG;
    dispensedG = gained > 0.0f ? gained : 0.0f;
  }
  float stNet = netG;
  bool stStable = scaleStable();
  bool stValve = valveOpen;
  int stMode = mode;
  float stDispensed = dispensedG;
  float stFlow = (mode == MODE_DISPENSING) ? flowEst : 0.0f;
  bool stVirtual = !realScale;
  Bridge.notify("mcu_status", stNet, stStable, stValve, stMode, stDispensed, stFlow, stVirtual);
}

void setup() {
  // The valve is closed and the RGB LED is off before anything else happens.
  pinMode(PIN_VALVE, OUTPUT);
  digitalWrite(PIN_VALVE, LOW);
  rgbBegin();

  Monitor.begin(115200);
  Bridge.begin();
  provideAll();

  matrix.begin();
  matrix.setGrayscaleBits(3);                  // brightness 0..7
  frameClear();

  calCountsPerG = CAL_DEFAULT;
  simFlowGps = FLOW_GPS_DEFAULT;
  leadS = LEAD_DEFAULT_S;
  realScale = hxDetect();                      // up to 600 ms, once

  Modulino.begin(Wire1);                       // the Qwiic connector is Wire1
  hasKnob = knob.begin();
  hasBuzzer = buzzer.begin();
  if (hasKnob) {
    knobLastPos = knob.get();
    knobPrevRaw = knobLastPos;
  }

#if USE_TILT
  pinMode(PIN_TILT, INPUT_PULLUP);
  delay(2);
  tiltNormal = (digitalRead(PIN_TILT) == HIGH);     // whatever it reads at boot is "normal"
#endif

  uint32_t now = millis();
  simLastMs = now;
  simSampleMs = now;
  sampleMs = now;
  stableLastMs = now;
  statusLastMs = now;
  knobLastMs = now;
  matrixLastMs = now;
  bootSentMs = now;
  lastPingMs = now;
  mode = MODE_IDLE;
  screen = SCREEN_IDLE;

  Monitor.println(infoText());
  sendEvent("boot", 0.0f);
}

void loop() {
  uint32_t now = millis();
  requestsUpdate(now);     // what the handlers asked for
  scaleUpdate(now);        // new weight, real or virtual
  dispenseUpdate(now);     // valve control and safety stops
  knobUpdate(now);
  beepUpdate(now);
  rgbUpdate(now);
  matrixUpdate(now);
  statusUpdate(now);
  delay(1);                // let the other threads run; every pass stays far below 10 ms
}
