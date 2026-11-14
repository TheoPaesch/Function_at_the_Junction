"""Link to the microcontroller: one interface, two implementations.

RouterBridgeLink  talks to the real sketch through arduino.app_utils.Bridge.
SimLink           is a pure-Python twin of sketch.ino in virtual mode (virtual
                  scale, virtual valve, start lag, water in flight, lead
                  learning, safety stops) for dev mode and the self-test.
Board             wraps either one: it keeps the latest status, queues events
                  and makes every call safe (exceptions never reach the caller).
"""

import collections
import logging
import random
import threading
import time

log = logging.getLogger("board")

# ---- Bridge contract (must match sketch.ino) -------------------------------

# name -> parameter types. MsgPack is strict: float parameters need float(),
# int parameters need int(), so Board.call() converts with this table.
SIGNATURES = {
    "info": (),
    "tare": (),
    "dispense": (float,),
    "stop": (),
    "ping": (),
    "sim_add": (float,),
    "sim_set": (float,),
    "show": (int,),
    "beep": (int,),
    "set_param": (str, float),
    "calibrate": (float,),
}
NOTIFY_STATUS = "mcu_status"   # (net_g, stable, valve_open, mode, dispensed_g, flow_gps, virtual_scale)
NOTIFY_EVENT = "mcu_event"     # (kind, value)

MODE_IDLE, MODE_DISPENSING, MODE_SETTLING = 0, 1, 2
SCREEN_NAMES = ("idle", "vessel", "ingredient", "ready", "dispensing", "done", "error", "thinking")
BEEP_NAMES = {1: "click", 2: "start", 3: "done", 4: "error"}

ONLINE_WINDOW_S = 2.0          # board.online: a status arrived within this time

# ---- constants shared with the sketch (keep in step with sketch.ino) -------

STATUS_PERIOD_S = 0.2
SAMPLE_PERIOD_S = 0.1          # virtual scale: 10 samples per second, like an HX711
NOISE_G = 0.3
STABLE_SAMPLES = 10            # one second of samples
STABLE_RANGE_G = 2.0
KNOB_STEP_G = 50.0
FLOW_WINDOW_S = 0.2
FLOW_EMA = 0.4
EXTRAPOLATE_MAX_S = 0.15
SETTLE_S = 1.5
LEAD_DEFAULT_S = 0.4
LEAD_MAX_S = 2.0
LEAD_MAX_STEP_S = 0.3
MAX_DISPENSE_ML = 4000.0
VESSEL_DROP_G = 150.0
NOFLOW_GRACE_S = 3.0
NOFLOW_WINDOW_S = 4.0
NOFLOW_MIN_G = 5.0
PING_TIMEOUT_S = 2.0
TIMEOUT_MIN_FLOW_GPS = 5.0
TIMEOUT_EXTRA_S = 10.0
TIMEOUT_CAP_S = 180.0
TILT_HOLD_S = 0.3
GREEN_S = 3.0
BOOT_REPEAT_S = 2.0
FLOW_GPS_DEFAULT = 30.0
FLOW_GPS_MAX = 200.0


def dispense_timeout_s(ml):
    """Longest time the valve may stay open for ml of water."""
    return min(float(ml) / TIMEOUT_MIN_FLOW_GPS + TIMEOUT_EXTRA_S, TIMEOUT_CAP_S)


def parse_info(text):
    """'fw=1.0;scale=virtual;knob=1;buzzer=1;tilt=0' -> dict."""
    out = {}
    for part in str(text or "").split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip()] = value.strip()
    return out


class BoardLink(object):
    """What the app needs from a board connection."""

    def start(self, on_status, on_event):
        """Register the two notification handlers. Called once, before the app runs."""
        raise NotImplementedError

    def call(self, name, *args, **kwargs):
        """Call a function the MCU provides. Raises on timeout or when the board is not ready."""
        raise NotImplementedError

    def debug_state(self):
        """Dev mode only: what the board would show (matrix screen, RGB LED, valve)."""
        return None


class RouterBridgeLink(BoardLink):
    """The real board, through the App Lab Router Bridge."""

    def __init__(self, bridge=None):
        if bridge is None:
            from arduino.app_utils import Bridge as bridge  # only exists on the board
        self._bridge = bridge

    def start(self, on_status, on_event):
        self._bridge.provide(NOTIFY_STATUS, on_status)
        self._bridge.provide(NOTIFY_EVENT, on_event)

    def call(self, name, *args, **kwargs):
        return self._bridge.call(name, *args, timeout=kwargs.get("timeout", 2))


