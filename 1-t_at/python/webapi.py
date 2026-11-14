"""REST API between the board and the phone (all JSON, polled with fetch).

register(ui, machine) works with the App Lab WebUI brick and with the dev
server's shim: both offer expose_api(method, path, function). Handlers are
plain functions (FastAPI runs them in its thread pool) and return dicts.
"""

import functools
import logging

from fastapi import Body

log = logging.getLogger("webapi")


def _safe(handler):
    """A bug in one handler must never take the UI down: answer with JSON instead of a 500."""
    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except Exception:
            log.exception("API handler %s failed", handler.__name__)
            return {"ok": False, "message": "Something went wrong on the board. Please try again."}
    return wrapper


def register(ui, machine):
    """Register every route. Call at import time, before App.run()."""

    @_safe
    def get_state():
        return machine.snapshot()

    @_safe
    def get_recipes():
        return machine.recipes()

    @_safe
    def post_parse(payload: dict = Body(default={})):
        return machine.parse(payload.get("text"))

    @_safe
    def post_select(payload: dict = Body(default={})):
        return machine.select(payload.get("recipe"), payload.get("portions"), payload.get("grams"))

    @_safe
    def post_next():
        return machine.next()

    @_safe
    def post_cancel():
        return machine.cancel()

    @_safe
    def post_stop():
        return machine.stop()

    @_safe
    def post_sim(payload: dict = Body(default={})):
        return machine.sim(payload.get("action"), payload.get("grams"))

    @_safe
    def get_settings():
        return machine.settings()

    @_safe
    def post_settings(payload: dict = Body(default={})):
        return machine.update_settings(payload)

    @_safe
    def post_calibrate(payload: dict = Body(default={})):
        return machine.calibrate(payload.get("known_g"))

    @_safe
    def get_history():
        return machine.history()

    ui.expose_api("GET", "/api/state", get_state)
    ui.expose_api("GET", "/api/recipes", get_recipes)
    ui.expose_api("POST", "/api/parse", post_parse)
    ui.expose_api("POST", "/api/select", post_select)
    ui.expose_api("POST", "/api/next", post_next)
    ui.expose_api("POST", "/api/cancel", post_cancel)
    ui.expose_api("POST", "/api/stop", post_stop)
    ui.expose_api("POST", "/api/sim", post_sim)
    ui.expose_api("GET", "/api/settings", get_settings)
    ui.expose_api("POST", "/api/settings", post_settings)
    ui.expose_api("POST", "/api/calibrate", post_calibrate)
    ui.expose_api("GET", "/api/history", get_history)
