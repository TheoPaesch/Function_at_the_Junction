"""Free text -> order: {"recipe", "portions", "grams", "source", "ms"}.

The local LLM goes first when it is ready; the rules are instant and always
there (LLM off, failed, slow or invalid). English and German. The LLM is an
optional accelerator, never a dependency:

* The rules always run. Numbers written in the text ("for 4", "2 cups",
  "300 g") come from the rules, because digits are not a matter of opinion.
* When the keywords in the text point to one recipe, that decides. The model
  decides when the rules find nothing, or keywords of two different recipes
  in different places ("tea and pasta").
* One request never waits longer than 15 s for the model, whatever fallbacks
  it goes through. A model that fails, is too slow or gives unusable answers
  is marked "failed" and tried again later, with growing pauses.

Synonyms live in recipes.json. Two bits of syntax keep German compounds and
word pairs short there:
  "*tee" / "kartoffel*"   wildcard: Pfefferminztee, Kartoffelsalat
  "instant+nudeln"        both words anywhere in the text, in any order
"""

import difflib
import json
import logging
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.request

from recipes import MAX_GRAMS, RecipeBook

log = logging.getLogger("intent")

LLM_URL = "http://llamacpp-models-runner:9999/v1"
LLM_MODEL = "Qwen3.5-0.8B-Q4_0"
LLM_LABEL = "Qwen 0.8B"
LLM_TIMEOUT_S = 15.0        # a user never waits longer than this for the model
WARMUP_TIMEOUT_S = 120.0    # the first request also loads the model
WARM_SAMPLES = 2            # tries to get one usable answer within LLM_TIMEOUT_S
RETRY_FIRST_S = 60.0        # after "failed": try again after this long ...
RETRY_MAX_S = 900.0         # ... doubling up to this
MAX_TEXT = 200
MAX_PORTIONS = 12

# What we ask the server for, from most to least: a server that rejects a
# field it does not know (HTTP 4xx) gets the next variant.
_VARIANTS = (
    ("response_format", "chat_template_kwargs"),    # JSON schema, thinking off
    ("chat_template_kwargs",),                      # thinking off
    (),                                             # plain chat completion
)

# ---- text folding ----------------------------------------------------------

_TRANSLIT = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def fold(text):
    """Lower case, accents and umlaut dots removed: 'Erdäpfel' -> 'erdapfel'."""
    text = unicodedata.normalize("NFKC", str(text)).lower().replace("ß", "ss")
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")


def variants(word):
    """The ways people type a word: 'fünf' -> {'funf', 'fuenf'}."""
    lower = unicodedata.normalize("NFKC", str(word)).lower()
    return {fold(lower), fold("".join(_TRANSLIT.get(ch, ch) for ch in lower))}


def normalize(text):
    """Folded text holding only letters, digits, decimal points and single spaces."""
    text = fold(str(text)[:MAX_TEXT]).replace("⁄", "/")
    text = re.sub(r"\b1\s*/\s*2\b", "0.5", text)
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = re.sub(r"[^a-z0-9.]+", " ", text)
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _alt(words):
    """Regex alternation of all typed variants of the given words, longest first."""
    forms = set()
    for word in words:
        forms |= variants(word)
    return "(?:" + "|".join(re.escape(f) for f in sorted(forms, key=lambda f: (-len(f), f))) + ")"


# ---- numbers ---------------------------------------------------------------

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "eins": 1, "ein": 1, "eine": 1, "einen": 1, "einem": 1, "einer": 1,
    "zwei": 2, "zwo": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6,
    "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12,
}
_NUMBERS = {}
for _word, _n in _NUMBER_WORDS.items():
    for _form in variants(_word):
        _NUMBERS[_form] = _n
_ARTICLES = {"a", "an", "ein", "eine", "einen", "einem", "einer"}   # a count only before a unit
_ZU = {"zweit": 2, "dritt": 3, "viert": 4, "funft": 5, "fuenft": 5, "sechst": 6, "siebt": 7, "acht": 8}

_NUM = r"(?:\d{1,4}(?:\.\d+)?|" + "|".join(sorted(_NUMBERS, key=lambda w: (-len(w), w))) + ")"

