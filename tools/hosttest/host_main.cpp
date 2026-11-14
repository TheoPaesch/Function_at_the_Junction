// Runs the real sketch.ino on a laptop against the stub headers in stubs/.
//
// We have no board, and CI can only tell us that the sketch compiles. This
// harness runs the sketch's actual control code (dispense loop, safety stops,
// HX711 driver, Knob, LED matrix) on fake time, so its logic is tested too.
//
//   tools/hosttest/run.sh          builds and runs every scenario
//
// One process is one boot: the scenario name is the first argument.

#include "Arduino.h"
#include "Arduino_LED_Matrix.h"
#include "Arduino_Modulino.h"
#include "Arduino_RouterBridge.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

// ============================================================================
// the simulated world
// ============================================================================

HostEnv host;
BridgeClass Bridge;
MonitorClass Monitor;
ModulinoClass Modulino;
TwoWire Wire1;

HostEnv::HostEnv() {
  for (int i = 0; i < HOST_PIN_COUNT; i++) {
    pinModes[i] = INPUT;
    pinOut[i] = LOW;
    pinConfigured[i] = false;
  }
  for (int i = 0; i < 104; i++) {
    frame[i] = 0;
  }
}

void HostEnv::advance(uint64_t us) {
  uint64_t t0 = nowUs;
  uint64_t t1 = nowUs + us;
  if (hxPresent && streamOn) {                    // real water landing on the real scale
    uint64_t a = t0 > streamStartUs ? t0 : streamStartUs;
    uint64_t b = (streamEnds && streamEndUs < t1) ? streamEndUs : t1;
    if (b > a) {
      plateG += realFlowGps * (double)(b - a) / 1e6;
    }
    if (streamEnds && t1 >= streamEndUs) {
      streamOn = false;
    }
  }
  nowUs = t1;
}

static uint32_t hostNoise = 12345;

static void hxClockEdge(bool rising) {
  if (rising) {
    host.sckHighAtUs = host.nowUs;
    if (host.hxPulse == 0) {
      if (host.nowUs < host.hxReadyAtUs) {
        return;                                   // clocked while not ready: nothing comes out
      }
      hostNoise = hostNoise * 1664525u + 1013904223u;
      long jitter = (long)((hostNoise >> 16) % 81u) - 40;        // about +-0.1 g
      long raw = lround(host.plateG * host.hxCountsPerGram) + host.hxOffset + jitter;
      host.hxLatched = raw & 0xFFFFFF;            // 24-bit two's complement
    }
    host.hxPulse++;
    if (host.hxPulse <= 24) {
      host.hxData = ((host.hxLatched >> (24 - host.hxPulse)) & 1) ? HIGH : LOW;
    } else {
      host.hxData = HIGH;
    }
  } else {
    uint64_t high = host.nowUs - host.sckHighAtUs;
    if (high > host.worstSckHighUs) {
      host.worstSckHighUs = high;
    }
    if (host.hxPulse >= 25) {                     // read complete: next conversion in 100 ms
      host.hxPulse = 0;
      host.hxReadyAtUs = host.nowUs + 100000;
      host.hxReads++;
    }
  }
}

void pinMode(int pin, PinMode mode) {
  host.advance(1);
  host.pinModes[pin] = mode;
  host.pinConfigured[pin] = true;
}

void digitalWrite(int pin, PinStatus level) {
  host.advance(1);
  PinStatus before = host.pinOut[pin];
  host.pinOut[pin] = level;
  if (before == level) {
    return;
  }
  if (pin == D7) {
    host.valveEdges++;
    if (level == HIGH) {
      host.streamOn = true;
      host.streamEnds = false;
      host.streamStartUs = host.nowUs + (uint64_t)(host.realLagS * 1e6);
    } else {
      host.streamEnds = true;
      host.streamEndUs = host.nowUs >= host.streamStartUs
                             ? host.nowUs + (uint64_t)(host.realTailS * 1e6)
                             : host.streamStartUs;
    }
  }
  if (pin == D5 && host.hxPresent) {
    hxClockEdge(level == HIGH);
  }
}

