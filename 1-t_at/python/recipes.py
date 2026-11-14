"""Recipes and water maths. Everything here is driven by recipes.json.

The phone UI, the free-text parser and the state machine all read the same
RecipeBook, so editing recipes.json changes every part of the app at once.
1 ml of water is treated as 1 g on the scale.
"""

import json
import math
import os

MAX_ML = 4000          # hard cap for one dispense
ROUND_ML = 10          # water is rounded to the nearest 10 ml
MAX_GRAMS = 5000       # largest ingredient weight we accept

VESSELS = ("kettle", "pot")
WATER_TYPES = ("per_portion", "per_gram", "per_gram_plus")
BASELINE_TYPES = ("factor", "ml_per_g")
UNIT_FORMS = {
    "cup": ("cup", "cups"),
    "pack": ("pack", "packs"),
    "person": ("person", "people"),
}

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recipes.json")


class RecipeError(ValueError):
    """recipes.json is missing a field or holds a value we cannot use."""


def round_ml(ml):
    """Round half up to the nearest 10 ml."""
    return int(math.floor(float(ml) / ROUND_ML + 0.5)) * ROUND_ML


def fmt_ml(ml):
    """1300 -> '1,300 ml'."""
    return "{:,} ml".format(int(round(ml)))


def fmt_water(ml):
    """500 -> '500 ml', 2000 -> '2.0 L' (used on the confirm card)."""
    ml = int(round(ml))
    if ml >= 1000:
        return "{:.1f} L".format(ml / 1000.0)
    return "{} ml".format(ml)


def _need(cond, recipe_id, text):
    if not cond:
        raise RecipeError("recipes.json, recipe '{}': {}".format(recipe_id, text))


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate(recipe):
    rid = recipe.get("id") if isinstance(recipe, dict) else None
    _need(isinstance(recipe, dict), rid, "must be an object")
    _need(isinstance(rid, str) and rid and rid != "unknown", rid, "needs an id (not 'unknown')")
    for key in ("name", "emoji", "tip", "baseline_note"):
        _need(isinstance(recipe.get(key), str) and recipe[key], rid, "needs text for '{}'".format(key))
    _need(recipe.get("vessel") in VESSELS, rid, "vessel must be kettle or pot")
    _need(recipe.get("unit") in UNIT_FORMS, rid, "unit must be cup, pack or person")
    _need(isinstance(recipe.get("max_portions"), int) and 1 <= recipe["max_portions"] <= 99,
          rid, "max_portions must be a whole number")
    _need(isinstance(recipe.get("default_portions"), int)
          and 1 <= recipe["default_portions"] <= recipe["max_portions"],
          rid, "default_portions must be between 1 and max_portions")
    _need(isinstance(recipe.get("weigh_in_pot"), bool), rid, "weigh_in_pot must be true or false")

    water = recipe.get("water")
    _need(isinstance(water, dict) and water.get("type") in WATER_TYPES, rid,
          "water.type must be one of {}".format(", ".join(WATER_TYPES)))
    if water["type"] == "per_portion":
        _need(_is_number(water.get("ml_per_portion")) and water["ml_per_portion"] > 0,
              rid, "water.ml_per_portion must be a positive number")
    else:
        _need(_is_number(water.get("ml_per_g")) and water["ml_per_g"] > 0,
              rid, "water.ml_per_g must be a positive number")
        _need(_is_number(recipe.get("grams_per_portion")) and recipe["grams_per_portion"] > 0,
              rid, "grams_per_portion must be a positive number for per-gram recipes")
    for key in ("plus_ml", "min_ml"):
        _need(key not in water or (_is_number(water[key]) and water[key] >= 0),
              rid, "water.{} must be a number".format(key))
    _need(not recipe["weigh_in_pot"] or water["type"] != "per_portion", rid,
          "weigh_in_pot needs a per-gram water rule")

    baseline = recipe.get("baseline")
    _need(isinstance(baseline, dict) and baseline.get("type") in BASELINE_TYPES
          and _is_number(baseline.get("value")) and baseline["value"] > 0,
          rid, "baseline needs type (factor or ml_per_g) and a positive value")
    _need(baseline["type"] != "ml_per_g" or water["type"] != "per_portion", rid,
          "an ml_per_g baseline needs a per-gram water rule")

    synonyms = recipe.get("synonyms")
    _need(isinstance(synonyms, list) and synonyms and all(isinstance(s, str) and s.strip() for s in synonyms),
          rid, "synonyms must be a list of words")
    return recipe


