// Host stand-in for Arduino_RouterBridge: records what the sketch provides and notifies.
#pragma once

#include "Arduino.h"

#include <cstdio>
#include <string>
#include <vector>

struct HostValue {
  char type;            // 'f' float, 'b' bool, 'i' int, 's' string
  double number;
  std::string text;
};

struct HostNote {
  std::string name;
  std::vector<HostValue> values;

  void add(float v) { push('f', (double)v, ""); }
  void add(double v) { push('f', v, ""); }
  void add(bool v) { push('b', v ? 1.0 : 0.0, ""); }
  void add(int v) { push('i', (double)v, ""); }
  void add(const String &v) { push('s', 0.0, v.c_str()); }
  void add(const char *v) { push('s', 0.0, v ? v : ""); }

 private:
  void push(char type, double number, const std::string &text) {
    HostValue value;
    value.type = type;
    value.number = number;
    value.text = text;
    values.push_back(value);
  }
};

class BridgeClass {
 public:
  bool begun = false;
  std::vector<std::string> provided;
  std::vector<HostNote> notes;

  bool begin() {
    begun = true;
    return true;
  }

  // The real one runs the handler on the loop thread between loop() passes.
  // The host test calls the rpc_* functions directly at exactly those moments.
  template <typename F>
  bool provide_safe(const char *name, F handler) {
    (void)handler;
    provided.push_back(name);
    return true;
  }

  template <typename... Args>
  void notify(const char *name, Args... args) {
    HostNote note;
    note.name = name;
    int unused[] = {0, (note.add(args), 0)...};
    (void)unused;
    notes.push_back(note);
  }
};

class MonitorClass {
 public:
  bool begun = false;
  std::string log;

  void begin(unsigned long baud) {
    (void)baud;
    begun = true;
  }
  void print(const char *text) { log += text; }
  void print(const String &text) { log += text.c_str(); }
  void println(const char *text) { log += text; log += "\n"; }
  void println(const String &text) { log += text.c_str(); log += "\n"; }
};

extern BridgeClass Bridge;
extern MonitorClass Monitor;
