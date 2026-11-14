"""The flow state machine. Python is the source of truth; the MCU runs the valve.

    idle -> vessel -> [ingredient] -> ready -> dispensing -> done      (+ error)

Threads: API handlers (FastAPI thread pool), the main loop (tick), the board
sync thread and bridge callbacks all meet here. One lock guards the flow
state. MCU calls are never made while holding it: transitions put commands in
an outbox, and whoever made the transition sends them right after unlocking,
in order. So the phone UI stays responsive even when the board is slow.
"""

import collections
import logging
import math
import threading
import time

import energy
from board import MODE_DISPENSING, MODE_IDLE, ONLINE_WINDOW_S, VESSEL_DROP_G, dispense_timeout_s
from recipes import fmt_ml

log = logging.getLogger("core")

APP_NAME = "1-T_AT"
APP_VERSION = "1.0"

STATES = ("idle", "vessel", "ingredient", "ready", "dispensing", "done", "error")
ACTIVE_STATES = ("vessel", "ingredient", "ready", "dispensing")
SELECTABLE_STATES = ("idle", "done", "error")

# LED matrix screens and buzzer patterns (same numbers as in sketch.ino)
SCREEN = {"idle": 0, "vessel": 1, "ingredient": 2, "ready": 3, "dispensing": 4, "done": 5, "error": 6}
SCREEN_THINKING = 7
BEEP_CLICK, BEEP_START, BEEP_DONE, BEEP_ERROR = 1, 2, 3, 4

VESSEL_RISE_G = 150.0       # weight gain that counts as "the vessel is on the plate"
INGREDIENT_MIN_G = 50.0
COUNTDOWN_S = 3.0
PING_PERIOD_S = 0.5
TARE_MIN_WAIT_S = 0.3       # at least one fresh status after the tare
TARE_MAX_WAIT_S = 6.0
OFFLINE_FAIL_S = 3.0        # no status for this long during a step: give up
DONE_TIMEOUT_S = 60.0
ERROR_TIMEOUT_S = 120.0
SIM_VESSEL_G = {"kettle": 900.0, "pot": 1500.0}

MSG_IDLE = "What are you making?"
ERROR_CODES = ("no_flow", "timeout", "vessel_removed", "link_lost", "tilt", "stopped", "board_offline")
ERROR_TEXT = {
    "no_flow": "No water is coming. Check the water supply, then try again.",
    "timeout": "That took too long, so I closed the valve.",
    "vessel_removed": "The {vessel} left the plate, so I stopped the water.",
    "link_lost": "The board lost contact with the app, so it closed the valve.",
    "tilt": "The {vessel} was lifted or tipped, so I stopped the water.",
    "stopped": "Stopped. The valve is closed.",
    "board_offline": "The board is not answering.",
}
ERROR_HINT = {
    "board_offline": "Check that the app is running on the board. The board closes the valve by itself.",
}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