class RecipeBook(object):
    """The recipes from recipes.json plus the water and baseline maths."""

    def __init__(self, recipes):
        if isinstance(recipes, dict):
            recipes = recipes.get("recipes")
        if not isinstance(recipes, list) or not recipes:
            raise RecipeError("recipes.json must hold a list of recipes")
        self._recipes = [_validate(r) for r in recipes]
        self._by_id = {}
        for recipe in self._recipes:
            if recipe["id"] in self._by_id:
                raise RecipeError("recipes.json: duplicate id '{}'".format(recipe["id"]))
            self._by_id[recipe["id"]] = recipe

    @classmethod
    def load(cls, path=None):
        with open(path or DEFAULT_PATH, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    # ---- lookup ------------------------------------------------------------

    def all(self):
        return list(self._recipes)

    def ids(self):
        return [r["id"] for r in self._recipes]

    def get(self, recipe_id):
        return self._by_id.get(recipe_id) if isinstance(recipe_id, str) else None

    def names_sentence(self):
        """'tea, instant noodles, pasta or potatoes' for the 'sorry' message."""
        names = [r["name"].lower() for r in self._recipes]
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " or " + names[-1]

    # ---- clamping ----------------------------------------------------------

    def clamp_portions(self, recipe, portions):
        """A whole number of portions between 1 and max_portions (default if missing)."""
        if isinstance(portions, bool):
            return recipe["default_portions"]
        try:
            value = int(round(float(portions)))
        except (TypeError, ValueError, OverflowError):      # None, text, NaN, infinity, 10**400
            return recipe["default_portions"]
        return max(1, min(recipe["max_portions"], value))

    @staticmethod
    def clamp_grams(grams):
        """Whole grams between 1 and MAX_GRAMS, or None when no weight was given."""
        if isinstance(grams, bool):
            return None
        try:
            value = int(round(float(grams)))
        except (TypeError, ValueError, OverflowError):      # None, text, NaN, infinity, 10**400
            return None
        if value <= 0:
            return None
        return min(MAX_GRAMS, value)

    # ---- maths -------------------------------------------------------------

    def uses_grams(self, recipe):
        return recipe["water"]["type"] != "per_portion"

    def basis_grams(self, recipe, portions, grams=None, weighed_g=None):
        """Ingredient weight the water rule works from, or None for per-portion recipes."""
        if not self.uses_grams(recipe):
            return None
        if recipe["weigh_in_pot"] and weighed_g is not None:
            return max(0.0, float(weighed_g))
        if grams:
            return float(grams)
        return float(portions) * float(recipe["grams_per_portion"])

    def water_ml(self, recipe, portions, grams=None, weighed_g=None, kettle_min_ml=0):
        """Water to dispense, rounded to 10 ml and capped at MAX_ML."""
        water = recipe["water"]
        if water["type"] == "per_portion":
            ml = float(water["ml_per_portion"]) * float(portions)
        else:
            ml = float(water["ml_per_g"]) * self.basis_grams(recipe, portions, grams, weighed_g)
            if water["type"] == "per_gram_plus":
                ml += float(water.get("plus_ml", 0))
        ml = max(ml, float(water.get("min_ml", 0)))
        if recipe["vessel"] == "kettle":
            ml = max(ml, float(kettle_min_ml or 0))
        return min(MAX_ML, max(ROUND_ML, round_ml(ml)))

    def baseline_value(self, recipe, overrides=None):
        """The 'normal habit' number: recipes.json unless the settings screen overrides it."""
        value = (overrides or {}).get(recipe["id"])
        if _is_number(value) and value > 0:
            return float(value)
        return float(recipe["baseline"]["value"])

    def baseline_ml(self, recipe, needed_ml, basis_g=None, overrides=None):
        """Water a person would normally heat for the same dish."""
        value = self.baseline_value(recipe, overrides)
        if recipe["baseline"]["type"] == "ml_per_g":
            return int(round(value * float(basis_g or 0)))
        return int(round(value * float(needed_ml)))

    # ---- wording -----------------------------------------------------------

    def portion_label(self, recipe, portions):
        """'1 cup', '4 people', '3 packs'."""
        one, many = UNIT_FORMS[recipe["unit"]]
        return "{} {}".format(portions, one if portions == 1 else many)

    def amount_label(self, recipe, portions, grams=None):
        """What the user asked for: a weight when they gave one, else portions."""
        if grams and self.uses_grams(recipe):
            return "{:,} g".format(int(grams))
        return self.portion_label(recipe, portions)

    def order_line(self, recipe, portions, grams=None, kettle_min_ml=0):
        """'Pasta · 4 people · 2.0 L water' for the confirm card."""
        ml = self.water_ml(recipe, portions, grams, kettle_min_ml=kettle_min_ml)
        about = "about " if recipe["weigh_in_pot"] else ""
        return "{} · {} · {}{} water".format(
            recipe["name"], self.amount_label(recipe, portions, grams), about, fmt_water(ml))

    def public(self, settings=None):
        """Recipe list for GET /api/recipes: the JSON fields plus ready-made amounts."""
        settings = settings or {}
        overrides = settings.get("baselines") or {}
        kettle_min = settings.get("kettle_min_ml", 0)
        out = []
        for recipe in self._recipes:
            one, many = UNIT_FORMS[recipe["unit"]]
            amounts = [self.water_ml(recipe, n, kettle_min_ml=kettle_min)
                       for n in range(1, recipe["max_portions"] + 1)]
            out.append({
                "id": recipe["id"],
                "name": recipe["name"],
                "emoji": recipe["emoji"],
                "vessel": recipe["vessel"],
                "unit": recipe["unit"],
                "unit_one": one,
                "unit_many": many,
                "default_portions": recipe["default_portions"],
                "max_portions": recipe["max_portions"],
                "water": dict(recipe["water"]),
                "grams_per_portion": recipe.get("grams_per_portion"),
                "weigh_in_pot": recipe["weigh_in_pot"],
                "baseline": {
                    "type": recipe["baseline"]["type"],
                    "value": self.baseline_value(recipe, overrides),
                    "default": float(recipe["baseline"]["value"]),
                },
                "baseline_note": recipe["baseline_note"],
                "tip": recipe["tip"],
                "note": recipe.get("note"),
                "amounts_ml": amounts,
            })
        return out