_UNIT_LIST = [
    # people
    "people", "persons", "person", "personen", "leute", "portions", "portion", "portionen",
    "servings", "serving", "guests", "guest", "gäste", "gast", "adults", "adult", "kids", "kid",
    "children", "kinder", "erwachsene", "friends", "freunde", "mann", "pers", "eaters", "esser",
    "plates", "teller", "bowls", "schüsseln",
    # cups
    "cups", "cup", "mugs", "mug", "tassen", "tasse", "becher", "glasses", "glass", "gläser", "glas",
    # packs
    "packs", "pack", "packets", "packet", "packages", "package", "packungen", "packung",
    "päckchen", "tüten", "tüte", "bags", "bag", "blocks", "block",
]
_UNITS = _alt(_UNIT_LIST)
_UNIT_FORMS = set()
for _word in _UNIT_LIST:
    _UNIT_FORMS |= variants(_word)
_KG = r"(?:kg|kilo|kilos|kilogram|kilograms|kilogramm|kilogramme)"
_G = r"(?:g|gr|gram|grams|gramm|gramme|grammes)"

_RE_HALF_KG = re.compile(r"\b(?:half an?|a half|ein halbes|einem halben|halbes|halbe)\s+" + _KG + r"\b")
_RE_KG = re.compile(r"\b(" + _NUM + r"|an?)\s*" + _KG + r"\b")
_RE_G = re.compile(r"\b(\d{1,4}(?:\.\d+)?)\s*" + _G + r"\b")
_RE_PFUND = re.compile(r"\b(" + _NUM + r"|an?)\s*pfund\b")
_RE_POUND = re.compile(r"\b(" + _NUM + r"|an?)\s*(?:lbs|lb|pounds|pound)\b")

_RE_FOR = re.compile(r"\b(?:for|fur|fuer)\s+(?:about\s+|around\s+|ca\s+|circa\s+|us\s+|uns\s+)?(" + _NUM + r")\b")
_RE_ME = re.compile(r"\b(?:for|fur|fuer)\s+(?:just\s+|only\s+|nur\s+)?(?:me|myself|mich)\b"
                    r"|\b(?:just me|only me|by myself|nur ich|nur fur mich|nur fuer mich)\b")
_RE_BOTH = re.compile(r"\b(?:us both|both of us|the two of us|us two|uns beide|wir beide|uns zwei)\b")
_SELF = r"(?:me|myself|i|ich|mich|mir)"
_RE_PLUS_ME = re.compile(r"\b" + _SELF + r"\s+(?:and|und|plus)\b|\b(?:and|und|plus)\s+" + _SELF + r"\b")
_RE_UNIT = re.compile(r"\b(" + _NUM + r"|an?)\s*(?:x\s*)?" + _UNITS + r"\b")
_RE_TIMES = re.compile(r"\b(" + _NUM + r")\s*(?:x|times|mal)\b|\bx\s*(" + _NUM + r")\b")
_RE_ZU = re.compile(r"\bzu\s+(" + "|".join(_ZU) + r")\b")
_RE_BARE = re.compile(r"\b(" + _NUM + r")\b")


def _to_number(token):
    if token in ("a", "an"):
        return 1.0
    if token in _NUMBERS:
        return float(_NUMBERS[token])
    try:
        return float(token)
    except ValueError:
        return None


def _blank(text, match):
    return text[:match.start()] + " " * (match.end() - match.start()) + text[match.end():]


def take_grams(norm):
    """Find a weight in the text. Returns (text without the weight, grams or None)."""
    match = _RE_HALF_KG.search(norm)
    if match:
        return _blank(norm, match), 500
    for pattern, factor in ((_RE_KG, 1000.0), (_RE_G, 1.0), (_RE_PFUND, 500.0), (_RE_POUND, 454.0)):
        match = pattern.search(norm)
        if match:
            value = _to_number(match.group(1))
            if value is not None and value > 0:
                return _blank(norm, match), int(round(value * factor))
    return norm, None