PinStatus digitalRead(int pin) {
  host.advance(1);
  if (pin == D4) {
    if (host.hxStuckLow) {
      return LOW;
    }
    if (!host.hxPresent) {
      return HIGH;                                // pulled up, nothing connected
    }
    if (host.hxPulse > 0) {
      return host.hxData;
    }
    return host.nowUs >= host.hxReadyAtUs ? LOW : HIGH;
  }
  if (pin == D6) {
    return host.tiltLevel ? HIGH : LOW;
  }
  return host.pinOut[pin];
}

unsigned long millis() { return (unsigned long)(host.nowUs / 1000); }
unsigned long micros() { return (unsigned long)host.nowUs; }
void delay(unsigned long ms) { host.advance((uint64_t)ms * 1000); }
void delayMicroseconds(unsigned int us) { host.advance(us); }
void noInterrupts() { host.interruptsOff++; }
void interrupts() { host.interruptsOff--; }

// ============================================================================
// the sketch itself
// ============================================================================

#ifndef SKETCH_FILE
#define SKETCH_FILE "../../1-t_at/sketch/sketch.ino"
#endif
#include SKETCH_FILE

// ============================================================================
// test helpers
// ============================================================================

static int failures = 0;
static int checks = 0;

#define CHECK(cond)                                                     \
  do {                                                                  \
    checks++;                                                           \
    if (!(cond)) {                                                      \
      failures++;                                                       \
      printf("    FAIL line %d: %s\n", __LINE__, #cond);                \
    }                                                                   \
  } while (0)

#define CHECK_NEAR(value, expected, tolerance)                          \
  do {                                                                  \
    checks++;                                                           \
    double v_ = (double)(value), e_ = (double)(expected);               \
    if (!(fabs(v_ - e_) <= (double)(tolerance))) {                      \
      failures++;                                                       \
      printf("    FAIL line %d: %s = %.3f, expected %.3f +- %.3f\n",    \
             __LINE__, #value, v_, e_, (double)(tolerance));            \
    }                                                                   \
  } while (0)

static size_t cursor = 0;            // notes already looked at
static bool autoPing = true;         // Python's heartbeat while water runs
static uint32_t lastPingAt = 0;
static uint64_t worstPassUs = 0;

// One loop() pass. Handlers run between passes, exactly like provide_safe promises.
static void pass() {
  uint64_t t0 = host.nowUs;
  loop();
  uint64_t took = host.nowUs - t0;
  if (took > worstPassUs) {
    worstPassUs = took;
  }
  host.advance(150);                 // the rest of the system gets a turn
  if (autoPing && (uint32_t)millis() - lastPingAt >= 500) {
    lastPingAt = (uint32_t)millis();
    rpc_ping();
  }
}

static void runMs(uint32_t ms) {
  uint64_t end = host.nowUs + (uint64_t)ms * 1000;
  while (host.nowUs < end) {
    pass();
  }
}

static const HostNote *lastStatus() {
  for (size_t i = Bridge.notes.size(); i > 0; i--) {
    if (Bridge.notes[i - 1].name == "mcu_status") {
      return &Bridge.notes[i - 1];
    }
  }
  return NULL;
}

static double stNet() { return lastStatus()->values[0].number; }
static bool stStable() { return lastStatus()->values[1].number != 0; }
static bool stValve() { return lastStatus()->values[2].number != 0; }
static int stMode() { return (int)lastStatus()->values[3].number; }
static double stDispensed() { return lastStatus()->values[4].number; }
static double stFlow() { return lastStatus()->values[5].number; }
static bool stVirtual() { return lastStatus()->values[6].number != 0; }

static int countNotes(const char *name, const char *kind, size_t from) {
  int n = 0;
  for (size_t i = from; i < Bridge.notes.size(); i++) {
    const HostNote &note = Bridge.notes[i];
    if (note.name == name && (kind == NULL || note.values[0].text == kind)) {
      n++;
    }
  }
  return n;
}

// Runs until the next "done" or "error_*" event. Returns its kind ("" on timeout).
static std::string waitEnd(uint32_t timeoutMs, double *value) {
  uint64_t end = host.nowUs + (uint64_t)timeoutMs * 1000;
  for (;;) {
    for (; cursor < Bridge.notes.size(); cursor++) {
      const HostNote &note = Bridge.notes[cursor];
      if (note.name != "mcu_event") {
        continue;
      }
      const std::string &kind = note.values[0].text;
      if (kind == "done" || kind.compare(0, 6, "error_") == 0) {
        *value = note.values[1].number;
        cursor++;
        return kind;
      }
    }
    if (host.nowUs >= end) {
      return "";
    }
    pass();
  }
}

static double lastEventValue(const char *kind) {
  for (size_t i = Bridge.notes.size(); i > 0; i--) {
    const HostNote &note = Bridge.notes[i - 1];
    if (note.name == "mcu_event" && note.values[0].text == kind) {
      return note.values[1].number;
    }
  }
  return -9999.0;
}

static bool ledOn(int pin) { return host.pinOut[pin] == LOW; }      // RGB LED 4 is active-low
static bool rgbIs(bool r, bool g, bool b) {
  return ledOn(LED4_R) == r && ledOn(LED4_G) == g && ledOn(LED4_B) == b;
}
static int px(int x, int y) { return host.frame[y * 13 + x]; }
static int litPixels() {
  int n = 0;
  for (int i = 0; i < 104; i++) {
    if (host.frame[i]) {
      n++;
    }
  }
  return n;
}
static void section(const char *name) { printf("  %s\n", name); }

static void commonBootChecks(const char *expectedInfo) {
  section("boot");
  CHECK(host.pinConfigured[D7] && host.pinModes[D7] == OUTPUT && host.pinOut[D7] == LOW);
  CHECK(host.valveEdges == 0);                                   // the valve never opened
  CHECK(host.pinModes[LED4_R] == OUTPUT && host.pinModes[LED4_G] == OUTPUT && host.pinModes[LED4_B] == OUTPUT);
  CHECK(rgbIs(false, false, false));
  CHECK(Monitor.begun && Bridge.begun && Modulino.begun && host.matrixBegun);
  CHECK(host.grayscaleBits == 3);
  CHECK(Monitor.log.find("provide_safe failed") == std::string::npos);
  static const char *names[] = {"info", "tare", "dispense", "stop", "ping", "sim_add", "sim_set",
                                "show", "beep", "set_param", "calibrate"};
  CHECK(Bridge.provided.size() == 11);
  for (size_t i = 0; i < 11 && i < Bridge.provided.size(); i++) {
    CHECK(Bridge.provided[i] == names[i]);
  }
  CHECK(countNotes("mcu_event", "boot", 0) == 1);
  CHECK(Bridge.notes[0].values.size() == 2 && Bridge.notes[0].values[0].type == 's' &&
        Bridge.notes[0].values[1].type == 'f');

  // Python is not there yet: the boot event repeats every 2 s until info() is called.
  runMs(4500);
  CHECK(countNotes("mcu_event", "boot", 0) == 3);
  CHECK(std::string(rpc_info().c_str()) == expectedInfo);
  runMs(4500);
  CHECK(countNotes("mcu_event", "boot", 0) == 3);

  const HostNote *status = lastStatus();
  CHECK(status != NULL && status->values.size() == 7);
  if (status != NULL && status->values.size() == 7) {
    const char *types = "fbbiffb";                               // float bool bool int float float bool
    for (int i = 0; i < 7; i++) {
      CHECK(status->values[i].type == types[i]);
    }
  }
  size_t mark = Bridge.notes.size();
  runMs(2000);
  CHECK_NEAR(countNotes("mcu_status", NULL, mark), 10, 1);       // one status every 200 ms
  CHECK(stMode() == 0 && !stValve() && stStable());
  CHECK_NEAR(stNet(), 0.0, 1.0);
  CHECK(host.draws > 50 && litPixels() > 10);                    // the idle drop is on the matrix
}

// ============================================================================
// scenario: virtual scale and virtual valve (the hardware we have today)
// ============================================================================

static void scenarioVirtual() {
  setup();
  commonBootChecks("fw=1.0;scale=virtual;knob=1;buzzer=1;tilt=0");
  CHECK(stVirtual());
  CHECK(rpc_calibrate(500.0f) == 0.0f);                          // nothing to calibrate

  section("virtual scale: add, set, tare, stable");
  CHECK(rpc_sim_add(900.0f));
  runMs(400);
  CHECK(!stStable());
  CHECK_NEAR(stNet(), 900.0, 1.0);
  runMs(1200);
  CHECK(stStable());
  CHECK(rpc_tare());
  runMs(300);
  CHECK_NEAR(stNet(), 0.0, 1.0);
  CHECK(stStable());                                             // a virtual tare does not unsettle it

  section("screens");
  CHECK(!rpc_show(-1) && !rpc_show(8));
  CHECK(rpc_show(3));                                            // 3-2-1 countdown
  runMs(200);
  CHECK(px(5, 1) == 7 && px(6, 1) == 7 && px(7, 1) == 7 && px(5, 2) == 0 && px(7, 2) == 7);   // "3"
  runMs(1000);
  CHECK(px(5, 3) == 7 && px(5, 4) == 7 && px(7, 4) == 0);                                     // "2"
  runMs(1000);
  CHECK(px(6, 1) == 7 && px(5, 1) == 0 && px(5, 5) == 7 && px(7, 5) == 7);                    // "1"
  CHECK(rpc_show(6));
  pass();
  CHECK(rgbIs(true, false, false));                              // red while in error
  CHECK(rpc_show(0));
  pass();
  CHECK(rgbIs(false, false, false));                             // until the next show
  for (int id = 0; id <= 7; id++) {
    CHECK(rpc_show(id));
    runMs(200);
    CHECK(litPixels() >= 8);
  }
  CHECK(rpc_show(0));

  section("dispense 500 ml");
  CHECK(!rpc_dispense(0.0f) && !rpc_dispense(-3.0f) && !rpc_dispense(4000.5f));
  cursor = Bridge.notes.size();
  uint32_t startedAt = (uint32_t)millis();
  CHECK(rpc_dispense(500.0f));
  CHECK(!rpc_dispense(500.0f));                                  // one at a time
  CHECK(!rpc_tare());                                            // not while water is about to run
  pass();
  CHECK(host.pinOut[D7] == HIGH);
  CHECK(rgbIs(false, false, true));                              // blue while the valve is open
  runMs(3000);
  CHECK(stValve() && stMode() == 1);
  CHECK_NEAR(stDispensed(), (3.0 - 0.3) * 30.0, 10.0);
  CHECK_NEAR(stFlow(), 30.0, 3.0);
  CHECK(px(0, 7) != 0 && px(6, 0) != 0);                         // water at the bottom, stream on top
  double value = 0;
  std::string kind = waitEnd(40000, &value);
  CHECK(kind == "done");
  CHECK_NEAR(value, 500.0, 10.0);                                // within +-10 ml
  CHECK_NEAR((uint32_t)millis() - startedAt, 300 + 500000 / 30 + 1500, 1500);
  CHECK(host.pinOut[D7] == LOW && host.valveEdges == 2);
  pass();
  CHECK(rgbIs(false, true, false));                              // green after done ...
  runMs(3200);
  CHECK(rgbIs(false, false, false));                             // ... for 3 s
  CHECK_NEAR(lastEventValue("lead_s"), 0.4, 0.15);
  CHECK(stMode() == 0);
  CHECK_NEAR(stDispensed(), value, 0.5);                         // kept until the next run

  section("accuracy at other amounts and flows");
  const float amounts[] = {250.0f, 1300.0f, 4000.0f, 500.0f, 250.0f};
  const float flows[] = {30.0f, 30.0f, 80.0f, 80.0f, 10.0f};
  for (int i = 0; i < 5; i++) {
    CHECK(rpc_set_param("flow_gps", flows[i]));
    CHECK(rpc_dispense(amounts[i]));
    kind = waitEnd(200000, &value);
    CHECK(kind == "done");
    CHECK_NEAR(value, amounts[i], 10.0);
    runMs(500);
  }
  CHECK(rpc_set_param("flow_gps", 30.0f));
  CHECK(!rpc_set_param("nonsense", 1.0f));
  CHECK(rpc_set_param("lead_s", 9.0f));                          // clamped to 2 s
  CHECK(rpc_set_param("lead_s", 0.4f));

  section("safety: stop");
  CHECK(rpc_stop());                                             // harmless when idle
  runMs(300);
  CHECK(waitEnd(0, &value) == "");
  CHECK(rpc_dispense(1000.0f));
  runMs(3000);
  CHECK(host.pinOut[D7] == HIGH);
  CHECK(rpc_stop());
  pass();                                                        // the very next pass
  CHECK(host.pinOut[D7] == LOW);
  kind = waitEnd(100, &value);
  CHECK(kind == "error_stopped");
  CHECK_NEAR(value, (3.0 - 0.3) * 30.0, 10.0);                   // what went in before the stop
  CHECK(rgbIs(true, false, false));
  CHECK(host.tones.size() > 0 && host.tones.back().first == 196);            // low error tone
  runMs(1000);
  CHECK(stMode() == 0 && !stValve());
  CHECK(rpc_show(0));

  section("safety: link lost (no ping for 2 s)");
  CHECK(rpc_dispense(1000.0f));
  runMs(1500);
  autoPing = false;
  uint32_t lastPing = lastPingAt;
  kind = waitEnd(4000, &value);
  autoPing = true;
  CHECK(kind == "error_link_lost");
  CHECK_NEAR((uint32_t)millis() - lastPing, 2000, 60);
  CHECK(host.pinOut[D7] == LOW);
  runMs(1000);

  section("safety: vessel removed");
  CHECK(rpc_dispense(1000.0f));
  runMs(2000);
  CHECK(rpc_sim_set(0.0f));                                      // the kettle is gone
  kind = waitEnd(400, &value);
  CHECK(kind == "error_vessel_removed");
  CHECK(host.pinOut[D7] == LOW);
  runMs(1000);
  CHECK(!rpc_dispense(500.0f));                                  // refuses to pour onto an empty plate
  CHECK(rpc_sim_set(900.0f));
  runMs(1500);
  CHECK(rpc_tare());
  runMs(300);

  section("safety: no flow");
  CHECK(rpc_set_param("flow_gps", 0.0f));
  CHECK(rpc_dispense(500.0f));
  startedAt = (uint32_t)millis();
  kind = waitEnd(6000, &value);
  CHECK(kind == "error_no_flow");
  CHECK_NEAR((uint32_t)millis() - startedAt, 3000, 250);         // after the first 3 s
  CHECK(host.pinOut[D7] == LOW);
  runMs(500);

  section("safety: timeout");
  CHECK(rpc_set_param("flow_gps", 3.0f));                        // a trickle
  CHECK(rpc_dispense(250.0f));
  startedAt = (uint32_t)millis();
  kind = waitEnd(90000, &value);
  CHECK(kind == "error_timeout");
  CHECK_NEAR((uint32_t)millis() - startedAt, 250 * 1000 / 5 + 10000, 300);   // ml / 5 g/s + 10 s
  CHECK(host.pinOut[D7] == LOW);
  CHECK(rpc_set_param("flow_gps", 30.0f));
  runMs(1000);
  CHECK(rpc_show(0));

  section("knob");
  double before = stNet();
  size_t mark = Bridge.notes.size();
  host.knobPos = (int16_t)(host.knobPos + 3);
  runMs(400);
  CHECK_NEAR(stNet() - before, 150.0, 1.0);                      // 50 g per detent
  CHECK(countNotes("mcu_event", "knob_turn", mark) == 1);
  CHECK_NEAR(lastEventValue("knob_turn"), 3.0, 0.01);
  host.knobPos = (int16_t)(host.knobPos - 100);
  runMs(400);
  CHECK(stNet() > -1000.0);                                      // the plate never goes below zero
  CHECK(rpc_sim_set(900.0f));
  CHECK(rpc_tare());
  runMs(1500);
  mark = Bridge.notes.size();
  host.knobPressed = true;
  runMs(300);
  CHECK(countNotes("mcu_event", "knob_press", mark) == 1);       // one event per press
  host.knobPressed = false;
  runMs(300);
  CHECK(countNotes("mcu_event", "knob_press", mark) == 1);
  CHECK(rpc_dispense(1000.0f));                                  // while water runs it is a stop button
  runMs(2000);
  cursor = Bridge.notes.size();
  host.knobPressed = true;
  kind = waitEnd(300, &value);
  host.knobPressed = false;
  CHECK(kind == "error_stopped");
  CHECK(host.pinOut[D7] == LOW);
  runMs(500);
  CHECK(rpc_show(0));

  section("buzzer patterns");
  CHECK(!rpc_beep(0) && !rpc_beep(5));
  host.tones.clear();
  CHECK(rpc_beep(1));
  runMs(300);
  CHECK(host.tones.size() == 1);
  host.tones.clear();
  CHECK(rpc_beep(2));
  runMs(600);
  CHECK(host.tones.size() == 2 && host.tones[0].first < host.tones[1].first);           // two rising tones
  host.tones.clear();
  CHECK(rpc_beep(3));
  runMs(900);
  CHECK(host.tones.size() == 3 && host.tones[0].first < host.tones[1].first &&
        host.tones[1].first < host.tones[2].first);                                     // three rising tones
  host.tones.clear();
  CHECK(rpc_beep(4));
  runMs(900);
  CHECK(host.tones.size() == 1 && host.tones[0].first < 300 && host.tones[0].second >= 500);   // low, long

  section("timing");
  CHECK(worstPassUs < 10000);                                    // every loop pass well under 10 ms
  CHECK(host.interruptsOff == 0);
  CHECK(host.hxReads == 0);                                      // no HX711: its pins are left alone
}

// ============================================================================
// scenario: HX711 load cell and a real valve (the hardware that comes later)
// ============================================================================

static void scenarioHx711() {
  host.hxPresent = true;
  host.hxReadyAtUs = host.nowUs + 420000;                        // first conversion 0.4 s after power-up
  setup();
  commonBootChecks("fw=1.0;scale=hx711;knob=1;buzzer=1;tilt=0");
  CHECK(!stVirtual());
  CHECK(host.pinModes[D5] == OUTPUT && host.pinModes[D4] == INPUT_PULLUP);

  section("HX711 driver");
  CHECK(host.hxReads > 50);                                      // about 10 readings per second
  CHECK(host.worstSckHighUs < 50);                               // SCK high for less than 50 us
  CHECK(host.interruptsOff == 0);
  CHECK(!rpc_sim_add(100.0f) && !rpc_sim_set(5.0f));             // virtual-only calls are refused
  host.knobPos = (int16_t)(host.knobPos + 4);                    // the Knob no longer adds weight
  runMs(1500);
  CHECK_NEAR(stNet(), 0.0, 1.0);
  CHECK_NEAR(lastEventValue("knob_turn"), 4.0, 0.01);

  section("calibration");
  CHECK(rpc_calibrate(0.0f) == 0.0f);
  CHECK(rpc_tare());
  pass();
  runMs(400);
  CHECK(!stStable());                                            // stable stays false until the tare is done
  runMs(2500);
  CHECK(stStable());
  host.plateG = 500.0;                                           // a known weight
  runMs(2500);
  CHECK_NEAR(stNet(), 500.0 * 415.0 / 420.0, 1.5);               // still the default factor
  float factor = rpc_calibrate(500.0f);
  CHECK_NEAR(factor, 415.0, 1.0);                                // counts per gram
  runMs(600);
  CHECK_NEAR(stNet(), 500.0, 1.0);
  CHECK(rpc_set_param("cal", 400.0f));                           // what Python sends back at boot
  runMs(600);
  CHECK_NEAR(stNet(), 500.0 * 415.0 / 400.0, 1.5);
  CHECK(rpc_set_param("cal", factor));
  host.plateG = -20.0;                                           // negative readings: sign extension
  runMs(2500);
  CHECK_NEAR(stNet(), -20.0, 1.0);

  section("dispense by weight through the real valve");
  host.plateG = 870.0;                                           // the empty kettle
  runMs(2500);
  CHECK(stStable());
  CHECK(rpc_tare());
  runMs(2500);
  CHECK_NEAR(stNet(), 0.0, 1.0);
  double value = 0;
  double firstError = 0;
  double lastError = 0;
  for (int run = 0; run < 4; run++) {
    cursor = Bridge.notes.size();
    double plateBefore = host.plateG;
    CHECK(rpc_dispense(500.0f));
    std::string kind = waitEnd(60000, &value);
    CHECK(kind == "done");
    CHECK_NEAR(value, host.plateG - plateBefore, 1.5);           // the report matches the real water
    CHECK_NEAR(value, 500.0, 10.0);
    lastError = value - 500.0;
    if (run == 0) {
      firstError = lastError;
    }
    runMs(800);
  }
  printf("    first run %+.1f g, fourth run %+.1f g, lead %.2f s\n", firstError, lastError,
         lastEventValue("lead_s"));
  CHECK(fabs(lastError) <= 4.0);                                 // the lead time was learned

  section("safety with the real scale");
  cursor = Bridge.notes.size();
  CHECK(rpc_dispense(1000.0f));
  runMs(3000);
  host.plateG = 0.0;                                             // the kettle was lifted off
  std::string kind = waitEnd(1000, &value);
  CHECK(kind == "error_vessel_removed");
  CHECK(host.pinOut[D7] == LOW);
  runMs(1000);
  host.plateG = 870.0;
  runMs(2500);
  CHECK(rpc_tare());
  runMs(2500);
  host.realFlowGps = 0.0;                                        // the tap is closed
  CHECK(rpc_dispense(500.0f));
  kind = waitEnd(6000, &value);
  CHECK(kind == "error_no_flow");
  CHECK(host.pinOut[D7] == LOW);

  section("timing");
  CHECK(worstPassUs < 10000);
  CHECK(host.interruptsOff == 0);
}

// ============================================================================
// scenario: nothing plugged in, and a wiring fault on D4
// ============================================================================

static void scenarioBare() {
  host.knobPresent = false;
  host.buzzerPresent = false;
  host.hxStuckLow = true;                                        // DT shorted low is not an HX711
  setup();
  commonBootChecks("fw=1.0;scale=virtual;knob=0;buzzer=0;tilt=0");
  CHECK(stVirtual());
  section("no Knob, no Buzzer");
  int transfers = host.i2cTransfers;
  CHECK(rpc_beep(3));                                            // accepted, silently ignored
  host.knobPos = 50;
  host.knobPressed = true;
  runMs(2000);
  CHECK(host.i2cTransfers == transfers && host.tones.empty());
  CHECK(countNotes("mcu_event", "knob_turn", 0) == 0 && countNotes("mcu_event", "knob_press", 0) == 0);
  CHECK_NEAR(stNet(), 0.0, 1.0);
  CHECK(rpc_sim_add(900.0f));
  runMs(1500);
  CHECK(rpc_tare());
  runMs(300);
  double value = 0;
  cursor = Bridge.notes.size();
  CHECK(rpc_dispense(250.0f));
  CHECK(waitEnd(30000, &value) == "done");
  CHECK_NEAR(value, 250.0, 10.0);
}

// ============================================================================
// scenario: tilt switch (the sketch is compiled with USE_TILT 1 for this one)
// ============================================================================

static void scenarioTilt() {
  setup();
  commonBootChecks("fw=1.0;scale=virtual;knob=1;buzzer=1;tilt=1");
  section("tilt switch");
  CHECK(host.pinModes[D6] == INPUT_PULLUP);
  CHECK(rpc_sim_add(1500.0f));
  runMs(1500);
  CHECK(rpc_tare());
  runMs(300);
  double value = 0;
  cursor = Bridge.notes.size();
  CHECK(rpc_dispense(1000.0f));
  runMs(2000);
  host.tiltLevel = 0;                                            // a short knock: under 300 ms
  runMs(200);
  host.tiltLevel = 1;
  runMs(1000);
  CHECK(host.pinOut[D7] == HIGH);
  host.tiltLevel = 0;                                            // lifted
  uint32_t at = (uint32_t)millis();
  std::string kind = waitEnd(2000, &value);
  CHECK(kind == "error_tilt");
  CHECK_NEAR((uint32_t)millis() - at, 300, 40);
  CHECK(host.pinOut[D7] == LOW);
}

int main(int argc, char **argv) {
  const char *name = argc > 1 ? argv[1] : "virtual";
  printf("%s\n", name);
  if (strcmp(name, "virtual") == 0) {
    scenarioVirtual();
  } else if (strcmp(name, "hx711") == 0) {
    scenarioHx711();
  } else if (strcmp(name, "bare") == 0) {
    scenarioBare();
  } else if (strcmp(name, "tilt") == 0) {
    scenarioTilt();
  } else {
    printf("unknown scenario\n");
    return 2;
  }
  printf("  %d checks, %d failed\n", checks, failures);
  return failures ? 1 : 0;
}