class Machine(object):
    """Flow state plus everything the REST API needs."""

    def __init__(self, board, book, storage, intent, clock=time.monotonic):
        self.board = board
        self.book = book
        self.storage = storage
        self.intent = intent
        self._clock = clock
        self._lock = threading.Lock()            # the flow state
        self._outbox = collections.deque()       # MCU commands waiting to be sent, in order
        self._send_order = threading.Lock()      # one sender at a time keeps that order
        self._need_sync = True
        self._syncing = False
        self._synced_at = None
        self._last_guard_stop = 0.0
        self._last_run = storage.last_run()
        self._state = "idle"
        self._since = clock()
        self._reset_flow()

    # ======================================================================
    # helpers (call with the lock held unless noted)
    # ======================================================================

    def _reset_flow(self):
        self._recipe = None
        self._portions = None
        self._grams = None
        self._ingredient_g = None
        self._target_ml = None
        self._dispensed_ml = 0
        self._error = None
        self._message = MSG_IDLE
        self._hint = None
        self._phase = None
        self._ref_g = None              # vessel step: lowest weight seen so far
        self._tare_sent_at = None
        self._tare_at = None
        self._ready_until = None
        self._last_ping = 0.0
        self._ping_queued = False
        self._dispense_ok = None
        self._seen_active = False
        self._idle_since = None
        self._max_time = 0.0

    def _send(self, name, *args, **options):
        """Queue an MCU call. options: cb(ok, result), front=True, force=True."""
        item = (name, args, options.get("cb"), bool(options.get("force")))
        if options.get("front"):
            self._outbox.appendleft(item)
        else:
            self._outbox.append(item)

    def _flush(self):
        """Send the queued MCU calls. Call WITHOUT the lock held."""
        while True:
            with self._send_order:
                try:
                    name, args, callback, force = self._outbox.popleft()
                except IndexError:
                    return
                if name == "stop":      # always try to close the valve, even when the board looks dead
                    ok, result = self.board.call(name, *args, force=True, timeout=1.0)
                else:
                    ok, result = self.board.call(name, *args, force=force)
            if callback is not None:
                try:
                    callback(ok, result)
                except Exception:
                    log.exception("callback for %s failed", name)

    def _vessel(self):
        return self._recipe["vessel"] if self._recipe else "vessel"

    def _target(self, settings, weighed_g=None):
        return self.book.water_ml(self._recipe, self._portions, self._grams, weighed_g,
                                  settings["kettle_min_ml"])

    def _suggested_g(self):
        recipe = self._recipe
        if recipe is None or not self.book.uses_grams(recipe):
            return None
        return int(round(self.book.basis_grams(recipe, self._portions, self._grams)))

    def _sorry(self):
        return "Sorry, I can do {}.".format(self.book.names_sentence())

    # ---- transitions -------------------------------------------------------

    def _to_idle(self, now):
        self._reset_flow()
        self._state = "idle"
        self._since = now
        self._send("show", SCREEN["idle"])

    def _fail(self, code, now, from_mcu=False, dispensed_g=None, message=None):
        """Go to the error state. from_mcu: the board already closed the valve and beeped."""
        was = self._state
        if dispensed_g is not None:
            self._dispensed_ml = int(round(max(0.0, dispensed_g)))
        self._state = "error"
        self._since = now
        self._error = code if code in ERROR_CODES else "stopped"
        self._message = message or ERROR_TEXT[self._error].format(vessel=self._vessel())
        self._hint = self._error_hint()
        if was == "dispensing" and not from_mcu:
            self._send("stop", front=True)
        self._send("show", SCREEN["error"])
        if not from_mcu:
            self._send("beep", BEEP_ERROR)

    def _error_hint(self):
        if self._error in ERROR_HINT:
            return ERROR_HINT[self._error]
        closed = "" if self._error == "stopped" else "The valve is closed. "     # "stopped" already says so
        if self._dispensed_ml > 0:
            return closed + "{} went in before the stop.".format(fmt_ml(self._dispensed_ml))
        return closed + "No water went in." if self._error == "stopped" else "The valve is closed."

    def _enter_vessel(self, now):
        recipe = self._recipe
        settings = self.storage.settings()
        status = self.board.status()
        self._state = "vessel"
        self._since = now
        self._phase = "wait"
        self._ref_g = status["net_g"] if status else None
        self._target_ml = None if recipe["weigh_in_pot"] else self._target(settings)
        self._message = "Put the empty {} on the plate".format(recipe["vessel"])
        if recipe["vessel"] == "kettle":
            hint = ("Empty it first: leftover water would be zeroed away and spoil the numbers. "
                    "Check your kettle's minimum fill line: I add at least {}."
                    .format(fmt_ml(settings["kettle_min_ml"])))
        else:
            hint = "Empty it first: leftover water or food would be zeroed away and spoil the numbers."
        if recipe.get("note"):
            hint += " " + recipe["note"]
        self._hint = hint
        self._send("show", SCREEN["vessel"])
        self._send("beep", BEEP_CLICK)

    def _begin_tare(self, now):
        self._phase = "tare"
        self._tare_sent_at = now
        self._tare_at = None
        self._message = "Got it. Zeroing the scale…"
        self._send("tare", cb=self._on_tare_result)

    def _on_tare_result(self, ok, result):       # called without the lock
        now = self._clock()
        with self._lock:
            if self._state != "vessel" or self._phase != "tare":
                return
            if ok and result is not False:
                self._tare_at = now
            else:
                self._fail("board_offline", now)

    def _after_vessel(self, now):
        if self._recipe["weigh_in_pot"]:
            self._enter_ingredient(now)
        else:
            self._enter_ready(now)

    def _enter_ingredient(self, now):
        recipe = self._recipe
        self._state = "ingredient"
        self._since = now
        self._phase = None
        self._ingredient_g = 0
        self._target_ml = self._target(self.storage.settings(), weighed_g=0)
        self._message = "Put the {} in".format(recipe["name"].lower())
        self._hint = "About {:,} g for {}. The water follows the weight.".format(
            self._suggested_g(), self.book.portion_label(recipe, self._portions))
        self._send("show", SCREEN["ingredient"])
        self._send("beep", BEEP_CLICK)

    def _enter_ready(self, now):
        recipe = self._recipe
        weighed = self._ingredient_g if recipe["weigh_in_pot"] else None
        self._target_ml = self._target(self.storage.settings(), weighed_g=weighed)
        self._state = "ready"
        self._since = now
        self._phase = None
        self._ready_until = now + COUNTDOWN_S
        self._message = "Adding " + fmt_ml(self._target_ml)
        self._hint = "Starts by itself. Keep the {} on the plate.".format(recipe["vessel"])
        self._send("show", SCREEN["ready"])
        self._send("beep", BEEP_CLICK)

    def _start_dispense(self, now):
        self._state = "dispensing"
        self._since = now
        self._dispensed_ml = 0
        self._last_ping = now
        self._ping_queued = False
        self._dispense_ok = None
        self._seen_active = False
        self._idle_since = None
        self._max_time = dispense_timeout_s(self._target_ml) + 8.0
        self._message = "Adding water…"
        self._hint = "Keep the {} on the plate.".format(self._vessel())
        self._send("dispense", float(self._target_ml), cb=self._on_dispense_result)
        self._send("beep", BEEP_START)

    def _on_dispense_result(self, ok, result):   # called without the lock
        now = self._clock()
        status = self.board.status()
        with self._lock:
            if self._state != "dispensing":
                return
            if ok and result:
                self._dispense_ok = True
            elif ok and status and status["net_g"] < -VESSEL_DROP_G:
                self._fail("vessel_removed", now)
            elif ok:
                self._fail("board_offline", now, message="The board did not start the water. Please try again.")
            else:
                self._fail("board_offline", now)

    def _on_ping_result(self, ok, result):       # called without the lock
        with self._lock:
            self._ping_queued = False

    def _finish(self, actual_g, now):
        """The MCU reported the final amount: work out the savings and store the run."""
        recipe = self._recipe
        settings = self.storage.settings()
        dispensed = int(round(max(0.0, actual_g)))
        weighed = self._ingredient_g if recipe["weigh_in_pot"] else None
        basis = self.book.basis_grams(recipe, self._portions, self._grams, weighed)
        baseline = self.book.baseline_ml(recipe, self._target_ml, basis, settings.get("baselines"))
        saved = energy.savings(recipe["vessel"], baseline, dispensed, settings)
        run = {
            "recipe": recipe["id"],
            "name": recipe["name"],
            "emoji": recipe["emoji"],
            "vessel": recipe["vessel"],
            "portions": self._portions,
            "amount": self.book.portion_label(recipe, self._portions),
            "grams": int(round(basis)) if basis is not None else None,
            "target_ml": self._target_ml,
            "dispensed_ml": dispensed,
            "baseline_ml": baseline,
            "saved_ml": saved["saved_ml"],
            "saved_wh": saved["saved_wh"],
            "saved_eur": saved["saved_eur"],
            "saved_seconds": saved["saved_seconds"],
            "used_wh": saved["used_wh"],
            "tip": recipe["tip"],
            "baseline_note": recipe["baseline_note"],
        }
        self._last_run = self.storage.add_run(run)
        self._dispensed_ml = dispensed
        self._state = "done"
        self._since = now
        self._message = "Done! {} in the {}.".format(fmt_ml(dispensed), recipe["vessel"])
        self._hint = recipe["tip"]
        self._send("show", SCREEN["done"])
        self._send("beep", BEEP_DONE)

    # ======================================================================
    # main loop
    # ======================================================================

    def tick(self):
        """Advance the flow. Called about every 50-100 ms from the main loop."""
        now = self._clock()
        events = self.board.drain_events()
        status = self.board.status()
        age = self.board.status_age()
        with self._lock:
            for kind, value in events:
                self._on_event(kind, value, now, status)
            self._step(now, status, age)
        self._flush()

    def _step(self, now, status, age):
        state = self._state
        if state == "vessel":
            self._step_vessel(now, status, age)
        elif state == "ingredient":
            self._step_ingredient(now, status, age)
        elif state == "ready":
            self._step_ready(now, status, age)
        elif state == "dispensing":
            self._step_dispensing(now, status, age)
        elif state == "done":
            if now - self._since >= DONE_TIMEOUT_S:
                self._to_idle(now)
        elif state == "error":
            if now - self._since >= ERROR_TIMEOUT_S:
                self._to_idle(now)

        # Safety net: the valve must never be open unless we are dispensing.
        if (self._state != "dispensing" and status is not None and age < ONLINE_WINDOW_S
                and (status["valve_open"] or status["mode"] == MODE_DISPENSING)
                and now - self._last_guard_stop > 1.0):
            self._last_guard_stop = now
            self._send("stop", front=True)

    def _step_vessel(self, now, status, age):
        if age > OFFLINE_FAIL_S:
            self._fail("board_offline", now)
            return
        if status is None:
            return
        if self._phase == "wait":
            net = status["net_g"]
            if self._ref_g is None or net < self._ref_g:
                self._ref_g = net
            if status["stable"] and net - self._ref_g >= VESSEL_RISE_G:
                self._begin_tare(now)
        elif self._tare_at is None:
            if now - self._tare_sent_at > 5.0:
                self._fail("board_offline", now)
        else:
            waited = now - self._tare_at
            fresh = status["at"] > self._tare_at + 0.05
            if (waited >= TARE_MIN_WAIT_S and fresh and status["stable"]) or waited > TARE_MAX_WAIT_S:
                self._after_vessel(now)

    def _step_ingredient(self, now, status, age):
        if age > OFFLINE_FAIL_S:
            self._fail("board_offline", now)
            return
        if status is None:
            return
        if status["net_g"] < -VESSEL_DROP_G:
            self._fail("vessel_removed", now)
            return
        grams = int(round(max(0.0, status["net_g"])))
        if grams != self._ingredient_g:
            self._ingredient_g = grams
            self._target_ml = self._target(self.storage.settings(), weighed_g=grams)

    def _step_ready(self, now, status, age):
        if age > OFFLINE_FAIL_S:
            self._fail("board_offline", now)
        elif status is not None and status["net_g"] < -VESSEL_DROP_G:
            self._fail("vessel_removed", now)      # never pour onto an empty plate
        elif now >= self._ready_until:
            self._start_dispense(now)

    def _step_dispensing(self, now, status, age):
        if age >= ONLINE_WINDOW_S:
            self._fail("board_offline", now)
            return
        if now - self._last_ping >= PING_PERIOD_S and not self._ping_queued:
            self._last_ping = now
            self._ping_queued = True
            self._send("ping", cb=self._on_ping_result)
        if status is None:
            return
        if status["mode"] != MODE_IDLE:
            self._seen_active = True
            self._idle_since = None
            self._dispensed_ml = int(round(max(0.0, status["dispensed_g"])))
        elif self._seen_active:
            # The MCU is idle again but its "done" event never arrived: use its last status.
            if self._idle_since is None:
                self._idle_since = now
            elif now - self._idle_since > 1.0:
                self._finish(status["dispensed_g"], now)
                return
        elif self._dispense_ok and now - self._since > 3.0:
            self._fail("board_offline", now, message="The board did not start the water. Please try again.")
            return
        if now - self._since > self._max_time:
            self._fail("timeout", now)

    # ---- events from the MCU ----------------------------------------------

    def _on_event(self, kind, value, now, status):
        if kind == "boot":
            recently = self._synced_at is not None and now - self._synced_at < 3.0
            if self._syncing or recently:
                return
            self._need_sync = True
            if self._synced_at is not None and self._state in ACTIVE_STATES:
                self._fail("board_offline", now, from_mcu=True,
                           message="The board restarted. Please start again.")
        elif kind == "done":
            if self._state == "dispensing":
                self._finish(value, now)
        elif kind == "lead_s":
            self.storage.set_device("lead_s", value)
        elif kind.startswith("error_"):
            code = kind[len("error_"):]
            if self._state == "dispensing":
                self._fail(code, now, from_mcu=True, dispensed_g=value)
            elif self._state == "error" and self._error == code:
                self._dispensed_ml = int(round(max(0.0, value)))     # the exact amount arrives later
                self._hint = self._error_hint()
        elif kind == "knob_press":
            if self._state == "dispensing":
                self._send("stop", front=True)                       # the Knob is a stop button too
                self._fail("stopped", now, from_mcu=True)
            elif self._state != "idle":
                self._next(now)
        elif kind == "knob_turn":
            self._on_knob_turn(int(round(value)), now, status)

    def _on_knob_turn(self, detents, now, status):
        """Real scale only: the Knob changes the portions before the water starts.
        (With the virtual scale the MCU has already turned the detents into grams.)"""
        recipe = self._recipe
        if not detents or recipe is None or status is None or status["virtual_scale"]:
            return
        if self._state not in ("vessel", "ingredient", "ready") or self._grams:
            return
        portions = max(1, min(recipe["max_portions"], self._portions + detents))
        if portions == self._portions:
            return
        self._portions = portions
        settings = self.storage.settings()
        if recipe["weigh_in_pot"]:
            if self._state == "ingredient":
                self._hint = "About {:,} g for {}. The water follows the weight.".format(
                    self._suggested_g(), self.book.portion_label(recipe, portions))
        else:
            self._target_ml = self._target(settings)
            if self._state == "ready":
                self._ready_until = now + COUNTDOWN_S
                self._message = "Adding " + fmt_ml(self._target_ml)
                self._send("show", SCREEN["ready"])
        self._send("beep", BEEP_CLICK)

    # ======================================================================
    # board sync (its own thread)
    # ======================================================================

    def sync_once(self):
        """Ask the board who it is and push the stored parameters. True when it answered."""
        with self._lock:
            self._syncing = True
        try:
            ok, info = self.board.call("info", force=True)
            if not ok or info is None:
                return False
            if isinstance(info, (bytes, bytearray)):
                info = info.decode("utf-8", "replace")
            self.board.set_info(info)
            settings = self.storage.settings()
            self.board.call("set_param", "flow_gps", settings["flow_gps"], force=True)
            self.board.call("set_param", "lead_s", settings["lead_s"], force=True)
            if settings.get("cal"):
                self.board.call("set_param", "cal", settings["cal"], force=True)
            with self._lock:
                self._need_sync = False
                self._synced_at = self._clock()
                if self._state != "dispensing":
                    self._send("stop", front=True)
                if self._state != "ready":
                    self._send("show", SCREEN[self._state], force=True)
            self._flush()
            log.info("board: %s", info)
            return True
        finally:
            with self._lock:
                self._syncing = False

    def sync_loop(self, stop_event=None):
        """Wait for the board (retry every second), then again after every MCU reboot."""
        while stop_event is None or not stop_event.is_set():
            try:
                if self._need_sync and not self.sync_once():
                    time.sleep(1.0)
                    continue
            except Exception:
                log.exception("board sync failed")
                time.sleep(1.0)
            time.sleep(0.2)

    # ======================================================================
    # REST API (any thread)
    # ======================================================================

    def _reply(self, ok, note=None):
        snap = self.snapshot()
        snap["ok"] = bool(ok)
        snap["note"] = note
        return snap

    def select(self, recipe_id, portions=None, grams=None):
        """Start the flow at the vessel step. Accepted only in idle, done or error."""
        now = self._clock()
        recipe = self.book.get(recipe_id)
        with self._lock:
            if self._state not in SELECTABLE_STATES:
                ok, note = False, "Something is already in progress."
            elif recipe is None:
                ok, note = False, self._sorry()
            else:
                ok, note = True, None
                self._reset_flow()
                self._recipe = recipe
                self._portions = self.book.clamp_portions(recipe, portions)
                self._grams = self.book.clamp_grams(grams) if self.book.uses_grams(recipe) else None
                if self.board.reachable():
                    self._enter_vessel(now)
                else:
                    self._fail("board_offline", now)
        self._flush()
        return self._reply(ok, note)

    def _next(self, now):
        state = self._state
        if state == "vessel":
            if self._phase == "wait":
                self._begin_tare(now)
            return True, None
        if state == "ingredient":
            if (self._ingredient_g or 0) < INGREDIENT_MIN_G:
                return False, "Add at least {:.0f} g first.".format(INGREDIENT_MIN_G)
            self._enter_ready(now)
            return True, None
        if state == "ready":
            self._start_dispense(now)
            return True, None
        if state in ("done", "error"):
            self._to_idle(now)
            return True, None
        return False, None

    def next(self):
        """Manual advance: vessel placed, ingredient done, start now."""
        now = self._clock()
        with self._lock:
            ok, note = self._next(now)
        self._flush()
        return self._reply(ok, note)

    def cancel(self):
        """From any state back to idle; the valve is closed first."""
        now = self._clock()
        with self._lock:
            self._to_idle(now)
            self._send("stop", front=True)
        self._flush()
        return self._reply(True)

    def stop(self):
        """Emergency stop."""
        now = self._clock()
        with self._lock:
            if self._state == "dispensing":
                self._fail("stopped", now)      # queues stop first, then the error screen and beep
            else:
                if self._state in ("vessel", "ingredient", "ready"):
                    self._to_idle(now)
                self._send("stop", front=True)
        self._flush()
        return self._reply(True)

    def parse(self, text):
        """Free text -> order. Does not start the flow."""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "recipe": None, "portions": None, "grams": None, "source": "rules",
                    "ms": 0, "message": "Tell me what you are making, for example “2 cups of tea”."}
        thinking = False
        if self.intent.will_use_llm():
            with self._lock:
                if self._state in SELECTABLE_STATES:
                    self._send("show", SCREEN_THINKING)
                    thinking = True
            self._flush()
        try:
            result = self.intent.parse(text)
        finally:
            if thinking:
                with self._lock:
                    if self._state in SELECTABLE_STATES:
                        self._send("show", SCREEN[self._state])
                self._flush()

        recipe = self.book.get(result.get("recipe"))
        out = {"ok": recipe is not None, "recipe": None, "portions": None, "grams": None,
               "source": result.get("source", "rules"), "ms": int(result.get("ms") or 0)}
        if recipe is None:
            out["message"] = self._sorry()
            return out
        settings = self.storage.settings()
        portions = self.book.clamp_portions(recipe, result.get("portions"))
        grams = self.book.clamp_grams(result.get("grams")) if self.book.uses_grams(recipe) else None
        if out["source"] == "llm":
            badge = "understood on-device by {} in {:.1f} s".format(self.intent.label, out["ms"] / 1000.0)
        else:
            badge = "understood by rules"
        out.update({
            "recipe": recipe["id"],
            "portions": portions,
            "grams": grams,
            "message": self.book.order_line(recipe, portions, grams, settings["kettle_min_ml"]),
            "name": recipe["name"],
            "emoji": recipe["emoji"],
            "target_ml": self.book.water_ml(recipe, portions, grams, kettle_min_ml=settings["kettle_min_ml"]),
            "estimate": bool(recipe["weigh_in_pot"]),
            "badge": badge,
        })
        return out

    def sim(self, action, grams=None):
        """Virtual scale only: place or remove the vessel, add or set weight."""
        if not self.board.virtual_scale():
            return self._reply(False, "The real scale is connected, so there is nothing to simulate.")
        with self._lock:
            vessel = self._recipe["vessel"] if self._recipe else "kettle"
        amount = _number(grams) or 0.0
        link = self.board.link
        if action == "place_vessel":
            ok, _ = self.board.call("sim_add", SIM_VESSEL_G[vessel])
        elif action == "remove_vessel":
            ok, _ = self.board.call("sim_set", 0.0)
        elif action == "add":
            ok, _ = self.board.call("sim_add", max(-5000.0, min(5000.0, amount)))
        elif action == "set":
            ok, _ = self.board.call("sim_set", max(0.0, min(20000.0, amount)))
        elif action == "knob_turn" and hasattr(link, "knob_turn"):      # dev mode only
            link.knob_turn(int(round(amount)) or 1)
            ok = True
        elif action == "knob_press" and hasattr(link, "knob_press"):    # dev mode only
            link.knob_press()
            ok = True
        else:
            return self._reply(False, "Unknown action.")
        return self._reply(ok, None if ok else "The board did not answer.")

    def settings(self):
        return self.storage.settings()

    def update_settings(self, partial):
        """Merge a partial settings object; {"reset": true} goes back to the defaults."""
        partial = partial if isinstance(partial, dict) else {}
        if partial.get("reset") is True:
            settings, changed = self.storage.reset_settings()
        else:
            if isinstance(partial.get("baselines"), dict):
                known = set(self.book.ids())
                partial = dict(partial)
                partial["baselines"] = {k: v for k, v in partial["baselines"].items() if k in known}
            settings, changed = self.storage.update_settings(partial)
        if "flow_gps" in changed:
            self.board.call("set_param", "flow_gps", settings["flow_gps"])
        if "llm_enabled" in changed:
            self.intent.set_enabled(settings["llm_enabled"])
        return settings

    def calibrate(self, known_g=None):
        """Real scale only. known_g of 0 (or missing) zeroes the empty plate; a weight calibrates."""
        if self.board.virtual_scale() is not False:
            return {"ok": False, "factor": None, "message": "Calibration needs the real scale (HX711)."}
        with self._lock:
            busy = self._state in ACTIVE_STATES
        if busy:
            return {"ok": False, "factor": None, "message": "Finish or cancel the current run first."}
        grams = _number(known_g)
        if grams is None or grams <= 0:
            ok, result = self.board.call("tare")
            if ok and result:
                return {"ok": True, "factor": None,
                        "message": "Zeroed. Now put a known weight on the plate and calibrate."}
            return {"ok": False, "factor": None, "message": "The board did not answer."}
        ok, factor = self.board.call("calibrate", grams)
        factor = _number(factor) if ok else None
        if factor is None or abs(factor) < 0.01:
            return {"ok": False, "factor": None,
                    "message": "Calibration failed. Zero the empty plate first, then add the weight."}
        self.storage.set_device("cal", factor)
        return {"ok": True, "factor": round(factor, 4), "message": "Calibrated and saved."}

    def history(self):
        return {"runs": self.storage.history(20), "totals": self.storage.stats()}

    def recipes(self):
        return {"recipes": self.book.public(self.storage.settings())}

    def snapshot(self):
        """Everything the phone needs to draw the current view."""
        now = self._clock()
        status = self.board.status()
        caps = self.board.caps()
        online = self.board.online()
        with self._lock:
            recipe = self._recipe
            state = self._state
            countdown = None
            if state == "ready":
                countdown = max(1, min(int(COUNTDOWN_S), int(math.ceil(self._ready_until - now))))
            target = self._target_ml
            progress = 0.0
            if target:
                progress = max(0.0, min(1.0, self._dispensed_ml / float(target)))
            flow = {
                "state": state,
                "recipe": recipe["id"] if recipe else None,
                "portions": self._portions,
                "grams": self._grams,
                "ingredient_g": self._ingredient_g,
                "target_ml": target,
                "dispensed_ml": self._dispensed_ml,
                "countdown": countdown,
                "message": self._message,
                "error": self._error,
                # extras for the UI
                "hint": self._hint,
                "phase": self._phase,
                "name": recipe["name"] if recipe else None,
                "emoji": recipe["emoji"] if recipe else None,
                "vessel": recipe["vessel"] if recipe else None,
                "amount": self.book.amount_label(recipe, self._portions, self._grams) if recipe else None,
                "suggested_g": self._suggested_g() if state == "ingredient" else None,
                "min_g": INGREDIENT_MIN_G if state == "ingredient" else None,
                "can_next": (state in ("vessel", "ready")
                             or (state == "ingredient" and (self._ingredient_g or 0) >= INGREDIENT_MIN_G)),
                "progress": round(progress, 3),
            }
            last_run = dict(self._last_run) if self._last_run else None
        return {
            "app": {"name": APP_NAME, "version": APP_VERSION},
            "board": {
                "online": online,
                "virtual_scale": bool(self.board.virtual_scale()),
                "knob": caps["knob"],
                "buzzer": caps["buzzer"],
                "tilt": caps["tilt"],
                "fw": caps["fw"],
            },
            "scale": {
                "net_g": round(status["net_g"], 1) if status else 0.0,
                "stable": bool(status["stable"]) if status else False,
                "flow_gps": round(status["flow_gps"], 1) if status else 0.0,
            },
            "valve": {"open": bool(status["valve_open"]) if (status and online) else False},
            "flow": flow,
            "last_run": last_run,
            "stats": self.storage.stats(),
            "llm": self.intent.status(),
            "sim": self.board.debug_state(),     # dev mode only; null on the real board
        }
