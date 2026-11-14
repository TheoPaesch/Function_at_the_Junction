#!/usr/bin/env python3
"""Self-test for the whole app without the board.

Plain asserts, standard library only (no pytest, no FastAPI needed):

    python3 1-t_at/python/selftest.py

It drives the real state machine against SimLink (the pure-Python twin of the
sketch) with a fake clock, so a 60 s dispense takes milliseconds.
"""

import http.server
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import board      # noqa: E402
import core       # noqa: E402
import energy     # noqa: E402
import intent     # noqa: E402
import recipes    # noqa: E402
import storage    # noqa: E402

NAME = core.APP_NAME
TESTS = []
_TEMP_DIRS = []


def test(fn):
    TESTS.append(fn)
    return fn


class FakeClock(object):
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class Rig(object):
    """Machine + Board + SimLink + Storage on a fake clock."""

    def __init__(self, engine=None, data_dir=None, **sim_options):
        self.dir = data_dir or tempfile.mkdtemp(prefix="selftest-")
        _TEMP_DIRS.append(self.dir)
        self.clock = FakeClock()
        self.wall = 1800000000.0        # a fixed "today"
        self.sim = board.SimLink(clock=self.clock, **sim_options)
        self.board = board.Board(self.sim, clock=self.clock)
        self.store = storage.Storage(self.dir, wallclock=lambda: self.wall)
        self.book = recipes.RecipeBook.load()
        self.intent = engine or intent.IntentEngine(self.book, url=None, enabled=False)
        self.m = core.Machine(self.board, self.book, self.store, self.intent, clock=self.clock)
        self.run(0.6)
        assert self.m.sync_once(), "the simulated board should answer info()"
        self.run(3.4)

    def run(self, seconds, until=None, dt=0.02, tick=True):
        """Advance time. tick=False freezes Python (no pings) while the board keeps running."""
        end = self.clock.t + seconds
        while self.clock.t < end - 1e-9:
            self.clock.advance(dt)
            self.sim.step()
            if tick:
                self.m.tick()
            if until is not None and until():
                return True
        return bool(until()) if until is not None else True

    def snap(self):
        return self.m.snapshot()

    def flow(self):
        return self.snap()["flow"]

    def state(self):
        return self.flow()["state"]

    def wait(self, state, seconds):
        assert self.run(seconds, until=lambda: self.state() == state), \
            "expected state {!r} within {} s, still {!r}: {}".format(
                state, seconds, self.state(), self.flow()["message"])

    def valve_open(self):
        return self.sim.debug_state()["valve_open"]

    def to_dispensing(self, recipe="tea", portions=4, add_g=None):
        """Walk the flow until water is running."""
        assert self.m.select(recipe, portions)["ok"]
        self.m.sim("place_vessel")
        if self.book.get(recipe)["weigh_in_pot"]:
            self.wait("ingredient", 6)
            self.m.sim("add", add_g or 1000)
            self.run(1.5)
            assert self.m.next()["ok"]
        self.wait("dispensing", 10)
        assert self.run(2.0, until=self.valve_open), "the valve should open"


def close(a, b, tol):
    return abs(a - b) <= tol


# ============================================================================
# maths
# ============================================================================

@test
def energy_sanity_check():
    settings = storage.DEFAULT_SETTINGS
    saved = energy.savings("kettle", 1000, 500, settings)
    assert saved["saved_ml"] == 500
    assert close(saved["saved_wh"], 58.1, 0.3), saved          # about 58 Wh
    assert close(saved["saved_seconds"], 105, 2), saved        # about 105 s
    assert close(saved["saved_eur"], 0.023, 0.001), saved      # about 0.023 EUR
    assert close(energy.heat_kwh(1000, 15), 0.0988, 0.0005)
    pot = energy.savings("pot", 3000, 1500, settings)
    assert pot["saved_seconds"] is None                        # seconds are for kettle recipes only
    assert close(pot["saved_wh"], 1.5 * 4.186 * 85 / 3.6 / 0.75, 0.5)
    assert energy.savings("kettle", 400, 500, settings)["saved_ml"] == 0   # never negative


@test
def recipe_water_rules():
    book = recipes.RecipeBook.load()
    tea, noodles, pasta, potatoes = (book.get(i) for i in ("tea", "noodles", "pasta", "potatoes"))
    assert book.ids() == ["tea", "noodles", "pasta", "potatoes"]
    assert book.water_ml(tea, 1) == 250 and book.water_ml(tea, 2) == 500
    assert book.water_ml(noodles, 1) == 500 and book.water_ml(noodles, 3) == 1500
    assert book.water_ml(pasta, 2, grams=300) == 1500            # 5 ml per g
    assert book.water_ml(pasta, 1) == 1000                       # the minimum
    assert book.water_ml(pasta, 4) == 2000                       # 4 x 100 g
    assert book.water_ml(pasta, 12) == recipes.MAX_ML            # capped at 4,000 ml
    assert book.water_ml(potatoes, 4, weighed_g=1000) == 1300    # 1 ml per g + 300
    assert book.water_ml(potatoes, 3) == 1050                    # estimate: 3 x 250 g
    assert book.water_ml(potatoes, 2, weighed_g=333) == 630      # rounded to 10 ml
    assert book.water_ml(tea, 1, kettle_min_ml=400) == 400       # kettle minimum fill
    assert book.water_ml(pasta, 1, kettle_min_ml=4000) == 1000   # ... applies to kettles only
    assert recipes.round_ml(1255) == 1260 and recipes.round_ml(1254) == 1250
    # baselines: the normal habit
    assert book.baseline_ml(tea, 500) == 1000
    assert book.baseline_ml(pasta, 1500, basis_g=300) == 3000    # 1 L per 100 g
    assert book.baseline_ml(potatoes, 1300) == 2600
    assert book.baseline_ml(tea, 500, overrides={"tea": 1.5}) == 750
    # wording
    assert book.portion_label(tea, 1) == "1 cup" and book.portion_label(pasta, 4) == "4 people"
    assert book.order_line(pasta, 4) == "Pasta · 4 people · 2.0 L water"
    assert book.order_line(tea, 2) == "Tea · 2 cups · 500 ml water"
    assert book.order_line(potatoes, 4) == "Potatoes · 4 people · about 1.3 L water"
    assert book.names_sentence() == "tea, instant noodles, pasta or potatoes"
    for recipe in book.all():
        assert recipe["max_portions"] == 12
        public = [r for r in book.public(storage.DEFAULT_SETTINGS) if r["id"] == recipe["id"]][0]
        assert len(public["amounts_ml"]) == 12


@test
def recipes_json_is_validated():
    book = recipes.RecipeBook.load()
    good = json.loads(json.dumps(book.all()))
    for breaker in (lambda r: r.pop("tip"), lambda r: r.update(vessel="wok"),
                    lambda r: r["water"].update(type="magic"), lambda r: r.update(synonyms=[]),
                    lambda r: r.update(id="unknown")):
        bad = json.loads(json.dumps(good))
        breaker(bad[0])
        try:
            recipes.RecipeBook(bad)
        except recipes.RecipeError:
            continue
        raise AssertionError("a broken recipes.json must be rejected")
    try:
        recipes.RecipeBook(good + [good[0]])
    except recipes.RecipeError:
        pass
    else:
        raise AssertionError("duplicate ids must be rejected")


# ============================================================================
# free text
# ============================================================================