class Board(object):
    """Thread-safe front of a BoardLink.

    Bridge callbacks only store data here; the state machine reads status()
    and drain_events() from its own thread. Every MCU call goes through call(),
    which catches all exceptions and marks the board offline.
    """

    def __init__(self, link, clock=time.monotonic):
        self.link = link
        self._clock = clock
        self._lock = threading.Lock()       # status, events, capabilities
        self._io_lock = threading.Lock()    # one MCU call at a time
        self._status = None
        self._seq = 0
        self._events = collections.deque(maxlen=200)
        self._call_failed = False
        self._last_error_log = 0.0
        self._caps = {"fw": None, "scale": None, "knob": False, "buzzer": False, "tilt": False}
        link.start(self._on_status, self._on_event)

    # ---- notifications from the MCU (bridge thread: store only) ------------

    def _on_status(self, net_g, stable, valve_open, mode, dispensed_g, flow_gps, virtual_scale):
        try:
            status = {
                "net_g": float(net_g),
                "stable": bool(stable),
                "valve_open": bool(valve_open),
                "mode": int(mode),
                "dispensed_g": float(dispensed_g),
                "flow_gps": float(flow_gps),
                "virtual_scale": bool(virtual_scale),
            }
        except (TypeError, ValueError):
            return
        with self._lock:
            self._seq += 1
            status["seq"] = self._seq
            status["at"] = self._clock()
            self._status = status
            self._call_failed = False

    def _on_event(self, kind, value=0.0):
        try:
            if isinstance(kind, (bytes, bytearray)):
                kind = kind.decode("utf-8", "replace")
            item = (str(kind), float(value))
        except (TypeError, ValueError):
            return
        with self._lock:
            self._events.append(item)

    # ---- read side ---------------------------------------------------------

    def status(self):
        """Latest status as a dict (with 'at' and 'seq'), or None before the first one."""
        with self._lock:
            return dict(self._status) if self._status else None

    def status_age(self):
        with self._lock:
            if not self._status:
                return float("inf")
            return max(0.0, self._clock() - self._status["at"])

    def reachable(self):
        return self.status_age() < ONLINE_WINDOW_S

    def online(self):
        with self._lock:
            failed = self._call_failed
        return self.reachable() and not failed

    def drain_events(self):
        with self._lock:
            items = list(self._events)
            self._events.clear()
        return items

    def caps(self):
        with self._lock:
            return dict(self._caps)

    def set_info(self, text):
        info = parse_info(text)
        with self._lock:
            self._caps = {
                "fw": info.get("fw"),
                "scale": info.get("scale"),
                "knob": info.get("knob") == "1",
                "buzzer": info.get("buzzer") == "1",
                "tilt": info.get("tilt") == "1",
            }

    def virtual_scale(self):
        """True, False, or None while we do not know yet."""
        with self._lock:
            if self._status:
                return self._status["virtual_scale"]
            if self._caps["scale"]:
                return self._caps["scale"] == "virtual"
        return None

    def debug_state(self):
        return self.link.debug_state()

    # ---- write side --------------------------------------------------------

    def call(self, name, *args, **kwargs):
        """Call the MCU. Returns (ok, result); never raises.

        When no status has arrived for 2 s the call is skipped at once (so the
        phone never waits on a dead board) unless force=True.
        """
        timeout = kwargs.get("timeout", 2.0)
        force = kwargs.get("force", False)
        types = SIGNATURES.get(name)
        if types is None or len(types) != len(args):
            log.error("bad MCU call %s%r", name, args)
            return False, None
        try:
            converted = [kind(arg) for kind, arg in zip(types, args)]
        except (TypeError, ValueError):
            log.error("bad arguments for MCU call %s%r", name, args)
            return False, None
        if not force and not self.reachable():
            return False, None
        try:
            with self._io_lock:
                result = self.link.call(name, *converted, timeout=timeout)
            return True, result
        except Exception as exc:  # timeout, board not ready, anything else
            with self._lock:
                self._call_failed = True
                now = self._clock()
                quiet = now - self._last_error_log < 5.0
                if not quiet:
                    self._last_error_log = now
            if not quiet:
                log.warning("MCU call %s failed: %s", name, exc)
            return False, None


