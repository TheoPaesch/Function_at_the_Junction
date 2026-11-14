"""Settings and run history on disk (JSON, atomic writes).

On the board the files live in /app/data. Nothing here may crash the app:
a broken file is set aside and replaced by defaults, a failed write is logged
and the values stay in memory.
"""

import copy
import json
import logging
import os
import tempfile
import threading
import time

log = logging.getLogger("storage")

SETTINGS_FILE = "settings.json"
HISTORY_FILE = "history.json"
HISTORY_MAX = 500

DEFAULT_SETTINGS = {
    # energy constants (editable in the UI)
    "t_start_c": 15.0,
    "eff_kettle": 0.85,
    "eff_stove": 0.75,
    "price_eur_per_kwh": 0.40,
    "kettle_kw": 2.0,
    "kettle_min_ml": 250,
    # behaviour
    "llm_enabled": True,
    "flow_gps": 30.0,          # virtual valve flow, sent to the MCU with set_param
    "baselines": {},           # recipe id -> baseline value override
    # learned by the hardware, sent back at boot
    "lead_s": 0.4,
    "cal": None,
}

# key -> (min, max) for everything numeric
LIMITS = {
    "t_start_c": (0.0, 60.0),
    "eff_kettle": (0.3, 1.0),
    "eff_stove": (0.2, 1.0),
    "price_eur_per_kwh": (0.0, 2.0),
    "kettle_kw": (0.5, 3.5),
    "kettle_min_ml": (0, 1500),
    "flow_gps": (0.0, 200.0),
    "lead_s": (0.0, 2.0),
}
BASELINE_LIMITS = (0.5, 30.0)
USER_KEYS = ("t_start_c", "eff_kettle", "eff_stove", "price_eur_per_kwh", "kettle_kw",
             "kettle_min_ml", "llm_enabled", "flow_gps", "baselines")
DEVICE_KEYS = ("lead_s", "cal")

EMPTY_TOTAL = {"runs": 0, "saved_ml": 0.0, "saved_wh": 0.0, "saved_eur": 0.0}

# Used when the data folder cannot be written (the app must still run).
FALLBACK_DIR = os.path.join(tempfile.gettempdir(), "water-assistant-data")