def take_portions(norm):
    """Find a count in the text. Returns (number or None, strong).

    strong is True when the wording is explicit ("for 4", "2 cups", "3x");
    a lone number is a weaker hint. "me and my two kids" counts the speaker
    too, but stays a weak hint so the model may correct it.
    """
    plus_me = bool(_RE_PLUS_ME.search(norm))
    for pattern in (_RE_FOR, _RE_UNIT, _RE_TIMES):
        match = pattern.search(norm)
        if match:
            number = _to_number(match.group(1) or match.group(match.lastindex))
            if number is not None and plus_me:
                return number + 1.0, False
            return number, True
    match = _RE_ZU.search(norm)
    if match:
        return float(_ZU[match.group(1)]), True
    if _RE_BOTH.search(norm):
        return 2.0, True
    if plus_me:
        return 2.0, False               # "me and my girlfriend": at least two
    if _RE_ME.search(norm):
        return 1.0, True
    for match in _RE_BARE.finditer(norm):
        if match.group(1) not in _ARTICLES:
            return _to_number(match.group(1)), False
    return None, False


# ---- recipe keywords -------------------------------------------------------

_PLURAL = r"(?:s|es|n|en)?"
_TIER_WORDS, _TIER_WILDCARD, _TIER_FUZZY = 2, 1, 0


def _word_regex(word):
    return r"(?<![a-z0-9])" + _alt([word]) + _PLURAL + r"(?![a-z0-9])"


class _Keyword(object):
    """One synonym from recipes.json, ready to be looked for in normalized text."""

    def __init__(self, recipe_id, synonym):
        self.recipe_id = recipe_id
        raw = synonym.strip()
        self.prefix = self.suffix = None
        self.patterns = []
        # "cup", "mug", "Tasse" are also measures: "2 cups of noodles" is not tea.
        # Such a word names its recipe only when no other recipe word is in the text.
        self.weak = not any(ch in raw for ch in "*+ ") and bool(variants(raw) & _UNIT_FORMS)
        if raw.startswith("*") or raw.endswith("*"):
            self.tier = _TIER_WILDCARD
            stems = variants(raw.strip("*"))
            self.length = max(len(s) for s in stems)
            if raw.startswith("*"):
                self.suffix = tuple(stems)        # "*tee": the token ends with it
            else:
                self.prefix = tuple(stems)        # "kartoffel*": the token starts with it
        elif "+" in raw:
            self.tier = _TIER_WORDS
            parts = [p.strip() for p in raw.split("+") if p.strip()]
            self.length = sum(len(fold(p)) for p in parts)
            self.patterns = [re.compile(_word_regex(p)) for p in parts]
        else:
            self.tier = _TIER_WORDS
            words = raw.split()
            self.length = sum(len(fold(w)) for w in words)
            body = r"\s+".join(_alt([w]) for w in words)
            self.patterns = [re.compile(r"(?<![a-z0-9])" + body + _PLURAL + r"(?![a-z0-9])")]

    def find(self, norm, tokens):
        """The (start, end) spans this keyword covers in the text, or None when it is not there."""
        if self.patterns:
            spans = []
            for pattern in self.patterns:
                match = pattern.search(norm)
                if not match:
                    return None
                spans.append((match.start(), match.end()))
            return spans
        position = 0
        for token in tokens:
            if self.suffix and len(token) > self.length and token.endswith(self.suffix):
                return [(position, position + len(token))]
            if self.prefix and len(token) > self.length and token.startswith(self.prefix):
                return [(position, position + len(token))]
            position += len(token) + 1
        return None


# ---- the local LLM ---------------------------------------------------------

def build_system_prompt(book):
    """Under 150 tokens: the recipe ids, four examples (one German), JSON only."""
    names = []
    for recipe in book.all():
        label = recipe["id"]
        if recipe["name"].lower() != recipe["id"]:
            label += " (" + recipe["name"].lower() + ")"
        names.append(label)
    lines = [
        "Turn the request into a water order. recipe: " + ", ".join(names) + " or unknown. "
        "portions: cups, packs or people, else null. "
        "grams: only a stated weight, else null. Answer with JSON only."
    ]
    examples = (
        ("2 cups of tea", "tea", 2, None),
        ("300 g spaghetti", "pasta", None, 300),
        ("Kartoffeln für 3", "potatoes", 3, None),
        ("a pizza", "unknown", None, None),
    )
    for text, recipe_id, portions, grams in examples:
        if recipe_id == "unknown" or book.get(recipe_id) is not None:
            order = {"recipe": recipe_id, "portions": portions, "grams": grams}
            lines.append(text + " -> " + json.dumps(order, separators=(",", ":")))
    return "\n".join(lines)


