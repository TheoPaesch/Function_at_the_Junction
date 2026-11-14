"""Energy and savings maths.

heat_kwh(ml)  = ml / 1000 * 4.186 * (100 - t_start_c) / 3600
elec_kwh(ml)  = heat_kwh(ml) / efficiency       (kettle: eff_kettle, pot: eff_stove)
saved_ml      = max(0, baseline_ml - dispensed_ml)
saved_kwh     = elec_kwh(saved_ml)
saved_eur     = saved_kwh * price_eur_per_kwh
saved_seconds = saved_kwh * 3600 / kettle_kw    (kettle recipes only)

Sanity check: 500 ml saved in a kettle is about 58 Wh, about 105 s, about 0.023 EUR.
"""

SPECIFIC_HEAT_KJ_PER_KG_K = 4.186
BOILING_C = 100.0


def heat_kwh(ml, t_start_c=15.0):
    """Heat needed to bring ml of water from t_start_c to the boil, in kWh."""
    return float(ml) / 1000.0 * SPECIFIC_HEAT_KJ_PER_KG_K * (BOILING_C - float(t_start_c)) / 3600.0


def elec_kwh(ml, efficiency, t_start_c=15.0):
    """Electricity drawn from the socket for that heat, in kWh."""
    return heat_kwh(ml, t_start_c) / max(float(efficiency), 0.01)


def efficiency_for(vessel, settings):
    return float(settings["eff_kettle"] if vessel == "kettle" else settings["eff_stove"])


def savings(vessel, baseline_ml, dispensed_ml, settings):
    """Savings of one run compared with the normal habit (baseline)."""
    saved_ml = max(0.0, float(baseline_ml) - float(dispensed_ml))
    eff = efficiency_for(vessel, settings)
    t_start = settings["t_start_c"]
    saved_kwh = elec_kwh(saved_ml, eff, t_start)
    seconds = None
    if vessel == "kettle":
        seconds = int(round(saved_kwh * 3600.0 / max(float(settings["kettle_kw"]), 0.1)))
    return {
        "saved_ml": int(round(saved_ml)),
        "saved_kwh": saved_kwh,
        "saved_wh": round(saved_kwh * 1000.0, 1),
        "saved_eur": round(saved_kwh * float(settings["price_eur_per_kwh"]), 4),
        "saved_seconds": seconds,
        "used_wh": round(elec_kwh(dispensed_ml, eff, t_start) * 1000.0, 1),
    }