class SimLink(BoardLink):
    """Pure-Python twin of sketch.ino running its virtual scale and virtual valve.

    Time comes from an injectable clock, so tests can run a 60 s dispense in
    milliseconds: advance the clock, call step(), repeat. In dev mode
    start_thread() steps it in real time.
    """

    DT = 0.01   # one simulated loop() pass

    def __init__(self, clock=time.monotonic, knob=True, buzzer=True, tilt=False,
                 start_lag_s=0.3, tail_s=0.4, seed=7):
        self._clock = clock
        self._lock = threading.RLock()
        self._rng = random.Random(seed)
        self._on_status = None
        self._on_event = None
        self._out = []
        self._thread = None
        self.offline = False             # True: calls raise, notifications are lost
        self.has_knob = bool(knob)
        self.has_buzzer = bool(buzzer)
        self.has_tilt = bool(tilt)
        self.start_lag_s = float(start_lag_s)
        self.tail_s = float(tail_s)
        self._t = clock()
        self._boot()

    # ---- BoardLink ---------------------------------------------------------

    def start(self, on_status, on_event):
        self._on_status = on_status
        self._on_event = on_event

    def call(self, name, *args, **kwargs):
        if self.offline:
            raise TimeoutError("simulated board is offline")
        handler = getattr(self, "_rpc_" + name, None)
        if handler is None:
            raise RuntimeError("the sketch does not provide '{}'".format(name))
        with self._lock:
            return handler(*args)

    def debug_state(self):
        with self._lock:
            if self._valve_open:
                rgb = "blue"
            elif self._error_active:
                rgb = "red"
            elif self._t < self._green_until:
                rgb = "green"
            else:
                rgb = "off"
            screen = 4 if self._valve_open else self._screen
            beep = BEEP_NAMES.get(self._last_beep) if self._t - self._last_beep_t < 1.5 else None
            return {
                "screen": SCREEN_NAMES[screen],
                "rgb": rgb,
                "valve_open": self._valve_open,
                "beep": beep,
                "gross_g": round(self._gross, 1),
                "lead_s": round(self._lead_s, 3),
            }

    # ---- driving the simulation --------------------------------------------

    def step(self):
        """Advance the simulated board to the clock's current time."""
        with self._lock:
            now = self._clock()
            if now - self._t > 5.0:          # the laptop slept: do not replay the gap
                self._t = now - self.DT
            while self._t + self.DT <= now + 1e-9:
                self._advance(self.DT)
            out, self._out = self._out, []
        if self.offline:
            return
        for item in out:
            try:
                if item[0] == "status" and self._on_status:
                    self._on_status(*item[1:])
                elif item[0] == "event" and self._on_event:
                    self._on_event(item[1], item[2])
            except Exception:
                log.exception("notification handler failed")

    def start_thread(self, period=0.005):
        def run():
            while True:
                try:
                    self.step()
                except Exception:
                    log.exception("simulation step failed")
                time.sleep(period)
        self._thread = threading.Thread(target=run, name="simlink", daemon=True)
        self._thread.start()

    # ---- things a person (or a test) does to the board ---------------------

    def knob_turn(self, detents):
        """Each detent adds or removes 50 g on the virtual scale."""
        with self._lock:
            if not self.has_knob or not detents:
                return
            self._gross = max(0.0, self._gross + KNOB_STEP_G * int(detents))
            self._emit("knob_turn", float(int(detents)))

    def knob_press(self):
        with self._lock:
            if not self.has_knob:
                return
            if self._mode == MODE_DISPENSING:
                self._req_stop = True          # the Knob is also a hardware stop button
            self._emit("knob_press", 1.0)

    def set_tilt(self, tilted):
        with self._lock:
            self._tilted = bool(tilted)
            self._tilt_since = self._t if tilted else None

    def reboot(self):
        """Power-cycle the MCU: everything it knew is gone."""
        with self._lock:
            self._boot()

    # ---- internals: the same steps as sketch.ino ---------------------------

    def _boot(self):
        t = self._t
        # scale
        self._gross = 0.0
        self._tare_g = 0.0
        self._sample_g = 0.0
        self._sample_t = t
        self._ring = collections.deque(maxlen=STABLE_SAMPLES)
        self._next_sample_t = t + SAMPLE_PERIOD_S
        # valve and virtual water stream
        self._valve_open = False
        self._stream_start = None
        self._stream_end = None
        # dispense loop
        self._mode = MODE_IDLE
        self._target_ml = 0.0
        self._start_g = 0.0
        self._goal_g = 0.0
        self._start_t = t
        self._settle_t = t
        self._timeout_s = 0.0
        self._last_ping = t
        self._flow_est = 0.0
        self._flow_at_close = 0.0
        self._flow_win_t = t
        self._flow_win_g = 0.0
        self._nf_ring = collections.deque(maxlen=int(round(NOFLOW_WINDOW_S / FLOW_WINDOW_S)) + 1)
        self._dispensed_g = 0.0
        # parameters
        self._flow_gps = FLOW_GPS_DEFAULT
        self._lead_s = LEAD_DEFAULT_S
        self._cal = None
        # requests set by handlers, done by the loop
        self._req_stop = False
        self._req_tare = False
        self._req_dispense = None
        # outputs
        self._screen = 0
        self._error_active = False
        self._green_until = t
        self._last_beep = 0
        self._last_beep_t = t - 10.0
        # misc
        self._tilted = False
        self._tilt_since = None
        self._synced = False
        self._next_boot_t = t
        self._next_status_t = t + STATUS_PERIOD_S

    def _emit(self, kind, value):
        self._out.append(("event", kind, float(value)))

    def _net(self):
        return self._sample_g - self._tare_g

    def _stable(self):
        ring = self._ring
        return len(ring) == STABLE_SAMPLES and (max(ring) - min(ring)) < STABLE_RANGE_G

    def _set_valve(self, open_, t):
        if open_ and not self._valve_open:
            self._valve_open = True
            self._stream_start = t + self.start_lag_s
            self._stream_end = None
        elif not open_ and self._valve_open:
            self._valve_open = False
            if t >= self._stream_start:
                self._stream_end = t + self.tail_s      # water in flight
            else:
                self._stream_end = self._stream_start   # closed before any water came

    def _begin_dispense(self, ml, t):
        self._target_ml = ml
        self._start_g = self._net()
        self._goal_g = self._start_g + ml
        self._start_t = t
        self._timeout_s = dispense_timeout_s(ml)
        self._last_ping = t
        self._flow_est = 0.0
        self._flow_at_close = 0.0
        self._flow_win_t = t
        self._flow_win_g = self._start_g
        self._nf_ring.clear()
        self._nf_ring.extend([self._start_g] * self._nf_ring.maxlen)
        self._dispensed_g = 0.0
        self._req_stop = False
        self._error_active = False
        self._screen = 4
        self._mode = MODE_DISPENSING
        self._set_valve(True, t)

    def _take_sample(self, gross, t):
        self._sample_g = gross
        self._sample_t = t
        self._ring.append(gross)
        if self._mode == MODE_DISPENSING and t - self._flow_win_t >= FLOW_WINDOW_S - 1e-6:
            net = gross - self._tare_g
            inst = max(0.0, (net - self._flow_win_g) / (t - self._flow_win_t))
            if self._flow_est <= 0.01:
                if inst > 2.0:
                    self._flow_est = inst
            else:
                self._flow_est = (1.0 - FLOW_EMA) * self._flow_est + FLOW_EMA * inst
            self._flow_win_t = t
            self._flow_win_g = net
            self._nf_ring.append(net)

    def _safety_stop(self, code, t):
        self._set_valve(False, t)
        self._mode = MODE_IDLE
        self._dispensed_g = max(0.0, self._net() - self._start_g)
        self._error_active = True
        self._screen = 6
        self._last_beep = 4
        self._last_beep_t = t
        self._emit("error_" + code, self._dispensed_g)

    def _finish(self, t):
        actual = self._net() - self._start_g
        self._dispensed_g = actual
        self._mode = MODE_IDLE
        self._green_until = t + GREEN_S
        self._emit("done", actual)
        change = 0.5 * (actual - self._target_ml) / max(self._flow_at_close, 1.0)
        change = max(-LEAD_MAX_STEP_S, min(LEAD_MAX_STEP_S, change))
        self._lead_s = max(0.0, min(LEAD_MAX_S, self._lead_s + change))
        self._emit("lead_s", self._lead_s)

    def _advance(self, dt):
        t0 = self._t
        t1 = t0 + dt
        self._t = t1

        # 1. requests from the handlers
        if self._req_tare:
            self._req_tare = False
            self._tare_g = self._gross
        if self._req_dispense is not None:
            ml, self._req_dispense = self._req_dispense, None
            self._begin_dispense(ml, t1)

        # 2. virtual water landing on the scale
        if self._stream_start is not None:
            a = max(t0, self._stream_start)
            b = t1 if self._stream_end is None else min(t1, self._stream_end)
            if b > a:
                self._gross += self._flow_gps * (b - a)

        # 3. scale sample
        if t1 >= self._next_sample_t - 1e-9:
            self._next_sample_t += SAMPLE_PERIOD_S
            self._take_sample(self._gross + self._rng.uniform(-NOISE_G, NOISE_G), t1)

        # 4. dispense loop and safety stops
        if self._mode == MODE_DISPENSING:
            net = self._net()
            open_for = t1 - self._start_t
            code = None
            if self._req_stop:
                code = "stopped"
            elif open_for > self._timeout_s:
                code = "timeout"
            elif t1 - self._last_ping > PING_TIMEOUT_S:
                code = "link_lost"
            elif net < self._start_g - VESSEL_DROP_G:
                code = "vessel_removed"
            elif open_for >= NOFLOW_GRACE_S and net - self._nf_ring[0] < NOFLOW_MIN_G:
                code = "no_flow"
            elif (self.has_tilt and self._tilted and self._tilt_since is not None
                  and t1 - self._tilt_since > TILT_HOLD_S):
                code = "tilt"
            if code:
                self._safety_stop(code, t1)
            else:
                estimate = net
                if self._flow_est > 0.0:
                    estimate += self._flow_est * min(t1 - self._sample_t, EXTRAPOLATE_MAX_S)
                if estimate + self._flow_est * self._lead_s >= self._goal_g:
                    self._set_valve(False, t1)
                    self._flow_at_close = self._flow_est
                    self._mode = MODE_SETTLING
                    self._settle_t = t1
        elif self._mode == MODE_SETTLING:
            if self._req_stop:
                self._safety_stop("stopped", t1)
            elif self._net() < self._start_g - VESSEL_DROP_G:
                self._safety_stop("vessel_removed", t1)
            elif t1 - self._settle_t >= SETTLE_S:
                self._finish(t1)
        self._req_stop = False

        # 5. boot event until Python has asked for info(), then status every 200 ms
        if not self._synced and t1 >= self._next_boot_t:
            self._next_boot_t = t1 + BOOT_REPEAT_S
            self._emit("boot", 0.0)
        if t1 >= self._next_status_t - 1e-9:
            self._next_status_t += STATUS_PERIOD_S
            net = self._net()
            if self._mode != MODE_IDLE:
                self._dispensed_g = max(0.0, net - self._start_g)
            flow = self._flow_est if self._mode == MODE_DISPENSING else 0.0
            self._out.append(("status", net, self._stable(), self._valve_open, self._mode,
                              self._dispensed_g, flow, True))

    # ---- handlers (same names and behaviour as the rpc_* functions) --------

    def _rpc_info(self):
        self._synced = True
        return "fw=1.0;scale=virtual;knob={};buzzer={};tilt={}".format(
            int(self.has_knob), int(self.has_buzzer), int(self.has_tilt))

    def _rpc_tare(self):
        if self._mode != MODE_IDLE or self._req_dispense is not None:
            return False
        self._req_tare = True
        return True

    def _rpc_dispense(self, ml):
        if self._mode != MODE_IDLE or self._req_dispense is not None:
            return False
        if not (0.0 < ml <= MAX_DISPENSE_ML):
            return False
        if self._net() < -VESSEL_DROP_G:        # the vessel left the plate after the tare
            return False
        self._req_dispense = float(ml)
        return True

    def _rpc_stop(self):
        self._req_dispense = None
        if self._mode != MODE_IDLE:
            self._req_stop = True
        return True

    def _rpc_ping(self):
        self._last_ping = self._t
        return True

    def _rpc_sim_add(self, grams):
        self._gross = max(0.0, self._gross + float(grams))
        return True

    def _rpc_sim_set(self, grams):
        self._gross = max(0.0, float(grams))
        return True

    def _rpc_show(self, screen):
        if not 0 <= screen < len(SCREEN_NAMES):
            return False
        self._screen = int(screen)
        self._error_active = screen == 6
        return True

    def _rpc_beep(self, pattern):
        if pattern not in BEEP_NAMES:
            return False
        self._last_beep = int(pattern)
        self._last_beep_t = self._t
        return True

    def _rpc_set_param(self, name, value):
        if name == "flow_gps":
            self._flow_gps = max(0.0, min(FLOW_GPS_MAX, float(value)))
        elif name == "lead_s":
            self._lead_s = max(0.0, min(LEAD_MAX_S, float(value)))
        elif name == "cal":
            if abs(float(value)) > 0.01:
                self._cal = float(value)
        else:
            return False
        return True

    def _rpc_calibrate(self, known_g):
        return 0.0      # the virtual scale has nothing to calibrate