def build_schema(book):
    """JSON schema for response_format: recipe ids plus 'unknown', bounded numbers or null."""
    return {
        "type": "object",
        "properties": {
            "recipe": {"type": "string", "enum": book.ids() + ["unknown"]},
            "portions": {"anyOf": [{"type": "integer", "minimum": 1, "maximum": MAX_PORTIONS},
                                   {"type": "null"}]},
            "grams": {"anyOf": [{"type": "integer", "minimum": 0, "maximum": MAX_GRAMS},
                                {"type": "null"}]},
        },
        "required": ["recipe", "portions", "grams"],
        "additionalProperties": False,
    }


def chat_endpoint(url):
    """Accepts a base URL with or without /v1, or the full chat completions URL."""
    url = str(url).strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def _is_timeout(exc):
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(exc).lower()


def _whole(value, low, high):
    """A whole number clamped to [low, high], or None when it is missing or below low."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):          # text, NaN, infinity, 10**400
        return None
    if number < low:
        return None
    return min(high, number)


class IntentEngine(object):
    """Understands what the user is making."""

    def __init__(self, book, url=LLM_URL, model=LLM_MODEL, label=LLM_LABEL, enabled=True,
                 use_brick=False, api_key=None, timeout_s=LLM_TIMEOUT_S, retry_s=RETRY_FIRST_S):
        self.book = book
        self.system_prompt = build_system_prompt(book)
        self.schema = build_schema(book)
        self.endpoint = chat_endpoint(url) if url else None
        self.model = model
        self.label = label
        self.timeout_s = float(timeout_s)
        self._api_key = api_key
        self._use_brick = bool(use_brick)
        self._brick = None
        self._variant = 0                   # index into _VARIANTS: what this server accepts
        self._available = bool(self.endpoint) or self._use_brick
        self._enabled = bool(enabled) and self._available
        self._lock = threading.Lock()       # status fields
        self._busy = threading.Lock()       # one model request at a time
        self._status = "off"
        self._note = None if self._available else "no local LLM configured"
        self._last_ms = None
        self._fails = 0                     # unusable answers in a row
        self._retry_first_s = float(retry_s)
        self._retry_s = float(retry_s)      # pause before the next try after "failed"
        self._generation = 0

        self._keywords = []
        self._fuzzy = {}
        for recipe in book.all():
            for synonym in recipe["synonyms"]:
                self._keywords.append(_Keyword(recipe["id"], synonym))
                plain = synonym.strip()
                if not any(ch in plain for ch in "*+ "):
                    for form in variants(plain):
                        if len(form) >= 5:
                            self._fuzzy.setdefault(form, recipe["id"])
        self._fuzzy_words = sorted(self._fuzzy)

    # ---- status ------------------------------------------------------------

    def status(self):
        with self._lock:
            return {"status": self._status, "last_ms": self._last_ms, "label": self.label,
                    "enabled": self._enabled, "available": self._available, "note": self._note}

    def will_use_llm(self):
        with self._lock:
            return self._enabled and self._status == "ready"

    def start(self):
        """Warm the model up in a background thread (never blocks the app)."""
        with self._lock:
            if not self._enabled:
                self._status = "off"
                return
            self._generation += 1
            generation = self._generation
            self._status = "loading"
            self._note = None
        thread = threading.Thread(target=self._warm_up, args=(generation,), name="llm-warmup")
        thread.daemon = True
        thread.start()

    def set_enabled(self, enabled):
        with self._lock:
            enabled = bool(enabled) and self._available
            if enabled == self._enabled:
                return
            self._enabled = enabled
            if not enabled:
                self._generation += 1       # cancels a running warm-up and any pending retry
                self._status = "off"
                self._note = None
                return
            self._retry_s = self._retry_first_s
        self.start()

    def _current(self, generation):
        with self._lock:
            return generation == self._generation and self._enabled

    def _set(self, generation, **fields):
        with self._lock:
            if generation != self._generation:
                return
            for key, value in fields.items():
                setattr(self, "_" + key, value)

    def _fail(self, generation, reason):
        """Mark the model failed and try again later: "failed" is never a dead end.
        The pause doubles each time, so a model that is simply too slow costs little."""
        with self._lock:
            if generation != self._generation or not self._enabled:
                return
            delay = self._retry_s
            self._retry_s = min(self._retry_s * 2.0, RETRY_MAX_S)
            self._status = "failed"
            self._fails = 0
            pause = "{:.0f} s".format(delay) if delay < 90 else "{:.0f} min".format(delay / 60.0)
            self._note = "{}; trying again in {}".format(reason, pause)
        log.warning("local LLM: %s; rules only for now, next try in %.0f s", reason, delay)
        timer = threading.Timer(delay, self._retry, args=(generation,))
        timer.daemon = True
        timer.start()

    def _retry(self, generation):
        with self._lock:
            if generation != self._generation or not self._enabled or self._status != "failed":
                return
        self.start()

    # ---- talking to the model ----------------------------------------------

    def _make_brick(self):
        """The App Lab LLM brick. It may be missing (brick removed from app.yaml)."""
        try:
            from arduino.app_bricks.llm import LargeLanguageModel
        except Exception as exc:
            log.info("LLM brick not available: %s", exc)
            return None
        try:
            return LargeLanguageModel(
                system_prompt=self.system_prompt, temperature=0.0, max_tokens=48,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        except Exception as exc:
            log.info("LLM brick refused extra_body (%s), retrying without", exc)
        try:
            return LargeLanguageModel(system_prompt=self.system_prompt, temperature=0.0, max_tokens=48)
        except Exception as exc:
            log.warning("LLM brick failed to start: %s", exc)
            return None

    def _http_chat(self, text, timeout, fields):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_prompt},
                         {"role": "user", "content": text}],
            "temperature": 0,
            "max_tokens": 40,
            "stream": False,
        }
        if "chat_template_kwargs" in fields:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if "response_format" in fields:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "order", "schema": self.schema},
            }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        request = urllib.request.Request(self.endpoint, data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        return data["choices"][0]["message"]["content"] or ""

    def _request(self, text, budget_s):
        """One chat completion within budget_s seconds IN TOTAL, whatever it has to try:
        HTTP first (stepping down to what the server accepts), the brick as a fallback.
        Raises when nothing answers in time."""
        deadline = time.monotonic() + budget_s
        late = TimeoutError("the local LLM took longer than {:.0f} s".format(budget_s))
        error = None
        if self.endpoint:
            variant = self._variant
            while True:
                left = deadline - time.monotonic()
                if left <= 0.05:
                    raise late
                try:
                    content = self._http_chat(text, left, _VARIANTS[variant])
                    self._variant = variant         # remember what worked
                    return content
                except urllib.error.HTTPError as exc:
                    error = exc
                    if 400 <= exc.code < 500 and variant < len(_VARIANTS) - 1:
                        variant += 1                # the server does not know one of our fields
                        continue
                    break
                except Exception as exc:
                    error = exc
                    break
            if _is_timeout(error):
                raise error             # too slow: asking again through the brick will not help
        if self._brick is not None:
            left = deadline - time.monotonic()
            if left <= 0.05:
                raise late
            return self._brick_chat(text, left)
        raise error or RuntimeError("no local LLM configured")

    def _brick_chat(self, text, timeout):
        """Ask the LLM brick, but never wait longer than timeout for it."""
        box = {}

        def work():
            try:
                self._brick.clear_memory()          # conversation memory is on by default
                box["text"] = self._brick.chat(text) or ""
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=work, name="llm-brick")
        thread.daemon = True
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("the LLM brick took longer than {:.0f} s".format(timeout))
        if "error" in box:
            raise box["error"]
        return box["text"]

    def _warm_up(self, generation):
        if self._use_brick and self._brick is None:
            self._brick = self._make_brick()         # can block about 10 s while the runner starts
        # With the brick up the runner is on its way: be patient. Without it, give up sooner.
        deadline = time.monotonic() + (180.0 if self._brick is not None else 45.0)
        problem = None
        while self._current(generation):
            try:
                self._request("2 cups of tea", WARMUP_TIMEOUT_S)     # cold: loads the model
                problem = None
                break
            except Exception as exc:
                problem = str(exc) or exc.__class__.__name__
                if time.monotonic() >= deadline:
                    break
                time.sleep(3.0)
        if not self._current(generation):
            return
        if problem is not None:
            self._fail(generation, "not reachable (" + problem[:100] + ")")
            return
        # Warm: this is what a user will wait for. "Ready" means a usable answer (a JSON order)
        # within the time limit; one slow sample while the board is still busy is forgiven.
        reason = "no usable answer"
        for attempt in range(WARM_SAMPLES):
            started = time.monotonic()
            order = None
            try:
                order = self.extract_order(self._request("pasta for 4", self.timeout_s))
                if order is None:
                    reason = "its answers are not usable"
            except Exception as exc:
                if _is_timeout(exc):
                    reason = "too slow for this board (over {:.0f} s)".format(self.timeout_s)
                else:
                    reason = "request failed ({})".format(str(exc)[:100] or exc.__class__.__name__)
            if not self._current(generation):
                return
            if order is not None:
                took = int((time.monotonic() - started) * 1000)
                log.info("local LLM ready, warm answer in %d ms", took)
                self._set(generation, status="ready", last_ms=took, fails=0, note=None,
                          retry_s=self._retry_first_s)
                return
            if attempt + 1 < WARM_SAMPLES:
                time.sleep(min(2.0, self.timeout_s))
        self._fail(generation, reason)

    def _ask_llm(self, text):
        """Ask the model for an order. Returns a validated dict, or None when it gave nothing usable."""
        started = time.monotonic()
        order = None
        try:
            order = self.extract_order(self._request(text, self.timeout_s))
        except Exception as exc:
            log.warning("local LLM request failed: %s", exc)
        took = int((time.monotonic() - started) * 1000)
        failed_generation = None
        with self._lock:
            self._last_ms = took
            if order is None:
                # No answer, a late answer and an answer without a JSON order all cost the user
                # the wait for nothing. Two in a row: stop asking, try again later.
                self._fails += 1
                if self._fails >= 2 and self._status == "ready":
                    failed_generation = self._generation
            else:
                self._fails = 0
        if failed_generation is not None:
            self._fail(failed_generation, "stopped giving usable answers")
        return order

    def extract_order(self, content):
        """Validate and clamp what the model said. None when there is no JSON object in it."""
        text = re.sub(r"<think>.*?</think>", " ", str(content), flags=re.S)
        match = re.search(r"\{[^{}]*\}", text, flags=re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        recipe = data.get("recipe")
        recipe = recipe.strip().lower() if isinstance(recipe, str) else None
        if self.book.get(recipe) is None:
            recipe = None
        return {
            "recipe": recipe,
            "portions": _whole(data.get("portions"), 1, MAX_PORTIONS),
            "grams": _whole(data.get("grams"), 1, MAX_GRAMS),
        }

    # ---- rules -------------------------------------------------------------

    def find_recipe(self, norm):
        """Best keyword match. Returns (recipe id or None, ids of the recipes that really compete).

        A second recipe competes only when its keyword sits somewhere else in the text
        ("tea and pasta"). A keyword inside the winner's own words does not: the "cup" in
        "cup noodles", the "nudeln" in "Instantnudeln" or in "instant Nudeln".
        """
        tokens = norm.split()
        hits = []                   # (tier, length, -position, recipe id, spans, weak)
        for keyword in self._keywords:
            spans = keyword.find(norm, tokens)
            if spans:
                first = min(start for start, _ in spans)
                hits.append((keyword.tier, keyword.length, -first, keyword.recipe_id, spans, keyword.weak))
        if any(not hit[5] for hit in hits):
            hits = [hit for hit in hits if not hit[5]]      # a measure word next to a real recipe word
        if not hits:                                        # typos: 'spagetti', 'kartofeln'
            position = 0
            for token in tokens:
                if len(token) >= 5:
                    close = difflib.get_close_matches(token, self._fuzzy_words, n=1, cutoff=0.82)
                    if close:
                        hits.append((_TIER_FUZZY, len(close[0]), -position, self._fuzzy[close[0]],
                                     [(position, position + len(token))], False))
                position += len(token) + 1
        if not hits:
            return None, set()
        hits.sort(key=lambda hit: hit[:3], reverse=True)
        best = hits[0]

        def inside_best(spans):
            return all(any(start >= b_start and end <= b_end for b_start, b_end in best[4])
                       for start, end in spans)

        candidates = set([best[3]])
        for hit in hits[1:]:
            if hit[3] != best[3] and not inside_best(hit[4]):
                candidates.add(hit[3])
        return best[3], candidates

    def read(self, text):
        """What the rules see in the text, before any recipe limits are applied."""
        norm = normalize(text)
        recipe_id, candidates = self.find_recipe(norm)
        rest, grams = take_grams(norm)
        number, strong = take_portions(rest)
        return {"recipe": recipe_id, "candidates": candidates, "number": number,
                "strong": strong, "grams": grams}

    def _fit(self, recipe, number, strong, grams):
        """Apply one recipe's limits to the numbers found in the text."""
        portions = None
        uses_grams = self.book.uses_grams(recipe)
        if not uses_grams:
            grams = None
        if number is not None and number >= 0.5:
            if strong or number <= recipe["max_portions"]:
                portions = max(1, min(recipe["max_portions"], int(round(number))))
            elif uses_grams and grams is None and 30 <= number <= MAX_GRAMS:
                grams = number                          # "300 pasta" can only mean grams
        return portions, RecipeBook.clamp_grams(grams)

    def rules(self, text):
        """Rules only. Instant."""
        seen = self.read(text)
        recipe = self.book.get(seen["recipe"])
        if recipe is None:
            return {"recipe": None, "portions": None, "grams": None, "source": "rules"}
        portions, grams = self._fit(recipe, seen["number"], seen["strong"], seen["grams"])
        return {"recipe": recipe["id"], "portions": portions, "grams": grams, "source": "rules"}

    # ---- the public entry point --------------------------------------------

    def parse(self, text):
        """LLM first when it is ready, rules otherwise. Never raises."""
        started = time.monotonic()
        text = str(text or "").strip()[:MAX_TEXT]
        try:
            result = self._parse(text)
        except Exception:
            log.exception("parse failed, using the rules alone")
            try:
                result = self.rules(text)
            except Exception:
                result = {"recipe": None, "portions": None, "grams": None, "source": "rules"}
        result["ms"] = int((time.monotonic() - started) * 1000)
        return result

    def _parse(self, text):
        seen = self.read(text)
        answer = None
        if text and self.will_use_llm() and self._busy.acquire(False):
            try:
                answer = self._ask_llm(text)
            except Exception:                       # whatever the model side does, the rules still answer
                log.exception("local LLM step failed")
                answer = None
            finally:
                self._busy.release()

        rules_recipe = self.book.get(seen["recipe"])
        llm_recipe = self.book.get(answer["recipe"]) if answer else None
        use_llm = llm_recipe is not None
        if use_llm and rules_recipe is not None and llm_recipe is not rules_recipe:
            # One clear keyword beats the model. With several candidates the model may choose.
            candidates = seen["candidates"]
            use_llm = len(candidates) > 1 and llm_recipe["id"] in candidates

        if not use_llm:
            if rules_recipe is None:
                return {"recipe": None, "portions": None, "grams": None, "source": "rules"}
            portions, grams = self._fit(rules_recipe, seen["number"], seen["strong"], seen["grams"])
            return {"recipe": rules_recipe["id"], "portions": portions, "grams": grams, "source": "rules"}

        number, strong = seen["number"], seen["strong"]
        if answer["portions"] is not None and not strong:
            number, strong = float(answer["portions"]), True     # "me and my two kids" -> 3
        portions, grams = self._fit(llm_recipe, number, strong, seen["grams"])
        return {"recipe": llm_recipe["id"], "portions": portions, "grams": grams, "source": "llm"}
