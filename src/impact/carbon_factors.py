"""DEFRA/DESNZ greenhouse-gas conversion factors for modal-shift carbon
estimates, hand-encoded as named constants with citations.

Values are transcribed from the UK Government GHG Conversion Factors for
Company Reporting (DESNZ/DEFRA, 2025 set, "business travel — land"
tables) and the National Rail carbon-calculator substantiation. Nothing
here is computed or estimated by us except the two DERIVED traction
split-outs, whose calibration is written out below.

The carbon module multiplies these by net-new journeys x distance; the
C1 validation gate reproduces the published worked example arithmetic
from these exact constants.
"""

from __future__ import annotations

# National rail average emission factor.
# DESNZ/DEFRA GHG Conversion Factors 2025, business travel — land,
# "National rail": 0.03546 kgCO2e per passenger-km.
RAIL_NATIONAL_KGCO2E_PER_PKM: float = 0.03546

# Average car emission factor (per VEHICLE km, unknown fuel).
# DESNZ/DEFRA GHG Conversion Factors 2025, business travel — land,
# "Average car, unknown fuel": 0.16743 kgCO2e per vehicle-km.
CAR_KGCO2E_PER_VKM: float = 0.16743

# Average car occupancy used to turn the per-vehicle factor into a
# per-passenger factor. The National Rail carbon-calculator
# substantiation (37.3 km worked example: car = 4.14 kgCO2e) implies
# occupancy ~1.5, consistent with the National Travel Survey average
# (~1.5-1.6 persons/car).
CAR_AVG_OCCUPANCY: float = 1.5

# Electric vs diesel rail split-out. DEFRA publishes only the national
# average above, not per-traction factors. DERIVED pair, calibrated so
# that the blend reproduces the DEFRA national average at the published
# national traction split (ORR infrastructure/usage statistics: ~70% of
# GB passenger-km on electric traction):
#   0.70 * 0.026 + 0.30 * 0.058 = 0.0356 ≈ 0.03546 (DEFRA national avg)
# Magnitudes sit inside the ranges quoted in RSSB/industry literature
# (electric ~0.025-0.035, diesel ~0.055-0.09 kgCO2e/pkm). Marked DERIVED;
# the carbon block's notes must say which factor (or blend) was used.
RAIL_ELECTRIC_KGCO2E_PER_PKM: float = 0.026   # DERIVED — see calibration above
RAIL_DIESEL_KGCO2E_PER_PKM: float = 0.058     # DERIVED — see calibration above
ELECTRIC_SHARE_NATIONAL: float = 0.70         # ORR usage stats, approx.

# Load-factor assumption, named so every carbon note can cite it: the
# DEFRA per-passenger-km factors already embed NATIONAL-AVERAGE train
# loading. We apply them flat — no service-specific or time-of-day
# crowding adjustment. This is the main reason our per-passenger figures
# can differ from the National Rail calculator, which uses per-service
# loadings (validation gate C3 band is set at ±40% for exactly this).
LOAD_FACTOR_ASSUMPTION: str = (
    "flat national-average train loading as embedded in the DEFRA/DESNZ "
    "per-passenger-km factors; no service-specific load adjustment"
)


def rail_factor_for_mix(electric_pct: float, diesel_pct: float,
                        unknown_pct: float) -> tuple[float, str]:
    """Blend the per-traction rail factors for a corridor's traction mix.

    Percentages are fractions summing to ~1.0. The unknown share falls
    back to the DEFRA national average — the honest neutral choice, and
    it is disclosed in the returned description string."""
    factor = (
        electric_pct * RAIL_ELECTRIC_KGCO2E_PER_PKM
        + diesel_pct * RAIL_DIESEL_KGCO2E_PER_PKM
        + unknown_pct * RAIL_NATIONAL_KGCO2E_PER_PKM
    )
    desc = (
        f"rail factor {factor:.5f} kgCO2e/pkm = blend of electric "
        f"{electric_pct:.0%} x {RAIL_ELECTRIC_KGCO2E_PER_PKM} + diesel "
        f"{diesel_pct:.0%} x {RAIL_DIESEL_KGCO2E_PER_PKM}"
        + (f" + unknown {unknown_pct:.0%} x national avg "
           f"{RAIL_NATIONAL_KGCO2E_PER_PKM}" if unknown_pct > 0 else "")
        + " (electric/diesel factors DERIVED — calibrated to DEFRA national avg)"
    )
    return factor, desc


def car_factor_per_passenger_km() -> float:
    """Per-passenger car factor: DEFRA per-vehicle-km / average occupancy."""
    return CAR_KGCO2E_PER_VKM / CAR_AVG_OCCUPANCY


__all__ = [
    "CAR_AVG_OCCUPANCY",
    "CAR_KGCO2E_PER_VKM",
    "ELECTRIC_SHARE_NATIONAL",
    "LOAD_FACTOR_ASSUMPTION",
    "RAIL_DIESEL_KGCO2E_PER_PKM",
    "RAIL_ELECTRIC_KGCO2E_PER_PKM",
    "RAIL_NATIONAL_KGCO2E_PER_PKM",
    "car_factor_per_passenger_km",
    "rail_factor_for_mix",
]
