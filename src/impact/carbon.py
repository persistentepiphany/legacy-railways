"""Modal-shift carbon block: what the demand block's NET-NEW rail
journeys mean in kgCO2e, if they shift from the average car.

ESTIMATE — the arithmetic is exact, the inputs are the estimates:

  carbon_delta_kg = net_new_journeys x distance_km
                    x (car_per_passenger_km - rail_per_passenger_km)

  - net_new_journeys comes ONLY from the demand block (abstracted
    passengers were already on the train — no modal shift, no carbon
    claim). No demand volumes → no total, and the block says why.
  - distance is the demand block's per-flow figure (method recorded
    there: rgd_shortest_path | great_circle_x1.2).
  - the rail factor is blended from the corridor's actual traction mix
    (CIF BS power types) when a timetable is on disk; otherwise the
    DEFRA national average, disclosed.
  - the car factor is DEFRA per-vehicle-km / average occupancy.

Independently of demand volumes, the block always carries the CORRIDOR
PER-PASSENGER figures (rail kg, car kg, saving per journey) — these
need only a distance and the factors, and they are what the C3
validation gate compares against the National Rail carbon calculator.

Every factor is a named, cited constant in src/impact/carbon_factors.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.impact.carbon_factors import (
    LOAD_FACTOR_ASSUMPTION,
    RAIL_NATIONAL_KGCO2E_PER_PKM,
    car_factor_per_passenger_km,
    rail_factor_for_mix,
)
from src.impact.demand import DemandBlock
from src.impact.distance import flow_distance_km
from src.impact.feed_paths import FeedPaths

METHODOLOGY_NOTE = (
    "ESTIMATE — modal-shift carbon: net-new rail journeys (from the demand "
    "block) x distance x (car - rail) DEFRA/DESNZ 2025 emission factors. "
    "Assumes every net-new rail journey displaces an average-car journey of "
    "the same distance — the standard modal-shift framing, disclosed here "
    "because some net-new travel is genuinely new, not shifted."
)


@dataclass(frozen=True)
class CarbonEstimate:
    """Per-flow carbon delta. Only flows where the demand block produced
    absolute net-new journeys AND a distance appear here."""
    flow_id: str
    ticket_code: str
    net_new_journeys: int
    distance_km: float
    distance_method: str
    rail_kgco2e_per_pkm: float
    car_kgco2e_per_pkm: float
    carbon_saving_kg: float        # positive = net CO2e avoided
    notes: tuple[str, ...]


@dataclass(frozen=True)
class CarbonBlock:
    """ESTIMATE — see METHODOLOGY_NOTE (always notes[0])."""
    estimates: tuple[CarbonEstimate, ...]
    total_carbon_saving_kg: float | None   # None when demand had no volumes
    # Corridor per-passenger figures — always populated when the corridor
    # distance is derivable; independent of ODM/demand volumes.
    corridor_distance_km: float | None
    corridor_distance_method: str | None
    corridor_rail_kg_per_passenger: float | None
    corridor_car_kg_per_passenger: float | None
    corridor_saving_kg_per_passenger: float | None
    rail_factor_kgco2e_per_pkm: float
    rail_factor_description: str
    car_factor_kgco2e_per_pkm: float
    traction_electric_pct: float | None
    traction_diesel_pct: float | None
    notes: tuple[str, ...]


def _corridor_rail_factor(
    feed_paths: FeedPaths,
    origin_crs: str | None,
    dest_crs: str | None,
) -> tuple[float, str, float | None, float | None, list[str]]:
    """Rail factor blended from the corridor's real traction mix when a
    timetable is available; DEFRA national average otherwise. Returns
    (factor, description, electric_pct, diesel_pct, notes)."""
    notes: list[str] = []
    mca = feed_paths.timetable_mca
    if mca is None or not mca.exists() or not origin_crs or not dest_crs:
        notes.append(
            "no timetable (or corridor CRS) available for a traction mix; "
            "rail factor = DEFRA national average "
            f"{RAIL_NATIONAL_KGCO2E_PER_PKM} kgCO2e/pkm."
        )
        return (RAIL_NATIONAL_KGCO2E_PER_PKM,
                "DEFRA/DESNZ 2025 national-rail average (no traction mix)",
                None, None, notes)

    from src.ingest.timetable import load_timetable_index, traction_mix

    idx = load_timetable_index(mca)
    mix = traction_mix(idx, origin_crs, dest_crs)
    notes.extend(mix.notes)
    if mix.train_count == 0:
        notes.append(
            f"no trains found serving {origin_crs}->{dest_crs} in the "
            "timetable; rail factor = DEFRA national average."
        )
        return (RAIL_NATIONAL_KGCO2E_PER_PKM,
                "DEFRA/DESNZ 2025 national-rail average (corridor not in timetable)",
                None, None, notes)

    factor, desc = rail_factor_for_mix(
        mix.electric_pct, mix.diesel_pct, mix.unknown_pct)
    notes.append(
        f"traction mix over {mix.train_count} corridor trains: "
        f"{mix.electric_pct:.0%} electric / {mix.diesel_pct:.0%} diesel"
        + (f" / {mix.unknown_pct:.0%} unknown" if mix.unknown_pct > 0 else "")
        + "."
    )
    return factor, desc, mix.electric_pct, mix.diesel_pct, notes


def compute_carbon(
    demand: DemandBlock,
    feed_paths: FeedPaths,
    corridor_origin_crs: str | None,
    corridor_dest_crs: str | None,
    *,
    msn_path=None,
) -> CarbonBlock:
    """Build the carbon block from an already-computed demand block.

    The demand dependency is structural: net-new journeys are the ONLY
    volume this block will multiply (abstraction is not modal shift).
    `msn_path` is injectable for tests; defaults next to the feed."""
    if msn_path is None:
        from src.api.geo import default_msn_path
        msn_path = default_msn_path(feed_paths.loc.parent)

    notes: list[str] = [METHODOLOGY_NOTE]
    notes.append(f"load factor: {LOAD_FACTOR_ASSUMPTION}.")

    rail_factor, rail_desc, elec_pct, diesel_pct, factor_notes = (
        _corridor_rail_factor(feed_paths, corridor_origin_crs, corridor_dest_crs)
    )
    notes.extend(factor_notes)
    car_factor = car_factor_per_passenger_km()

    # --- corridor per-passenger figures (volume-independent) -------------
    corridor_km = corridor_method = None
    rail_pp = car_pp = saving_pp = None
    if corridor_origin_crs and corridor_dest_crs:
        dist = flow_distance_km(
            corridor_origin_crs, corridor_dest_crs,
            rgd_path=feed_paths.rgd, msn_path=msn_path)
        if dist is not None:
            corridor_km = dist.km
            corridor_method = dist.method
            rail_pp = round(dist.km * rail_factor, 3)
            car_pp = round(dist.km * car_factor, 3)
            saving_pp = round(car_pp - rail_pp, 3)
            notes.append(
                f"corridor distance {dist.km} km via {dist.method}; "
                f"per-passenger: rail {rail_pp} kg, car {car_pp} kg, "
                f"saving {saving_pp} kgCO2e per shifted journey."
            )
        else:
            notes.append(
                "corridor distance underivable (no .RGD path and no .MSN "
                "coords) — per-passenger figures unavailable."
            )
    else:
        notes.append(
            "corridor endpoints have no CRS — per-passenger figures "
            "unavailable."
        )

    # --- per-flow totals (demand-volume-dependent) ------------------------
    estimates: list[CarbonEstimate] = []
    skipped_no_distance = 0
    for est in demand.estimates:
        if est.net_new_journeys is None:
            continue
        if est.distance_km is None or est.distance_method is None:
            skipped_no_distance += 1
            continue
        saving = est.net_new_journeys * est.distance_km * (car_factor - rail_factor)
        estimates.append(CarbonEstimate(
            flow_id=est.flow_id,
            ticket_code=est.ticket_code,
            net_new_journeys=est.net_new_journeys,
            distance_km=est.distance_km,
            distance_method=est.distance_method,
            rail_kgco2e_per_pkm=rail_factor,
            car_kgco2e_per_pkm=round(car_factor, 5),
            carbon_saving_kg=round(saving, 1),
            notes=(
                (f"net-new journeys are extrapolation beyond the elasticity "
                 f"validity band — treat as order of magnitude.",)
                if not est.within_validity else ()
            ),
        ))

    if estimates:
        total = round(sum(e.carbon_saving_kg for e in estimates), 1)
    else:
        total = None
        if demand.total_net_new_journeys is None:
            notes.append(
                "no total: the demand block is percentage-only (no ODM "
                "volumes), so there are no net-new journeys to multiply. "
                "Per-passenger corridor figures above still hold."
            )
    if skipped_no_distance:
        notes.append(
            f"{skipped_no_distance} flow(s) had net-new journeys but no "
            "derivable distance; excluded from the total, never guessed."
        )

    return CarbonBlock(
        estimates=tuple(estimates),
        total_carbon_saving_kg=total,
        corridor_distance_km=corridor_km,
        corridor_distance_method=corridor_method,
        corridor_rail_kg_per_passenger=rail_pp,
        corridor_car_kg_per_passenger=car_pp,
        corridor_saving_kg_per_passenger=saving_pp,
        rail_factor_kgco2e_per_pkm=round(rail_factor, 5),
        rail_factor_description=rail_desc,
        car_factor_kgco2e_per_pkm=round(car_factor, 5),
        traction_electric_pct=elec_pct,
        traction_diesel_pct=diesel_pct,
        notes=tuple(notes),
    )


__all__ = [
    "CarbonBlock",
    "CarbonEstimate",
    "METHODOLOGY_NOTE",
    "compute_carbon",
]