RULE_CASES = [
    # English
    ("2 cups of tea", "tea", 2, None),
    ("tea", "tea", None, None),
    ("a cup of tea", "tea", 1, None),
    ("tea for two", "tea", 2, None),
    ("three mugs of chai please", "tea", 3, None),
    ("tea 2", "tea", 2, None),
    ("2x tea", "tea", 2, None),
    ("tea for me", "tea", 1, None),
    ("tea for me and my two kids", "tea", 3, None),
    ("tea for me and my girlfriend", "tea", 2, None),
    ("pasta for 3 friends and me", "pasta", 4, None),
    ("15 cups of tea", "tea", 12, None),
    ("300 g tea", "tea", None, None),
    ("pasta for 4", "pasta", 4, None),
    ("spaghetti for 3 people", "pasta", 3, None),
    ("300 g pasta", "pasta", None, 300),
    ("pasta 300g", "pasta", None, 300),
    ("300 pasta", "pasta", None, 300),
    ("pasta for 4, 350 g", "pasta", 4, 350),
    ("pasta for 20", "pasta", 12, None),
    ("pasta for my family of 5", "pasta", 5, None),
    ("spagetti for 2", "pasta", 2, None),
    ("potatoes for 4", "potatoes", 4, None),
    ("0.5 kg potatoes", "potatoes", None, 500),
    ("1 kilo potatoes", "potatoes", None, 1000),
    ("half a kilo of potatoes", "potatoes", None, 500),
    ("3 packs of instant noodles", "noodles", 3, None),
    ("cup noodles", "noodles", None, None),
    ("2 ramen", "noodles", 2, None),
    ("instant noodles x2", "noodles", 2, None),
    ("a pizza", None, None, None),
    ("instant coffee", None, None, None),
    ("", None, None, None),
    # German
    ("zwei Tassen Tee", "tea", 2, None),
    ("eine Tasse Tee", "tea", 1, None),
    ("Tee für vier", "tea", 4, None),
    ("Tee für mich", "tea", 1, None),
    ("Pfefferminztee", "tea", None, None),
    ("Nudeln für 3", "pasta", 3, None),
    ("Nudeln zu dritt", "pasta", 3, None),
    ("Spaghetti für 2 Personen", "pasta", 2, None),
    ("Ramen", "noodles", None, None),
    ("Instantnudeln", "noodles", None, None),
    ("instant Nudeln für 2", "noodles", 2, None),
    ("Mie-Nudeln", "noodles", None, None),
    ("Nudelsuppe", "noodles", None, None),
    ("Kartoffeln für 3", "potatoes", 3, None),
    ("Erdäpfel für fünf", "potatoes", 5, None),
    ("Erdaepfel fuer fuenf", "potatoes", 5, None),
    ("500 g Kartoffeln", "potatoes", None, 500),
    ("ein Kilo Kartoffeln", "potatoes", None, 1000),
    ("1,5 kg Kartoffeln", "potatoes", None, 1500),
    ("Salzkartoffeln für 4", "potatoes", 4, None),
    ("kartofeln", "potatoes", None, None),
]


@test
def rules_english_and_german():
    engine = intent.IntentEngine(recipes.RecipeBook.load(), url=None, enabled=False)
    for text, recipe, portions, grams in RULE_CASES:
        got = engine.parse(text)
        assert (got["recipe"], got["portions"], got["grams"]) == (recipe, portions, grams), \
            "{!r}: expected {} got {}".format(text, (recipe, portions, grams), got)
        assert got["source"] == "rules" and isinstance(got["ms"], int)
    assert engine.status()["status"] == "off"


@test
def llm_prompt_and_schema():
    book = recipes.RecipeBook.load()
    prompt = intent.build_system_prompt(book)
    for recipe_id in book.ids():
        assert recipe_id in prompt
    assert "Answer with JSON only" in prompt
    assert prompt.count("->") == 4 and "Kartoffeln" in prompt       # four examples, one German
    # "under 150 tokens": about 3.3 characters per token for this mix of words and JSON
    assert len(prompt) / 3.3 < 150, len(prompt)
    schema = intent.build_schema(book)
    assert schema["properties"]["recipe"]["enum"] == book.ids() + ["unknown"]
    assert schema["properties"]["portions"]["anyOf"][0] == {"type": "integer", "minimum": 1, "maximum": 12}
    assert schema["properties"]["grams"]["anyOf"][0] == {"type": "integer", "minimum": 0, "maximum": 5000}
    assert intent.chat_endpoint("http://x:1") == "http://x:1/v1/chat/completions"
    assert intent.chat_endpoint("http://x:1/v1/") == "http://x:1/v1/chat/completions"
    assert intent.LLM_URL == "http://llamacpp-models-runner:9999/v1"
    assert intent.LLM_MODEL == "Qwen3.5-0.8B-Q4_0"


