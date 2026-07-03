"""Demand-shift block: elasticity response of journeys to a fares change.

ESTIMATE — PDFH-framework structure with published elasticity values,
NOT a calibrated forecast (the calibrated tools — MOIRA, EDGE — sit on
walled data). What this block does claim:

  - the DIRECTION of the demand response, from the sign of the change;
  - an order-of-magnitude MAGNITUDE, from published own-price
    elasticities (src/impact/elasticities.py, each value cited);
  - a clean split of gross product take-up into ABSTRACTION (existing
    passengers switching ticket — no new travel) vs NET-NEW journeys.

Method, per canonical affected fare:

  1. Classify the flow into a PDFH segment: London flow (reusing the
     compliance module's London-terminals inference) x distance band
     (from src/impact/distance.py) x season/non-season (.TTY TKT_TYPE —
     'N' = season per RSPS5045 §4.6).
  2. Price ratio P_new/P_old. Base = ODM implied yield (revenue/journeys)
     when the ODM carries revenue, else the resolver's own old price.
     The basis is recorded per flow.
  3. Multiplicative PDFH form:  D_new/D_old = (P_new/P_old) ** eps —
     never the linear approximation, which overstates large changes.
  4. eps chosen by (segment, INCREASE|REDUCTION). Reductions route to
     the much weaker published reduction-side elasticities.
  5. Abstraction: a new discounted product is adopted by the eligible
     share of EXISTING full-fare passengers on the flow — they were
     already travelling, so they contribute revenue dilution, not new
     journeys. net_new = gross_product_volume - abstracted, and
     abstracted can never exceed the existing volume (it is a share of
     it by construction).
  6. Validity: published elasticities are estimated on small changes;
     beyond +/-25% the response is extrapolation, so the row is marked
     within_validity=False and a warning note is emitted.

Absolute journey counts appear only where the ODM covers the flow;
otherwise the row is percentage-only and says so. No ODM at all →
every row is percentage-only and the block-level note explains how to
upgrade (drop an ORR ODM at data/odm/odm.csv).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.impact.affected import AffectedSet
from src.impact.compliance import _infer_london_flow  # single source of the London rule
from src.impact.distance import flow_distance_km
from src.impact.elasticities import (
    CROSS_ELASTICITY_SOURCE,
    CROSS_ELASTICITY_TICKET_SWITCH_RANGE,
    Direction,
    FlowType,
    TicketSegment,
    lookup_elasticity,
)
from src.impact.feed_paths import FeedPaths
from src.impact.odm import ODMIndex
from src.ingest.inspect import load_loc_meta, load_ticket_type_meta

# PDFH distance banding (public convention): "long distance" starts at
# ~100 miles; everything below is network/short-distance for our 4-way
# segmentation. Named so the classification is auditable per flow.
LONG_DISTANCE_MIN_KM = 160.0

# Share of a flow's existing journeys eligible for the new discounted
# product. ASSUMPTION (order of magnitude from National Travel Survey
# age-band trip shares for a student-type demographic) — a named knob,
# not an estimate. Every absolute journey figure downstream scales
# linearly with it, and the block notes say so. The D2 invariants hold
# for any value in (0, 1].
ELIGIBLE_SHARE_ASSUMPTION = 0.15

# Beyond this |price change|, published elasticities are extrapolation.
VALIDITY_BOUND_PCT = 25.0

METHODOLOGY_NOTE = (
    "ESTIMATE — PDFH-framework structure with published elasticity values "
    "(each cell cited in src/impact/elasticities.py), not a calibrated "
    "forecast. Direction and order of magnitude only."
)


@dataclass(frozen=True)
class DemandEstimate:
    """Per-canonical-fare demand response. String fields carry the enum
    values so the Pydantic mirror stays plain."""
    flow_id: str
    ticket_code: str
    flow_type: str                 # FlowType value
    ticket_segment: str            # season | non_season
    direction: str                 # increase | reduction | none
    elasticity: float
    elasticity_source: str
    elasticity_derived: bool
    price_base_pence: int          # denominator of the ratio
    yield_basis: str               # odm_yield | resolved_fare
    price_ratio: float             # P_new / P_base
    price_change_pct: float
    gross_demand_change_pct: float # (ratio**eps - 1) * 100
    within_validity: bool
    distance_km: float | None
    distance_method: str | None
    # Absolute volumes — None when the ODM doesn't cover the flow.
    odm_journeys_per_period: int | None
    eligible_base_journeys: int | None
    gross_product_journeys: int | None
    abstracted_journeys: int | None
    net_new_journeys: int | None
    notes: tuple[str, ...]


@dataclass(frozen=True)
class DemandBlock:
    """ESTIMATE — see METHODOLOGY_NOTE (always notes[0])."""
    estimates: tuple[DemandEstimate, ...]
    total_net_new_journeys: int | None   # None when NO flow had ODM coverage
    flows_with_volume: int               # rows carrying absolute journeys
    flows_percent_only: int
    validity_warnings: int
    eligible_share_assumption: float
    notes: tuple[str, ...]


def _classify_flow_type(
    is_london: bool, distance_km: float | None,
) -> tuple[FlowType, str | None]:
    """Map (London?, distance) to the PDFH segment. Unknown distance
    defaults to the LESS price-sensitive segment on that side of the
    London split — the conservative choice — and says so."""
    if distance_km is None:
        ft = FlowType.NETWORK_LONDON if is_london else FlowType.SHORT_DISTANCE
        return ft, (
            "distance unavailable for this flow; defaulted to the less "
            f"price-sensitive segment ({ft.value}) — conservative."
        )
    if is_london:
        ft = (FlowType.LD_LONDON if distance_km >= LONG_DISTANCE_MIN_KM
              else FlowType.NETWORK_LONDON)
    else:
        ft = (FlowType.LD_NON_LONDON if distance_km >= LONG_DISTANCE_MIN_KM
              else FlowType.SHORT_DISTANCE)
    return ft, None


def _crs_for(nlc: str, loc, cache: dict[str, str | None]) -> str | None:
    """CRS for an NLC, falling back to a member station when the NLC is a
    cluster/group (e.g. 1072 London BR has no CRS of its own) — the same
    representative-member rule as report._corridor_crses."""
    if nlc in cache:
        return cache[nlc]
    crs = None
    meta = loc.get(nlc)
    if meta is not None and meta.crs:
        crs = meta.crs
    else:
        for m in loc.values():
            if m.group_nlc == nlc and m.crs:
                crs = m.crs
                break
    cache[nlc] = crs
    return crs


def compute_demand(
    affected_set: AffectedSet,
    feed_paths: FeedPaths,
    odm: ODMIndex | None,
    *,
    eligible_share: float | None = None,
) -> DemandBlock:
    """Build the demand block over the canonical affected set. Pure and
    deterministic; the only inputs are the feed, the change, the (optional)
    ODM and the (optional) eligible-share override.

    `eligible_share` replaces ELIGIBLE_SHARE_ASSUMPTION when given — the
    analyst's knob, still disclosed in the block (eligible_share_assumption
    always reports the value actually used). Range (0, 1] enforced at the
    API boundary; the D2 invariants hold for any value in that range."""
    share = ELIGIBLE_SHARE_ASSUMPTION if eligible_share is None else eligible_share
    # Deferred: importing src.api.geo at module level triggers the
    # src.api.__init__ -> main -> schemas -> src.impact.report cycle.
    from src.api.geo import default_msn_path

    loc = load_loc_meta(feed_paths.loc)
    tty = load_ticket_type_meta(feed_paths.tty)
    msn = default_msn_path(feed_paths.loc.parent)

    estimates: list[DemandEstimate] = []
    crs_cache: dict[str, str | None] = {}
    flows_with_volume = 0
    flows_percent_only = 0
    validity_warnings = 0
    any_odm_row = False

    for fare in affected_set.canonical:
        if fare.old_price_pence is None or fare.new_price_pence is None:
            continue  # non-resolved rows carry no priced change
        row_notes: list[str] = []

        # --- price base -------------------------------------------------
        price_base = fare.old_price_pence
        yield_basis = "resolved_fare"
        if odm is not None and odm.has_revenue:
            y = odm.yield_pence(
                fare.representative_origin_nlc, fare.representative_dest_nlc)
            if y is not None and y > 0:
                price_base = y
                yield_basis = "odm_yield"
        if price_base <= 0:
            row_notes.append("zero/negative price base; row skipped — no guess.")
            continue

        ratio = fare.new_price_pence / price_base
        price_change_pct = (ratio - 1.0) * 100.0

        # --- segment ------------------------------------------------------
        is_london = _infer_london_flow(
            fare.representative_origin_nlc, fare.representative_dest_nlc, loc)
        o_crs = _crs_for(fare.representative_origin_nlc, loc, crs_cache)
        d_crs = _crs_for(fare.representative_dest_nlc, loc, crs_cache)
        dist = None
        if o_crs and d_crs:
            dist = flow_distance_km(
                o_crs, d_crs,
                rgd_path=feed_paths.rgd, msn_path=msn)
        flow_type, class_note = _classify_flow_type(
            is_london, dist.km if dist else None)
        if class_note:
            row_notes.append(class_note)

        tty_rec = tty.get(fare.ticket_code)
        # RSPS5045 §4.6 TKT_TYPE: 'S'=single, 'R'=return, 'N'=season.
        segment = (TicketSegment.SEASON
                   if tty_rec is not None and tty_rec.tkt_type == "N"
                   else TicketSegment.NON_SEASON)
        if tty_rec is None:
            row_notes.append(
                f"ticket {fare.ticket_code} missing from .TTY; treated as "
                "non-season (weaker-response side not knowable here).")

        # --- elasticity response -----------------------------------------
        if ratio == 1.0:
            direction_label = "none"
            eps_value, eps_source, eps_derived = 0.0, "no price change", False
            gross_pct = 0.0
        else:
            direction = (Direction.REDUCTION if ratio < 1.0
                         else Direction.INCREASE)
            direction_label = direction.value
            eps = lookup_elasticity(flow_type, segment, direction)
            eps_value, eps_source, eps_derived = eps.value, eps.source, eps.derived
            gross_pct = (ratio ** eps_value - 1.0) * 100.0

        within_validity = abs(price_change_pct) <= VALIDITY_BOUND_PCT
        if not within_validity:
            validity_warnings += 1
            row_notes.append(
                f"price change {price_change_pct:+.1f}% exceeds the "
                f"±{VALIDITY_BOUND_PCT:.0f}% band the published elasticities "
                "were estimated on; the response figure is an extrapolation."
            )

        # --- absolute volumes (ODM-dependent) ----------------------------
        odm_journeys = None
        eligible_base = gross_product = abstracted = net_new = None
        if odm is not None:
            matched = 0
            for (o, d) in fare.blast_radius_pairs:
                j = odm.by_pair.get((o, d))
                if j is not None:
                    matched += j
                    any_odm_row = True
            if matched > 0:
                odm_journeys = matched
                # Existing eligible passengers adopt the cheaper product:
                # that is abstraction (no new travel), bounded by the
                # existing volume by construction. Published ticket-switch
                # cross-elasticities (+0.2..+0.4) are the evidence base;
                # taking the FULL eligible base as switchers is the
                # max-abstraction (revenue-cautious) reading.
                eligible_base = round(matched * share)
                gross_product = round(eligible_base * (ratio ** eps_value))
                abstracted = eligible_base
                net_new = gross_product - abstracted

        if net_new is not None:
            flows_with_volume += 1
        else:
            flows_percent_only += 1
            if odm is not None:
                row_notes.append(
                    "no ODM coverage across this flow's blast radius; "
                    "percentage-only.")

        estimates.append(DemandEstimate(
            flow_id=fare.flow_id,
            ticket_code=fare.ticket_code,
            flow_type=flow_type.value,
            ticket_segment=segment.value,
            direction=direction_label,
            elasticity=eps_value,
            elasticity_source=eps_source,
            elasticity_derived=eps_derived,
            price_base_pence=price_base,
            yield_basis=yield_basis,
            price_ratio=round(ratio, 4),
            price_change_pct=round(price_change_pct, 2),
            gross_demand_change_pct=round(gross_pct, 2),
            within_validity=within_validity,
            distance_km=dist.km if dist else None,
            distance_method=dist.method if dist else None,
            odm_journeys_per_period=odm_journeys,
            eligible_base_journeys=eligible_base,
            gross_product_journeys=gross_product,
            abstracted_journeys=abstracted,
            net_new_journeys=net_new,
            notes=tuple(row_notes),
        ))

    total_net_new = (
        sum(e.net_new_journeys for e in estimates
            if e.net_new_journeys is not None)
        if any_odm_row else None
    )

    notes: list[str] = [METHODOLOGY_NOTE]
    notes.append(
        f"abstraction model: eligible share {share:.0%} "
        "of existing journeys (named ASSUMPTION) switches to the new "
        "product; switchers are abstraction, not new travel. Evidence base "
        f"for switching: {CROSS_ELASTICITY_SOURCE} "
        f"(range {CROSS_ELASTICITY_TICKET_SWITCH_RANGE})."
    )
    if odm is None:
        notes.append(
            "no ODM at data/odm/odm.csv — all rows are percentage-only. "
            "Drop an ORR origin-destination release there for absolute "
            "journey figures."
        )
    elif not odm.has_revenue:
        notes.append(
            "ODM has no revenue column; price base is the resolver's own "
            "fare, not an implied yield."
        )
    if validity_warnings:
        notes.append(
            f"{validity_warnings} row(s) exceed the ±{VALIDITY_BOUND_PCT:.0f}% "
            "elasticity validity band — treat those magnitudes as "
            "extrapolation, direction only."
        )

    return DemandBlock(
        estimates=tuple(estimates),
        total_net_new_journeys=total_net_new,
        flows_with_volume=flows_with_volume,
        flows_percent_only=flows_percent_only,
        validity_warnings=validity_warnings,
        eligible_share_assumption=share,
        notes=tuple(notes),
    )


__all__ = [
    "DemandBlock",
    "DemandEstimate",
    "ELIGIBLE_SHARE_ASSUMPTION",
    "LONG_DISTANCE_MIN_KM",
    "METHODOLOGY_NOTE",
    "VALIDITY_BOUND_PCT",
    "compute_demand",
]