def atomic_write_json(path, obj):
    """Write to a temp file in the same folder, sync, then rename over the target."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(str(value).replace(",", ".")) if isinstance(value, str) else float(value)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def clean_settings(candidate, base=None, keys=None):
    """Merge candidate into base, keeping only known keys with sane values."""
    out = copy.deepcopy(base if base is not None else DEFAULT_SETTINGS)
    if not isinstance(candidate, dict):
        return out
    for key, value in candidate.items():
        if key not in DEFAULT_SETTINGS or (keys is not None and key not in keys):
            continue
        if key in LIMITS:
            number = _number(value)
            if number is None:
                continue
            low, high = LIMITS[key]
            number = max(low, min(high, number))
            out[key] = int(round(number)) if key == "kettle_min_ml" else number
        elif key == "llm_enabled":
            if isinstance(value, bool):
                out[key] = value
            elif value in (0, 1, "0", "1", "true", "false", "on", "off"):
                out[key] = value in (1, "1", "true", "on")
        elif key == "baselines":
            if not isinstance(value, dict):
                continue
            merged = dict(out.get("baselines") or {})
            for rid, raw in value.items():
                number = _number(raw)
                if raw is None or raw == "":
                    merged.pop(str(rid), None)      # empty means "back to recipes.json"
                elif number is not None:
                    merged[str(rid)] = max(BASELINE_LIMITS[0], min(BASELINE_LIMITS[1], number))
            out[key] = merged
        elif key == "cal":
            number = _number(value)
            out[key] = number if number is not None and abs(number) > 1e-6 else None
    return out


def _total_of(runs):
    total = dict(EMPTY_TOTAL)
    for run in runs:
        total["runs"] += 1
        total["saved_ml"] += float(run.get("saved_ml") or 0)
        total["saved_wh"] += float(run.get("saved_wh") or 0)
        total["saved_eur"] += float(run.get("saved_eur") or 0)
    return total


def _rounded(total):
    return {
        "runs": int(total["runs"]),
        "saved_ml": int(round(total["saved_ml"])),
        "saved_wh": round(total["saved_wh"], 1),
        "saved_eur": round(total["saved_eur"], 3),
    }


class Storage(object):
    """Thread-safe settings and history, persisted under data_dir."""

    def __init__(self, data_dir, wallclock=time.time):
        self._lock = threading.Lock()
        self._wallclock = wallclock
        self.data_dir = self._prepare_dir(data_dir)
        self._settings_path = os.path.join(self.data_dir, SETTINGS_FILE)
        self._history_path = os.path.join(self.data_dir, HISTORY_FILE)
        self._settings = clean_settings(self._read(self._settings_path))
        history = self._read(self._history_path)
        runs = history.get("runs") if isinstance(history, dict) else None
        self._runs = [r for r in runs if isinstance(r, dict)][-HISTORY_MAX:] if isinstance(runs, list) else []
        total = history.get("total") if isinstance(history, dict) else None
        if isinstance(total, dict) and all(_number(total.get(k)) is not None for k in EMPTY_TOTAL):
            self._total = {k: float(total[k]) for k in EMPTY_TOTAL}
        else:
            self._total = _total_of(self._runs)

    # ---- files -------------------------------------------------------------

    @staticmethod
    def _prepare_dir(data_dir):
        try:
            os.makedirs(data_dir, exist_ok=True)
            probe = os.path.join(data_dir, ".write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            return data_dir
        except OSError as exc:
            log.warning("cannot write to %s (%s), using %s", data_dir, exc, FALLBACK_DIR)
            os.makedirs(FALLBACK_DIR, exist_ok=True)
            return FALLBACK_DIR

    @staticmethod
    def _read(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("%s is unreadable (%s): starting fresh, old file kept as .bad", path, exc)
            try:
                os.replace(path, path + ".bad")
            except OSError:
                pass
            return None

    def _write(self, path, obj):
        try:
            atomic_write_json(path, obj)
            return True
        except OSError as exc:
            log.warning("could not write %s: %s", path, exc)
            return False

    # ---- settings ----------------------------------------------------------

    def settings(self):
        with self._lock:
            return copy.deepcopy(self._settings)

    def update_settings(self, partial):
        """Merge user-editable keys; returns (settings, changed_keys)."""
        with self._lock:
            before = self._settings
            after = clean_settings(partial, base=before, keys=USER_KEYS)
            changed = [k for k in after if after[k] != before.get(k)]
            if changed:
                self._settings = after
                self._write(self._settings_path, after)
            return copy.deepcopy(self._settings), changed

    def reset_settings(self):
        """Back to defaults, but keep what the hardware learned (lead time, calibration)."""
        with self._lock:
            fresh = copy.deepcopy(DEFAULT_SETTINGS)
            for key in DEVICE_KEYS:
                fresh[key] = self._settings.get(key)
            changed = [k for k in fresh if fresh[k] != self._settings.get(k)]
            self._settings = fresh
            self._write(self._settings_path, fresh)
            return copy.deepcopy(fresh), changed

    def set_device(self, key, value):
        """Store a value the hardware learned (lead_s, cal)."""
        if key not in DEVICE_KEYS:
            return
        with self._lock:
            after = clean_settings({key: value}, base=self._settings, keys=DEVICE_KEYS)
            if after != self._settings:
                self._settings = after
                self._write(self._settings_path, after)

    # ---- history -----------------------------------------------------------

    def today(self):
        return time.strftime("%Y-%m-%d", time.localtime(self._wallclock()))

    def add_run(self, run):
        """Append one finished run and update the all-time totals."""
        run = dict(run)
        run.setdefault("ts", int(self._wallclock()))
        run.setdefault("date", self.today())
        with self._lock:
            self._runs.append(run)
            del self._runs[:-HISTORY_MAX]
            self._total["runs"] += 1
            self._total["saved_ml"] += float(run.get("saved_ml") or 0)
            self._total["saved_wh"] += float(run.get("saved_wh") or 0)
            self._total["saved_eur"] += float(run.get("saved_eur") or 0)
            self._write(self._history_path, {"version": 1, "total": self._total, "runs": self._runs})
        return run

    def last_run(self):
        with self._lock:
            return dict(self._runs[-1]) if self._runs else None

    def history(self, limit=20):
        """Newest first."""
        with self._lock:
            return [dict(r) for r in reversed(self._runs[-int(limit):])]

    def stats(self):
        today = self.today()
        with self._lock:
            todays = _total_of(r for r in self._runs if r.get("date") == today)
            return {"today": _rounded(todays), "total": _rounded(self._total)}