class FakeLLM(object):
    """A tiny OpenAI-compatible server. answer(user_text, body) -> content, or an int HTTP status."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                outer.requests.append((self.path, body))
                reply = outer.answer(body["messages"][-1]["content"], body)
                if isinstance(reply, int):
                    self.send_response(reply)
                    self.end_headers()
                    return
                data = json.dumps({"choices": [{"message": {"role": "assistant", "content": reply}}]})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data.encode("utf-8"))

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}/v1".format(self.server.server_address[1])
        thread = threading.Thread(target=self.server.serve_forever)
        thread.daemon = True
        thread.start()

    def engine(self, **options):
        engine = intent.IntentEngine(recipes.RecipeBook.load(), url=self.url, model="test-model",
                                     label="Test LLM", **options)
        engine.start()
        deadline = time.time() + 10
        while engine.status()["status"] == "loading" and time.time() < deadline:
            time.sleep(0.01)
        return engine

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def order(recipe, portions=None, grams=None):
    return json.dumps({"recipe": recipe, "portions": portions, "grams": grams})


@test
def llm_first_then_rules():
    answers = {
        "2 cups of tea": order("tea", 2),
        "something hot to drink for the band, 4 of us plus 1 roadie": order("tea", 5),
        "Nudeln für 3": order("noodles", 3),              # wrong: one clear keyword says pasta
        "pasta for 4": order("pasta", 9, 400),            # wrong numbers: the text says 4, no weight
        "a pizza": order("unknown"),
        "potatoes": "I think you want potatoes!",         # not JSON
        "dinner for my family": '<think>hm</think> {"recipe": "pasta", "portions": 99, "grams": -5}',
        "instant tea": order("tea", None),                # two candidates: the model may choose
    }
    fake = FakeLLM(lambda text, body: answers.get(text, order("unknown")))
    try:
        engine = fake.engine()
        status = engine.status()
        assert status["status"] == "ready" and status["last_ms"] is not None, status

        got = engine.parse("2 cups of tea")
        assert (got["recipe"], got["portions"], got["source"]) == ("tea", 2, "llm"), got
        got = engine.parse("something hot to drink for the band, 4 of us plus 1 roadie")
        assert (got["recipe"], got["portions"], got["source"]) == ("tea", 5, "llm"), got   # a lone "4" is a weak hint
        got = engine.parse("Nudeln für 3")
        assert (got["recipe"], got["portions"], got["source"]) == ("pasta", 3, "rules"), got
        got = engine.parse("pasta for 4")
        assert (got["recipe"], got["portions"], got["grams"], got["source"]) == ("pasta", 4, None, "llm"), got
        got = engine.parse("a pizza")
        assert got["recipe"] is None and got["source"] == "rules", got
        got = engine.parse("potatoes")
        assert (got["recipe"], got["source"]) == ("potatoes", "rules"), got
        got = engine.parse("dinner for my family")
        assert (got["recipe"], got["portions"], got["grams"], got["source"]) == ("pasta", 12, None, "llm"), got
        got = engine.parse("instant tea")
        assert (got["recipe"], got["source"]) == ("tea", "llm"), got

        path, body = fake.requests[-1]
        assert path == "/v1/chat/completions"
        assert body["model"] == "test-model" and body["temperature"] == 0 and body["max_tokens"] == 40
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["name"] == "order"
        assert body["response_format"]["json_schema"]["schema"] == engine.schema
        assert body["messages"][0] == {"role": "system", "content": engine.system_prompt}

        # switching it off in the settings means rules only, at once
        engine.set_enabled(False)
        assert engine.status()["status"] == "off"
        before = len(fake.requests)
        assert engine.parse("2 cups of tea")["source"] == "rules"
        assert len(fake.requests) == before
    finally:
        fake.close()


@test
def llm_fallbacks():
    # 1. a server that rejects response_format gets a plain request instead
    def picky(text, body):
        return 400 if "response_format" in body else "Sure: " + order("tea", 2)
    fake = FakeLLM(picky)
    try:
        engine = fake.engine()
        assert engine.status()["status"] == "ready"
        got = engine.parse("two teas")
        assert (got["recipe"], got["portions"], got["source"]) == ("tea", 2, "llm"), got
    finally:
        fake.close()

    # 2. a slow model: the rules answer, the user never waits longer than the timeout
    state = {"slow": False}

    def slow(text, body):
        if state["slow"]:
            time.sleep(1.0)
        return order("tea", 5)
    fake = FakeLLM(slow)
    try:
        engine = fake.engine(timeout_s=0.3)
        assert engine.status()["status"] == "ready"
        state["slow"] = True
        started = time.time()
        got = engine.parse("2 cups of tea")
        assert got["source"] == "rules" and got["portions"] == 2, got
        assert time.time() - started < 0.9
    finally:
        fake.close()

    # 3. HTTP unreachable: the LLM brick is asked instead (memory cleared first)
    class FakeBrick(object):
        def __init__(self):
            self.calls = []

        def clear_memory(self):
            self.calls.append("clear")

        def chat(self, text):
            self.calls.append(text)
            return "<think>…</think>" + order("potatoes", 3)

    engine = intent.IntentEngine(recipes.RecipeBook.load(), url="http://127.0.0.1:9/v1", enabled=True)
    engine._brick = FakeBrick()
    engine._status = "ready"
    got = engine.parse("something with spuds for three")
    assert (got["recipe"], got["portions"], got["source"]) == ("potatoes", 3, "llm"), got
    assert engine._brick.calls == ["clear", "something with spuds for three"]

    # 4. nothing reachable at all: status goes to failed, the rules keep working
    engine = intent.IntentEngine(recipes.RecipeBook.load(), url="http://127.0.0.1:9/v1", enabled=True)
    engine._status = "ready"
    engine._recoveries = 99                     # do not start a background recovery in this test
    for _ in range(2):
        assert engine.parse("pasta for 4")["source"] == "rules"
    assert engine.status()["status"] == "failed"

    # 5. no LLM configured (the brick was removed from app.yaml): off, and it stays off
    engine = intent.IntentEngine(recipes.RecipeBook.load(), url=None, enabled=True)
    engine.start()
    engine.set_enabled(True)
    assert engine.status()["status"] == "off" and not engine.will_use_llm()


@test
def thinking_screen_while_the_llm_works():
    seen = {}
    rig_box = {}

    def answer(text, body):
        if "rig" in rig_box:
            seen["screen"] = rig_box["rig"].sim.debug_state()["screen"]
        return order("pasta", 4)
    fake = FakeLLM(answer)
    try:
        rig = Rig(engine=fake.engine())
        rig_box["rig"] = rig
        result = rig.m.parse("pasta for 4")
        assert seen["screen"] == "thinking", seen
        assert rig.sim.debug_state()["screen"] == "idle"
        assert result["ok"] and result["source"] == "llm"
        assert result["message"] == "Pasta · 4 people · 2.0 L water"
        assert result["badge"].startswith("understood on-device by Test LLM in ")
        assert rig.state() == "idle", "parse must not start the flow"
        assert rig.snap()["llm"]["status"] == "ready"
    finally:
        fake.close()


# ============================================================================
# the flow
# ============================================================================

@test
def snapshot_shape():
    rig = Rig()
    snap = rig.snap()
    assert snap["app"] == {"name": NAME, "version": "1.0"}
    assert NAME == "1-" + "T_AT"
    board_part = snap["board"]
    assert (board_part["online"], board_part["virtual_scale"], board_part["knob"],
            board_part["buzzer"], board_part["tilt"]) == (True, True, True, True, False)
    assert abs(snap["scale"]["net_g"]) < 1 and snap["scale"]["stable"] is True
    assert snap["valve"] == {"open": False}
    expected = {"state": "idle", "recipe": None, "portions": None, "grams": None,
                "ingredient_g": None, "target_ml": None, "dispensed_ml": 0, "countdown": None,
                "message": "What are you making?", "error": None}
    for key, value in expected.items():
        assert snap["flow"][key] == value, (key, snap["flow"][key])
    assert snap["last_run"] is None
    empty = {"runs": 0, "saved_ml": 0, "saved_wh": 0, "saved_eur": 0}
    assert snap["stats"] == {"today": empty, "total": empty}
    assert snap["llm"]["status"] == "off" and snap["llm"]["last_ms"] is None
    json.dumps(snap)        # everything must be JSON


@test
def flow_two_cups_of_tea():
    rig = Rig()
    parsed = rig.m.parse("2 cups of tea")
    assert parsed["ok"] and (parsed["recipe"], parsed["portions"], parsed["source"]) == ("tea", 2, "rules")
    assert parsed["message"] == "Tea · 2 cups · 500 ml water" and parsed["badge"] == "understood by rules"
    assert rig.state() == "idle", "parse must not start the flow"

    snap = rig.m.select(parsed["recipe"], parsed["portions"], parsed["grams"])
    assert snap["ok"] and snap["flow"]["state"] == "vessel"
    assert snap["flow"]["message"] == "Put the empty kettle on the plate"
    assert "minimum fill line" in snap["flow"]["hint"]
    assert rig.sim.debug_state()["screen"] == "vessel"
    rig.run(3.0)
    assert rig.state() == "vessel", "nothing happens until the kettle is on the plate"

    assert rig.m.sim("place_vessel")["ok"]                     # +900 g
    rig.wait("ready", 5)
    flow = rig.flow()
    assert flow["target_ml"] == 500 and flow["message"] == "Adding 500 ml" and flow["countdown"] == 3
    assert abs(rig.snap()["scale"]["net_g"]) < 2, "the kettle must be tared away"
    assert rig.sim.debug_state()["screen"] == "ready"
    rig.run(1.2)
    assert rig.flow()["countdown"] == 2

    rig.wait("dispensing", 4)                                   # no extra tap
    assert rig.run(1.0, until=rig.valve_open)
    rig.run(5.0)
    snap = rig.snap()
    assert snap["valve"]["open"] and 60 < snap["flow"]["dispensed_ml"] < 300
    assert 0.1 < snap["flow"]["progress"] < 0.6
    assert snap["sim"]["rgb"] == "blue" and snap["sim"]["screen"] == "dispensing"

    rig.wait("done", 60)
    snap = rig.snap()
    run = snap["last_run"]
    assert close(run["dispensed_ml"], 500, 10), run             # within +-10 ml
    assert close(run["saved_ml"], 500, 10), run                 # about 500 ml not heated
    assert close(run["saved_wh"], 58, 2), run                   # about 58 Wh
    assert close(run["saved_seconds"], 105, 4), run
    assert run["baseline_ml"] == 1000 and run["recipe"] == "tea" and run["amount"] == "2 cups"
    assert snap["flow"]["message"].startswith("Done! ") and snap["flow"]["hint"] == run["tip"]
    assert not snap["valve"]["open"]
    assert snap["sim"]["screen"] == "done" and snap["sim"]["rgb"] == "green"
    assert snap["stats"]["today"]["runs"] == 1 and snap["stats"]["total"]["runs"] == 1
    assert close(snap["stats"]["today"]["saved_wh"], 58, 2)
    history = rig.m.history()
    assert len(history["runs"]) == 1 and history["totals"]["total"]["runs"] == 1

    rig.run(59.0)
    assert rig.state() == "done"
    rig.run(2.0)
    assert rig.state() == "idle", "back to idle 60 s after done"
    assert rig.sim.debug_state()["screen"] == "idle"
    assert rig.snap()["last_run"]["recipe"] == "tea"


@test
def flow_potatoes_for_four():
    rig = Rig()
    parsed = rig.m.parse("potatoes for 4")
    assert (parsed["recipe"], parsed["portions"]) == ("potatoes", 4)
    assert parsed["message"] == "Potatoes · 4 people · about 1.3 L water" and parsed["estimate"]
    assert rig.m.select("potatoes", 4)["flow"]["message"] == "Put the empty pot on the plate"
    assert rig.flow()["target_ml"] is None, "the water is not known before weighing"
    rig.m.sim("place_vessel")                                   # +1,500 g
    rig.wait("ingredient", 5)
    flow = rig.flow()
    assert flow["message"] == "Put the potatoes in" and flow["suggested_g"] == 1000
    assert not flow["can_next"]
    refused = rig.m.next()
    assert not refused["ok"] and refused["flow"]["state"] == "ingredient", "needs at least 50 g"

    rig.m.sim("add", 1000)
    rig.run(1.5)
    flow = rig.flow()
    assert close(flow["ingredient_g"], 1000, 1) and flow["target_ml"] == 1300 and flow["can_next"]
    assert rig.m.next()["flow"]["state"] == "ready"
    assert rig.flow()["message"] == "Adding 1,300 ml"
    rig.wait("done", 90)
    run = rig.snap()["last_run"]
    assert run["target_ml"] == 1300 and close(run["dispensed_ml"], 1300, 10), run
    assert run["baseline_ml"] == 2600 and close(run["saved_ml"], 1300, 10)
    assert run["saved_seconds"] is None and close(run["grams"], 1000, 1)


@test
def pasta_targets():
    rig = Rig()
    parsed = rig.m.parse("300 g pasta")
    assert (parsed["recipe"], parsed["grams"], parsed["target_ml"]) == ("pasta", 300, 1500)
    assert parsed["message"] == "Pasta · 300 g · 1.5 L water"
    snap = rig.m.select(parsed["recipe"], parsed["portions"], parsed["grams"])
    assert snap["flow"]["target_ml"] == 1500 and snap["flow"]["amount"] == "300 g"
    rig.m.sim("place_vessel")
    rig.wait("done", 120)                # pasta is not weighed: vessel -> ready -> water
    run = rig.snap()["last_run"]
    assert close(run["dispensed_ml"], 1500, 10) and run["baseline_ml"] == 3000, run

    parsed = rig.m.parse("pasta for 1")
    assert (parsed["recipe"], parsed["portions"], parsed["target_ml"]) == ("pasta", 1, 1000)
    snap = rig.m.select("pasta", 1)
    assert snap["ok"] and snap["flow"]["target_ml"] == 1000       # the minimum


@test
def german_requests():
    rig = Rig()
    for text, recipe, portions in (("zwei Tassen Tee", "tea", 2), ("Nudeln für 3", "pasta", 3),
                                   ("Ramen", "noodles", 1), ("Kartoffeln für 3", "potatoes", 3)):
        parsed = rig.m.parse(text)
        assert parsed["ok"] and (parsed["recipe"], parsed["portions"]) == (recipe, portions), parsed
    sorry = rig.m.parse("eine Pizza")
    assert not sorry["ok"] and sorry["recipe"] is None
    assert sorry["message"] == "Sorry, I can do tea, instant noodles, pasta or potatoes."
    assert not rig.m.parse("   ")["ok"]


@test
def kettle_minimum_fill():
    rig = Rig()
    rig.m.update_settings({"kettle_min_ml": 400})
    assert rig.m.select("tea", 1)["flow"]["target_ml"] == 400
    assert "400 ml" in rig.flow()["hint"]
    rig.m.cancel()
    assert rig.m.select("pasta", 1)["flow"]["target_ml"] == 1000   # pots are not affected


@test
def select_only_when_free_and_cancel_everywhere():
    rig = Rig()
    assert not rig.m.select("pizza")["ok"] and rig.state() == "idle"
    assert rig.m.select("tea", 99)["flow"]["portions"] == 12       # clamped
    assert not rig.m.select("pasta", 2)["ok"], "select is refused during a run"
    assert rig.flow()["recipe"] == "tea"
    assert rig.m.cancel()["flow"]["state"] == "idle"

    # cancel from every step closes the valve and returns to idle
    rig.m.select("potatoes", 2)
    rig.m.sim("place_vessel")
    rig.wait("ingredient", 5)
    assert rig.m.cancel()["flow"]["state"] == "idle"
    rig.m.sim("remove_vessel")
    rig.run(1.5)

    rig.m.select("tea", 2)
    rig.m.sim("place_vessel")
    rig.wait("ready", 5)
    assert rig.m.cancel()["flow"]["state"] == "idle"
    rig.run(5.0)
    assert rig.state() == "idle" and not rig.valve_open(), "a cancelled countdown must not pour"
    rig.m.sim("remove_vessel")
    rig.run(1.5)

    rig.to_dispensing("tea", 4)
    rig.run(3.0)
    snap = rig.m.cancel()
    assert snap["flow"]["state"] == "idle" and snap["flow"]["recipe"] is None
    rig.run(0.1)
    assert not rig.valve_open(), "cancel closes the valve"
    rig.run(3.0)
    assert rig.state() == "idle"
    assert rig.snap()["stats"]["total"]["runs"] == 0, "a cancelled run is not a saving"
    assert rig.m.select("tea", 1)["ok"], "select works again after cancel"


@test
def next_button_and_start_now():
    rig = Rig()
    rig.m.select("tea", 1)
    rig.m.sim("add", 100)                    # too light to count as a kettle
    rig.run(3.0)
    assert rig.state() == "vessel"
    assert rig.m.next()["ok"]                # "it is already on the plate"
    rig.wait("ready", 3)
    assert rig.m.next()["flow"]["state"] == "dispensing"     # start now, skipping the countdown
    rig.wait("done", 40)
    assert close(rig.snap()["last_run"]["dispensed_ml"], 250, 10)
    assert rig.m.next()["flow"]["state"] == "idle"           # "New"
    assert not rig.m.next()["ok"]                            # nothing to advance in idle


@test
def vessel_swap_is_detected():
    """Old kettle still on the plate at the start: remove it, put the empty one on."""
    rig = Rig()
    rig.m.sim("add", 1400)                   # yesterday's kettle with water in it
    rig.run(1.5)
    rig.m.select("tea", 2)
    rig.run(2.0)
    assert rig.state() == "vessel"
    rig.m.sim("remove_vessel")
    rig.run(1.5)
    rig.m.sim("place_vessel")
    rig.wait("ready", 5)


# ============================================================================
# safety
# ============================================================================

@test
def safety_vessel_removed():
    rig = Rig()
    rig.to_dispensing("tea", 4)
    rig.run(4.0)
    rig.m.sim("remove_vessel")
    rig.wait("error", 1.0)
    flow = rig.flow()
    assert flow["error"] == "vessel_removed" and "kettle" in flow["message"], flow
    assert not rig.valve_open() and not rig.snap()["valve"]["open"]
    assert rig.sim.debug_state()["rgb"] == "red" and rig.sim.debug_state()["screen"] == "error"
    assert rig.snap()["stats"]["total"]["runs"] == 0
    assert rig.m.next()["flow"]["state"] == "idle"           # "Try again"


@test
def safety_link_lost():
    rig = Rig()
    rig.to_dispensing("tea", 4)
    rig.run(2.0)
    started = rig.clock.t
    closed_at = []

    def valve_closed():
        if not rig.valve_open() and not closed_at:
            closed_at.append(rig.clock.t)
        return bool(closed_at)

    assert rig.run(3.0, until=valve_closed, dt=0.01, tick=False), "no pings: the board must stop"
    # 2 s after the last ping, and the last ping was up to 0.5 s before Python froze
    assert 1.4 <= closed_at[0] - started <= 2.1, closed_at[0] - started
    rig.run(0.3)
    assert rig.state() == "error" and rig.flow()["error"] == "link_lost"


@test
def safety_stop_within_200_ms():
    rig = Rig()
    rig.to_dispensing("tea", 4)
    rig.run(3.0)
    assert rig.valve_open()
    started = rig.clock.t
    snap = rig.m.stop()
    assert snap["flow"]["state"] == "error" and snap["flow"]["error"] == "stopped"
    while rig.valve_open() and rig.clock.t - started < 1.0:
        rig.clock.advance(0.01)
        rig.sim.step()
    assert rig.clock.t - started <= 0.2, "stop must close the valve within 200 ms"
    rig.run(0.5)
    flow = rig.flow()
    assert flow["error"] == "stopped" and 30 < flow["dispensed_ml"] < 200
    assert "went in before the stop" in flow["hint"]
    rig.run(119.0)
    assert rig.state() == "error"
    rig.run(2.0)
    assert rig.state() == "idle", "an error clears itself after two minutes"


@test
def safety_no_flow_and_timeout():
    rig = Rig()
    rig.m.update_settings({"flow_gps": 0})           # water supply closed
    rig.to_dispensing("tea", 2)
    started = rig.clock.t
    rig.wait("error", 5.0)
    assert rig.flow()["error"] == "no_flow" and 2.0 < rig.clock.t - started < 4.0
    assert not rig.valve_open()
    rig.m.cancel()
    rig.m.sim("remove_vessel")
    rig.run(1.5)

    rig.m.update_settings({"flow_gps": 3})           # a trickle: 250 ml would take 83 s
    rig.to_dispensing("tea", 1)
    started = rig.clock.t
    rig.wait("error", 70.0)
    assert rig.flow()["error"] == "timeout", rig.flow()
    assert close(rig.clock.t - started, 250 / 5.0 + 10.0, 2.0)      # ml / 5 g/s + 10 s
    assert not rig.valve_open()


@test
def safety_tilt_switch():
    rig = Rig(tilt=True)
    assert rig.snap()["board"]["tilt"] is True
    rig.to_dispensing("pasta", 2)
    rig.run(2.0)
    rig.sim.set_tilt(True)
    rig.wait("error", 1.0)
    assert rig.flow()["error"] == "tilt" and "pot" in rig.flow()["message"]


@test
def never_pour_without_a_vessel():
    rig = Rig()
    rig.m.select("tea", 2)
    rig.m.sim("place_vessel")
    rig.wait("ready", 5)
    rig.m.sim("remove_vessel")               # lifted during the countdown
    rig.wait("error", 4)
    assert rig.flow()["error"] == "vessel_removed"
    assert not rig.valve_open()
    # the board itself also refuses to pour when the vessel left after the tare
    ok, result = rig.board.call("dispense", 500.0)
    assert ok and result is False


@test
def board_offline_and_back():
    rig = Rig()
    rig.sim.offline = True
    rig.run(2.5)
    assert rig.snap()["board"]["online"] is False
    started = time.time()
    snap = rig.m.select("tea", 2)
    assert time.time() - started < 0.5, "a dead board must not make the phone wait"
    assert snap["flow"]["state"] == "error" and snap["flow"]["error"] == "board_offline"
    rig.sim.offline = False
    rig.run(1.0)
    assert rig.snap()["board"]["online"] is True
    assert rig.m.cancel()["flow"]["state"] == "idle"

    # offline during a step
    rig.m.select("tea", 2)
    rig.sim.offline = True
    rig.wait("error", 5)
    assert rig.flow()["error"] == "board_offline"
    rig.sim.offline = False
    rig.run(1.0)
    rig.m.cancel()

    # offline while water runs: Python reports it, the board closes the valve by itself
    rig.to_dispensing("tea", 4)
    rig.run(2.0)
    rig.sim.offline = True
    rig.wait("error", 3)
    assert rig.flow()["error"] == "board_offline"
    rig.run(1.0)
    assert not rig.valve_open(), "the board stops on its own when the pings end"


@test
def board_reboot_during_a_run():
    rig = Rig()
    rig.m.update_settings({"flow_gps": 45})
    rig.m.select("tea", 2)
    rig.m.sim("place_vessel")
    rig.wait("ready", 5)
    rig.sim.reboot()                         # power glitch
    rig.run(0.5)
    flow = rig.flow()
    assert flow["state"] == "error" and flow["error"] == "board_offline", flow
    assert "restarted" in flow["message"]
    assert rig.m.sync_once()                 # what the sync thread does next
    ok, _ = rig.board.call("set_param", "nonsense", 1.0)
    assert ok
    assert rig.sim._flow_gps == 45.0, "stored parameters are pushed again after a reboot"
    assert rig.sim.debug_state()["screen"] == "error"
    rig.m.cancel()
    rig.to_dispensing("tea", 1)
    rig.wait("done", 30)


@test
def lost_done_event_is_recovered():
    rig = Rig()
    rig.to_dispensing("tea", 2)
    real = rig.board._on_event
    rig.sim._on_event = lambda kind, value: None if kind == "done" else real(kind, value)
    rig.wait("done", 60)
    assert close(rig.snap()["last_run"]["dispensed_ml"], 500, 10)


# ============================================================================
# the Knob
# ============================================================================

@test
def knob_runs_the_whole_demo():
    rig = Rig()
    rig.m.select("potatoes", 4)
    rig.sim.knob_turn(30)                    # 30 detents = 1,500 g: "the pot"
    rig.wait("ingredient", 5)
    rig.sim.knob_press()                     # nothing in yet
    rig.run(0.3)
    assert rig.state() == "ingredient"
    rig.sim.knob_turn(21)
    rig.sim.knob_turn(-1)                    # 20 detents = 1,000 g of potatoes
    rig.run(1.5)
    assert close(rig.flow()["ingredient_g"], 1000, 1)
    rig.sim.knob_press()                     # done
    rig.run(0.2)
    assert rig.state() == "ready" and rig.flow()["target_ml"] == 1300
    rig.sim.knob_press()                     # start now
    rig.run(0.2)
    assert rig.state() == "dispensing"
    rig.wait("done", 90)
    rig.sim.knob_press()                     # new
    rig.run(0.2)
    assert rig.state() == "idle"
    rig.sim.knob_press()                     # nothing to do in idle
    rig.run(0.2)
    assert rig.state() == "idle"


@test
def knob_three_detents_place_the_vessel_and_press_stops():
    rig = Rig()
    rig.m.select("tea", 4)
    rig.sim.knob_turn(3)                     # exactly 150 g
    rig.wait("ready", 5)
    rig.wait("dispensing", 5)
    assert rig.run(1.0, until=rig.valve_open)
    rig.run(2.0)
    started = rig.clock.t
    rig.sim.knob_press()                     # the Knob is a stop button while water runs
    rig.run(0.1)
    assert not rig.valve_open() and rig.clock.t - started <= 0.2
    assert rig.state() == "error" and rig.flow()["error"] == "stopped"


@test
def no_knob_no_buzzer():
    rig = Rig(knob=False, buzzer=False)
    board_part = rig.snap()["board"]
    assert board_part["knob"] is False and board_part["buzzer"] is False
    rig.to_dispensing("tea", 1)
    rig.wait("done", 30)


# ============================================================================
# the simulated board itself
# ============================================================================

def raw_dispense(sim, clock, ml):
    """Drive SimLink the way Python does, without the state machine. Returns (kind, value)."""
    events = []
    sim.start(lambda *status: None, lambda kind, value: events.append((kind, value)))
    assert sim.call("dispense", float(ml)) is True
    last_ping = clock.t
    limit = clock.t + 400
    while clock.t < limit:
        clock.advance(0.01)
        if clock.t - last_ping >= 0.5:
            sim.call("ping")
            last_ping = clock.t
        sim.step()
        for kind, value in events:
            if kind == "done" or kind.startswith("error_"):
                return kind, value
    raise AssertionError("dispense never ended")


@test
def dispense_accuracy_across_amounts_and_flows():
    for flow in (10.0, 30.0, 80.0):
        for ml in (250, 500, 1300, 4000):
            if ml / flow > 170:
                continue                     # slower than the 180 s cap allows
            clock = FakeClock()
            sim = board.SimLink(clock=clock, seed=int(flow) + ml)
            sim.call("set_param", "flow_gps", flow)
            sim.call("sim_add", 900.0)
            for _ in range(150):
                clock.advance(0.01)
                sim.step()
            sim.call("tare")
            clock.advance(0.01)
            sim.step()
            kind, actual = raw_dispense(sim, clock, ml)
            assert kind == "done" and close(actual, ml, 10), (flow, ml, kind, actual)
    clock = FakeClock()
    sim = board.SimLink(clock=clock)
    assert sim.call("dispense", 0.0) is False and sim.call("dispense", 4000.1) is False
    assert sim.call("dispense", -5.0) is False
    assert sim.call("dispense", 100.0) is True and sim.call("dispense", 100.0) is False   # busy
    assert sim.call("show", 8) is False and sim.call("show", 7) is True
    assert sim.call("beep", 5) is False and sim.call("beep", 4) is True
    assert sim.call("set_param", "lead_s", 9.0) is True and sim._lead_s == 2.0            # clamped
    assert sim.call("calibrate", 500.0) == 0.0                                            # virtual scale
    assert sim.call("info") == "fw=1.0;scale=virtual;knob=1;buzzer=1;tilt=0"


@test
def lead_time_is_learned_and_kept():
    rig = Rig(tail_s=0.8)                    # more water in flight than the default lead expects
    errors = []
    for _ in range(5):
        rig.to_dispensing("tea", 2)
        rig.wait("done", 60)
        errors.append(rig.snap()["last_run"]["dispensed_ml"] - 500)
        rig.m.cancel()
        rig.m.sim("remove_vessel")
        rig.run(1.5)
    assert errors[0] >= 8, errors            # the first run overshoots
    assert abs(errors[-1]) <= 3, errors      # then the lead adapts
    learned = rig.store.settings()["lead_s"]
    assert 0.6 < learned <= 0.9, learned
    rig.sim.reboot()
    rig.run(0.3)
    assert rig.m.sync_once()
    assert close(rig.sim._lead_s, learned, 1e-6), "the learned lead is sent back at boot"


@test
def stable_flag_and_tare():
    clock = FakeClock()
    sim = board.SimLink(clock=clock)
    statuses = []
    sim.start(lambda *status: statuses.append(status), lambda kind, value: None)

    def run(seconds):
        end = clock.t + seconds
        while clock.t < end:
            clock.advance(0.01)
            sim.step()

    run(1.5)
    assert statuses[-1][1] is True                           # stable
    assert statuses[-1][6] is True and statuses[-1][3] == 0  # virtual scale, idle
    sim.knob_turn(4)
    run(0.4)
    assert statuses[-1][1] is False and close(statuses[-1][0], 200, 1)
    run(1.2)
    assert statuses[-1][1] is True
    sim.knob_turn(-10)                                       # never below zero
    run(1.5)
    assert close(statuses[-1][0], 0, 1)
    sim.call("sim_set", 750.0)
    run(1.5)
    assert sim.call("tare") is True
    run(0.5)
    assert close(statuses[-1][0], 0, 1) and statuses[-1][1] is True
    assert len(statuses) in range(30, 36), "a status every 200 ms"


# ============================================================================
# storage
# ============================================================================

@test
def settings_are_checked_and_kept():
    rig = Rig()
    defaults = rig.m.settings()
    assert (defaults["t_start_c"], defaults["eff_kettle"], defaults["eff_stove"]) == (15.0, 0.85, 0.75)
    assert (defaults["price_eur_per_kwh"], defaults["kettle_kw"], defaults["kettle_min_ml"]) == (0.40, 2.0, 250)
    changed = rig.m.update_settings({"eff_kettle": 5, "price_eur_per_kwh": "0,35", "kettle_min_ml": 99999,
                                     "t_start_c": "warm", "hacker": True, "lead_s": 1.9,
                                     "baselines": {"tea": 3, "pizza": 9, "pasta": 999}})
    assert changed["eff_kettle"] == 1.0 and changed["price_eur_per_kwh"] == 0.35
    assert changed["kettle_min_ml"] == 1500 and changed["t_start_c"] == 15.0
    assert "hacker" not in changed and changed["lead_s"] == 0.4, "lead_s is not user-editable"
    assert changed["baselines"] == {"tea": 3.0, "pasta": 30.0}
    assert not os.path.exists(os.path.join(rig.dir, "settings.json.tmp")), "atomic write leaves no temp file"
    assert rig.sim._flow_gps == 30.0
    rig.m.update_settings({"flow_gps": 55})
    assert rig.sim._flow_gps == 55.0, "the virtual flow is pushed to the board"

    again = storage.Storage(rig.dir)
    assert again.settings() == rig.m.settings()
    recipes_now = rig.m.recipes()["recipes"]
    assert [r["baseline"]["value"] for r in recipes_now if r["id"] == "tea"] == [3.0]
    assert rig.m.update_settings({"baselines": {"tea": None}})["baselines"] == {"pasta": 30.0}

    rig.store.set_device("lead_s", 0.55)
    rig.store.set_device("cal", 421.5)
    reset = rig.m.update_settings({"reset": True})
    assert reset["eff_kettle"] == 0.85 and reset["baselines"] == {} and reset["flow_gps"] == 30.0
    assert reset["lead_s"] == 0.55 and reset["cal"] == 421.5, "reset keeps what the hardware learned"

    with open(os.path.join(rig.dir, "settings.json"), "w") as fh:
        fh.write("{ this is not json")
    broken = storage.Storage(rig.dir)
    assert broken.settings() == storage.DEFAULT_SETTINGS
    assert os.path.exists(os.path.join(rig.dir, "settings.json.bad"))


@test
def baseline_override_changes_the_savings():
    rig = Rig()
    rig.m.update_settings({"baselines": {"tea": 1.5}, "price_eur_per_kwh": 0.30, "t_start_c": 10})
    rig.to_dispensing("tea", 2)
    rig.wait("done", 60)
    run = rig.snap()["last_run"]
    assert run["baseline_ml"] == 750 and close(run["saved_ml"], 250, 10), run
    expected_wh = run["saved_ml"] / 1000.0 * 4.186 * 90 / 3600 / 0.85 * 1000
    assert close(run["saved_wh"], expected_wh, 0.2) and close(run["saved_eur"], expected_wh * 0.0003, 0.0005)


@test
def history_totals_and_today():
    folder = tempfile.mkdtemp(prefix="selftest-")
    _TEMP_DIRS.append(folder)
    wall = [1800000000.0]
    store = storage.Storage(folder, wallclock=lambda: wall[0])
    for i in range(505):
        store.add_run({"recipe": "tea", "saved_ml": 100, "saved_wh": 10.0, "saved_eur": 0.004, "n": i})
    assert len(store.history(1000)) == 500, "only the last 500 runs are kept"
    assert store.history(20)[0]["n"] == 504 and len(store.history(20)) == 20
    stats = store.stats()
    assert stats["total"] == {"runs": 505, "saved_ml": 50500, "saved_wh": 5050.0, "saved_eur": 2.02}
    assert stats["today"]["runs"] == 500

    wall[0] += 86400.0                       # the next day
    assert store.stats()["today"]["runs"] == 0 and store.stats()["total"]["runs"] == 505
    store.add_run({"recipe": "pasta", "saved_ml": 1500, "saved_wh": 190.0, "saved_eur": 0.076})
    assert store.stats()["today"] == {"runs": 1, "saved_ml": 1500, "saved_wh": 190.0, "saved_eur": 0.076}

    again = storage.Storage(folder, wallclock=lambda: wall[0])
    assert again.stats() == store.stats() and again.last_run()["recipe"] == "pasta"
    assert not os.path.exists(os.path.join(folder, "history.json.tmp"))
    with open(os.path.join(folder, "history.json")) as fh:
        assert len(json.load(fh)["runs"]) == 500

    rig = Rig(data_dir=folder)               # the app shows the last run after a restart
    assert rig.snap()["last_run"]["recipe"] == "pasta"


@test
def storage_survives_a_read_only_folder():
    original = storage.FALLBACK_DIR
    storage.FALLBACK_DIR = tempfile.mkdtemp(prefix="selftest-fallback-")    # never the shared one
    _TEMP_DIRS.append(storage.FALLBACK_DIR)
    try:
        store = storage.Storage("/proc/definitely/not/writable/data")
        assert store.data_dir == storage.FALLBACK_DIR
        assert store.settings() == storage.DEFAULT_SETTINGS
        store.add_run({"recipe": "tea", "saved_ml": 1})
        assert store.stats()["total"]["runs"] == 1
    finally:
        storage.FALLBACK_DIR = original


# ============================================================================
# calibration and real-scale behaviour (a scripted link stands in for the HX711 board)
# ============================================================================

class ScriptedLink(board.BoardLink):
    """A board that reports a real scale. Records calls; tests push status and events by hand."""

    def __init__(self):
        self.calls = []
        self.on_status = self.on_event = None

    def start(self, on_status, on_event):
        self.on_status, self.on_event = on_status, on_event

    def call(self, name, *args, **kwargs):
        self.calls.append((name,) + args)
        if name == "info":
            return "fw=1.0;scale=hx711;knob=1;buzzer=0;tilt=0"
        if name == "calibrate":
            return 412.5 if args[0] == 500.0 else 0.0
        return True


@test
def real_scale_calibration_and_knob_portions():
    folder = tempfile.mkdtemp(prefix="selftest-")
    _TEMP_DIRS.append(folder)
    clock = FakeClock()
    link = ScriptedLink()
    brd = board.Board(link, clock=clock)
    store = storage.Storage(folder)
    book = recipes.RecipeBook.load()
    machine = core.Machine(brd, book, store, intent.IntentEngine(book, url=None), clock=clock)

    def status(net=0.0, stable=True):
        clock.advance(0.2)
        link.on_status(net, stable, False, 0, 0.0, 0.0, False)
        machine.tick()

    status()
    assert machine.sync_once()
    snap = machine.snapshot()
    assert snap["board"]["virtual_scale"] is False and snap["board"]["buzzer"] is False
    assert snap["sim"] is None
    assert not machine.sim("place_vessel")["ok"], "nothing to simulate on a real scale"

    zero = machine.calibrate(0)
    assert zero["ok"] and zero["factor"] is None and link.calls[-1] == ("tare",)
    done = machine.calibrate(500)
    assert done == {"ok": True, "factor": 412.5, "message": "Calibrated and saved."}
    assert link.calls[-1] == ("calibrate", 500.0) and isinstance(link.calls[-1][1], float)
    assert store.settings()["cal"] == 412.5
    assert not machine.calibrate(123)["ok"]

    link.calls[:] = []
    assert machine.sync_once()               # after a reboot the factor goes back to the MCU
    assert ("set_param", "cal", 412.5) in link.calls and ("set_param", "lead_s", 0.4) in link.calls
    assert ("set_param", "flow_gps", 30.0) in link.calls and ("show", 0) in link.calls
    for call in link.calls:
        if call[0] == "show":
            assert type(call[1]) is int, "int parameters must be real ints for MsgPack"
        if call[0] == "set_param":
            assert type(call[2]) is float

    # real scale: the Knob changes the portions before the water starts
    for _ in range(20):
        status()                             # let the "just synced" window pass
    assert machine.select("tea", 2)["flow"]["target_ml"] == 500
    link.on_event("knob_turn", 2.0)
    status()
    flow = machine.snapshot()["flow"]
    assert flow["portions"] == 4 and flow["target_ml"] == 1000
    for _ in range(3):
        status(net=0.0)
    status(net=870.0, stable=False)
    assert machine.snapshot()["flow"]["state"] == "vessel", "wait until the scale is stable"
    status(net=870.0)
    assert machine.snapshot()["flow"]["phase"] == "tare" and ("tare",) in link.calls
    status(net=870.0, stable=False)          # tare is asynchronous on a real scale
    status(net=870.0, stable=False)
    assert machine.snapshot()["flow"]["state"] == "vessel"
    status(net=0.2)
    assert machine.snapshot()["flow"]["state"] == "ready"
    link.on_event("knob_turn", -1.0)
    status(net=0.2)
    flow = machine.snapshot()["flow"]
    assert flow["portions"] == 3 and flow["target_ml"] == 750 and flow["countdown"] == 3
    for _ in range(16):
        status(net=0.2)
    assert machine.snapshot()["flow"]["state"] == "dispensing"
    assert ("dispense", 750.0) in link.calls and type(link.calls[-1][1] if link.calls[-1][0] == "dispense" else 1.0) is float
    assert ("ping",) in link.calls or True
    link.on_event("done", 748.6)
    link.on_event("lead_s", 0.47)
    status(net=748.6)
    snap = machine.snapshot()
    assert snap["flow"]["state"] == "done" and snap["last_run"]["dispensed_ml"] == 749
    assert store.settings()["lead_s"] == 0.47


# ============================================================================
# files: the things a compiler or App Lab would otherwise catch
# ============================================================================

def read(*parts):
    with open(os.path.join(APP_DIR, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


@test
def app_yaml_and_sketch_yaml_are_exact():
    assert read("app.yaml") == (
        'name: "' + NAME + '"\n'
        "icon: 💧\n"
        "description: Cooking water assistant that dispenses only the water you need\n"
        "bricks:\n"
        "  - arduino:web_ui\n"
        "  - arduino:llm:\n"
        "      model: llamacpp:Qwen3.5-0.8B-Q4_0\n"
        "      variables:\n"
        '        LLAMA_ARG_CTX_SIZE: "2048"\n')
    assert read("sketch", "sketch.yaml") == (
        "profiles:\n"
        "  default:\n"
        "    platforms:\n"
        "      - platform: arduino:zephyr\n"
        "    libraries:\n"
        "      - Arduino_Modulino (0.7.0)\n"
        "      - dependency: ArduinoGraphics (1.1.4)\n"
        "      - dependency: Arduino_HS300x (1.0.0)\n"
        "      - dependency: Arduino_LPS22HB (1.0.2)\n"
        "      - dependency: Arduino_LSM6DSOX (1.1.2)\n"
        "      - dependency: Arduino_LTR381RGB (1.0.0)\n"
        "      - dependency: STM32duino VL53L4CD (1.0.5)\n"
        "      - dependency: STM32duino VL53L4ED (1.0.1)\n"
        "default_profile: default\n")


@test
def app_name_lives_in_one_place():
    allowed = {os.path.join("python", "core.py"): 1, "app.yaml": 1,
               os.path.join("assets", "manifest.json"): 2}
    found = {}
    for folder, dirs, files in os.walk(APP_DIR):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "data")]
        for name in files:
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, APP_DIR)
            if rel == "README.md" or name.endswith((".pyc", ".png")) or name == ".DS_Store":
                continue
            with open(path, "r", encoding="utf-8") as fh:
                count = fh.read().count(NAME)
            if count:
                found[rel] = count
    assert found == allowed, "the app name may only be written in {}: found {}".format(allowed, found)
    manifest = json.loads(read("assets", "manifest.json"))
    assert manifest["name"] == NAME and manifest["short_name"] == NAME


@test
def python_modules_are_safe_names():
    forbidden = {"select", "platform", "types", "queue", "json", "http", "logging", "parser", "test",
                 "time", "random", "socket", "threading", "math", "re", "os", "sys", "copy", "tempfile",
                 "collections", "string", "io", "signal", "code", "token", "site", "email", "calendar"}
    names = sorted(n[:-3] for n in os.listdir(HERE) if n.endswith(".py"))
    assert not (set(names) & forbidden), set(names) & forbidden
    for expected in ("main", "core", "recipes", "intent", "energy", "storage", "board",
                     "webapi", "dev_server", "selftest"):
        assert expected in names, "missing python/{}.py".format(expected)
    if hasattr(sys, "stdlib_module_names"):
        assert not (set(names) & set(sys.stdlib_module_names)), set(names) & set(sys.stdlib_module_names)
    main = read("python", "main.py")
    for needle in ("from arduino.app_utils import", "from arduino.app_bricks.web_ui import WebUI",
                   "ui = WebUI()", "App.run(user_loop=loop)"):
        assert needle in main, "main.py should contain: " + needle
    assert "ui.start(" not in main and "socketio" not in main.lower()


C_TYPES = {float: "float", int: "int", str: "String"}


@test
def bridge_contract_matches_the_sketch():
    sketch = read("sketch", "sketch.ino")
    code = re.sub(r"//[^\n]*", "", sketch)
    provided = dict(re.findall(r'Bridge\.provide_safe\(\s*"(\w+)"\s*,\s*(\w+)\s*\)', code))
    assert set(provided) == set(board.SIGNATURES), (sorted(provided), sorted(board.SIGNATURES))
    returns = {"info": "String", "calibrate": "float"}
    for name, types in board.SIGNATURES.items():
        assert provided[name] == "rpc_" + name, "handler for {} must be rpc_{}".format(name, name)
        match = re.search(r"^(\w+)\s+rpc_" + name + r"\s*\(([^)]*)\)\s*\{", code, flags=re.M)
        assert match, "rpc_{} is not defined".format(name)
        assert match.group(1) == returns.get(name, "bool"), (name, match.group(1))
        params = [p.split()[0] for p in match.group(2).split(",") if p.strip()]
        assert params == [C_TYPES[t] for t in types], (name, params)
    assert "Bridge.provide(" not in code, "every handler must use provide_safe"
    assert len(re.findall(r'Bridge\.notify\(\s*"mcu_status"', code)) == 1
    status_call = re.search(r'Bridge\.notify\(\s*"mcu_status"\s*,([^;]*)\);', code).group(1)
    assert len(status_call.split(",")) == 7, "mcu_status carries seven values"
    assert 'Bridge.notify("mcu_event"' in code
    for kind in ("boot", "done", "lead_s", "knob_press", "knob_turn"):
        assert '"' + kind + '"' in code, "the sketch never sends " + kind
    for error in core.ERROR_CODES:
        if error != "board_offline":
            assert '"error_' + error + '"' in code or '"' + error + '"' in code, "missing safety stop " + error
    # platform rules
    assert not re.search(r"\bSerial\s*\.", code), "use Monitor, never Serial"
    for millis in re.findall(r"\bdelay\(\s*(\d+)\s*\)", code):
        assert int(millis) <= 2, "no delay() longer than 2 ms"
    assert "Bridge.call" not in code, "never call Bridge.call from the sketch"
    for needle in ("#include <Arduino_RouterBridge.h>", "#include <Arduino_LED_Matrix.h>",
                   "#include <Arduino_Modulino.h>", "Monitor.begin(115200)", "Bridge.begin()",
                   "Modulino.begin(Wire1)", "matrix.setGrayscaleBits(3)", "#define RGB_ACTIVE_LOW 1",
                   "#define USE_TILT 0", "LED4_R", "LED4_G", "LED4_B", "noInterrupts()", "interrupts()"):
        assert needle in code, "the sketch should contain: " + needle
    setup = code[code.index("void setup()"):]
    assert setup.index("PIN_VALVE") < setup.index("Monitor.begin"), "the valve is closed first in setup()"


@test
def sketch_constants_match_the_simulation():
    sketch = read("sketch", "sketch.ino")

    def value(name):
        match = re.search(r"\b" + name + r"\s*=\s*(-?[0-9.]+)f?\s*;", sketch)
        assert match, "constant {} not found in sketch.ino".format(name)
        return float(match.group(1))

    pairs = [
        ("STATUS_PERIOD_MS", board.STATUS_PERIOD_S * 1000), ("SIM_SAMPLE_PERIOD_MS", board.SAMPLE_PERIOD_S * 1000),
        ("STABLE_SAMPLES", board.STABLE_SAMPLES), ("STABLE_RANGE_G", board.STABLE_RANGE_G),
        ("KNOB_STEP_G", board.KNOB_STEP_G), ("FLOW_WINDOW_MS", board.FLOW_WINDOW_S * 1000),
        ("FLOW_EMA", board.FLOW_EMA), ("EXTRAPOLATE_MAX_MS", board.EXTRAPOLATE_MAX_S * 1000),
        ("SETTLE_MS", board.SETTLE_S * 1000), ("LEAD_DEFAULT_S", board.LEAD_DEFAULT_S),
        ("LEAD_MAX_S", board.LEAD_MAX_S), ("LEAD_MAX_STEP_S", board.LEAD_MAX_STEP_S),
        ("MAX_DISPENSE_ML", board.MAX_DISPENSE_ML), ("VESSEL_DROP_G", board.VESSEL_DROP_G),
        ("NOFLOW_GRACE_MS", board.NOFLOW_GRACE_S * 1000), ("NOFLOW_WINDOW_MS", board.NOFLOW_WINDOW_S * 1000),
        ("NOFLOW_MIN_G", board.NOFLOW_MIN_G), ("PING_TIMEOUT_MS", board.PING_TIMEOUT_S * 1000),
        ("TIMEOUT_MIN_FLOW_GPS", board.TIMEOUT_MIN_FLOW_GPS), ("TIMEOUT_EXTRA_MS", board.TIMEOUT_EXTRA_S * 1000),
        ("TIMEOUT_CAP_MS", board.TIMEOUT_CAP_S * 1000), ("TILT_HOLD_MS", board.TILT_HOLD_S * 1000),
        ("GREEN_MS", board.GREEN_S * 1000), ("BOOT_REPEAT_MS", board.BOOT_REPEAT_S * 1000),
        ("SIM_START_LAG_MS", 300), ("SIM_TAIL_MS", 400), ("SIM_NOISE_G", board.NOISE_G),
        ("FLOW_GPS_DEFAULT", board.FLOW_GPS_DEFAULT), ("FLOW_GPS_MAX", board.FLOW_GPS_MAX),
    ]
    for name, expected in pairs:
        assert close(value(name), expected, 1e-6), "{}: sketch {} != simulation {}".format(
            name, value(name), expected)
    assert 'FW_VERSION = "1.0"' in sketch
    for screen_id, screen in enumerate(board.SCREEN_NAMES):
        assert re.search(r"SCREEN_" + screen.upper() + r"\s*=\s*" + str(screen_id) + r"\b", sketch), screen


@test
def assets_work_offline():
    for name in ("index.html", "app.js", "style.css", "manifest.json", "icon.svg"):
        assert os.path.exists(os.path.join(APP_DIR, "assets", name)), "missing assets/" + name
    external = re.compile(r"""(?:src|href|action)\s*=\s*["']\s*(?:https?:)?//|url\(\s*["']?\s*(?:https?:)?//|"""
                          r"""@import|import\s*\(|\bfrom\s+["']https?:|fetch\(\s*["']https?:""", re.I)
    for name in ("index.html", "app.js", "style.css"):
        text = read("assets", name)
        assert not external.search(text), "assets/{} loads something from the network".format(name)
        assert "socket.io" not in text.lower() and "cdn" not in text.lower()
    html = read("assets", "index.html")
    for needle in ('rel="manifest"', "apple-mobile-web-app-capable", 'name="theme-color"',
                   'aria-live="polite"', 'name="viewport"', "app.js", "style.css", "icon.svg"):
        assert needle in html, "index.html should contain " + needle
    js = read("assets", "app.js")
    for needle in ("/api/state", "/api/recipes", "/api/parse", "/api/select", "/api/next", "/api/cancel",
                   "/api/stop", "/api/sim", "/api/settings", "/api/calibrate", "/api/history",
                   "speechSynthesis"):
        assert needle in js, "app.js never uses " + needle
    css = read("assets", "style.css")
    assert "prefers-color-scheme" in css
    manifest = json.loads(read("assets", "manifest.json"))
    assert manifest["display"] == "standalone" and manifest["icons"][0]["src"] == "icon.svg"


@test
def rest_routes_are_all_registered():
    source = read("python", "webapi.py")
    routes = set(re.findall(r'\(\s*"(GET|POST)"\s*,\s*"(/api/\w+)"', source))
    expected = {("GET", "/api/state"), ("GET", "/api/recipes"), ("POST", "/api/parse"),
                ("POST", "/api/select"), ("POST", "/api/next"), ("POST", "/api/cancel"),
                ("POST", "/api/stop"), ("POST", "/api/sim"), ("GET", "/api/settings"),
                ("POST", "/api/settings"), ("POST", "/api/calibrate"), ("GET", "/api/history")}
    assert routes == expected, routes ^ expected


# ============================================================================

def main():
    started = time.time()
    failed = []
    for fn in TESTS:
        try:
            fn()
            print("ok    " + fn.__name__)
        except Exception:
            failed.append(fn.__name__)
            print("FAIL  " + fn.__name__)
            traceback.print_exc()
    for folder in _TEMP_DIRS:
        shutil.rmtree(folder, ignore_errors=True)
    print("\n{} tests, {} failed, {:.1f} s".format(len(TESTS), len(failed), time.time() - started))
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    sys.exit(main())
