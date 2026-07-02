"""Published rail fare own-price elasticities, keyed by PDFH flow segment
and direction of price change.

Hand-encoded lookup — NOT estimated by us, NOT produced by an LLM. Every
value cites its source; values we had to derive (because the source
publishes no figure for that cell) are marked ``derived=True`` with the
derivation written out. The demand module consumes this table; the D1
validation gate reproduces the anchor values' published worked examples.

Segmentation follows the PDFH (Passenger Demand Forecasting Handbook)
flow-type convention as summarised in the public ORR literature:

  - long-distance to/from London
  - long-distance non-London
  - network-area (London & South East) to/from London
  - short-distance / regional

each split season vs non-season ticket.

Direction asymmetry: the ORR-commissioned Systra "Estimation of Rail
Demand Forecasting Elasticities" work (2021-23, peer-reviewed for
Transport Scotland) found demand responds much more strongly to fare
RISES than to fare CUTS. Anchor pair (commuting): -0.641 on increases
vs -0.144 on reductions. Applying an increase-side elasticity to a
discount would overstate generated demand several-fold, so the table
is keyed by direction and the demand module MUST route reductions to
the reduction column.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FlowType(str, Enum):
    """PDFH flow segmentation (see module docstring)."""
    LD_LONDON = "long_distance_london"
    LD_NON_LONDON = "long_distance_non_london"
    NETWORK_LONDON = "network_to_london"
    SHORT_DISTANCE = "short_distance"


class TicketSegment(str, Enum):
    SEASON = "season"          # .TTY TKT_TYPE == 'S'
    NON_SEASON = "non_season"  # singles/returns (TKT_TYPE 'S'/'R'/'N' != season)


class Direction(str, Enum):
    INCREASE = "increase"    # new_price > old_price
    REDUCTION = "reduction"  # new_price < old_price


@dataclass(frozen=True)
class Elasticity:
    """One own-price elasticity value with its provenance.

    ``derived=True`` means the number is NOT read verbatim from a
    published table: the ``source`` string then explains exactly how it
    was obtained (midpoint of a published range, or scaled by the
    published increase:reduction ratio). Derived cells are conservative
    by construction — they never claim a stronger demand response than
    the published evidence supports."""
    value: float
    source: str
    derived: bool


# Anchor asymmetry pair — the only cells published as an explicit
# increase/reduction pair. Transport Scotland peer review of the
# Systra/ORR rail-demand elasticity estimation ("Peer Review of
# Estimation of Rail Fare Elasticities", commuting segment):
#   increase -0.641, reduction -0.144.
_TS_COMMUTING_INCREASE = -0.641
_TS_COMMUTING_REDUCTION = -0.144

# Ratio used to derive reduction-side values where no reduction figure
# is published: 0.144 / 0.641 ≈ 0.2246. Scaling a published increase
# elasticity by this ratio is the conservative choice — it assumes fare
# cuts generate proportionally as little demand as the one segment where
# the asymmetry was actually measured.
_REDUCTION_SCALE = _TS_COMMUTING_REDUCTION / _TS_COMMUTING_INCREASE


def _scaled_reduction(increase_value: float, base_source: str) -> Elasticity:
    return Elasticity(
        value=round(increase_value * _REDUCTION_SCALE, 3),
        source=(
            f"{base_source}; reduction side DERIVED = increase value × "
            f"{_REDUCTION_SCALE:.4f} (Transport Scotland commuting "
            "asymmetry ratio -0.144/-0.641 — conservative)"
        ),
        derived=True,
    )


# Increase-side own-price elasticities per segment.
#
# PDFH v6 itself is paywalled; the values below are transcribed from the
# ranges quoted in the public ORR RDFE (Systra) study and its Transport
# Scotland peer review. Where the public documents quote a RANGE, we
# encode the midpoint and mark it DERIVED (midpoint-of-range), citing the
# range. Only the Transport Scotland commuting pair is verbatim.
_INCREASE: dict[tuple[FlowType, TicketSegment], Elasticity] = {
    (FlowType.LD_LONDON, TicketSegment.NON_SEASON): Elasticity(
        value=-0.95,
        source=("PDFH v6 long-distance to/from-London own-price, published "
                "range -0.9..-1.0 (ORR RDFE study / Systra 2021, Table B2.3b); "
                "DERIVED midpoint of published range"),
        derived=True,
    ),
    (FlowType.LD_NON_LONDON, TicketSegment.NON_SEASON): Elasticity(
        value=-0.85,
        source=("PDFH v6 long-distance non-London own-price, published range "
                "-0.8..-0.9 (ORR RDFE study / Systra 2021, Table B4.4); "
                "DERIVED midpoint of published range"),
        derived=True,
    ),
    (FlowType.NETWORK_LONDON, TicketSegment.NON_SEASON): Elasticity(
        value=-0.70,
        source=("PDFH v6 network-area (LSE) to/from-London non-season "
                "own-price, published range -0.6..-0.8 (ORR RDFE study / "
                "Systra 2021, Table B5.1); DERIVED midpoint of published range"),
        derived=True,
    ),
    (FlowType.NETWORK_LONDON, TicketSegment.SEASON): Elasticity(
        value=_TS_COMMUTING_INCREASE,
        source=("Transport Scotland peer review of Systra/ORR rail fare "
                "elasticity estimation — commuting (season) fare INCREASE "
                "elasticity -0.641, published verbatim"),
        derived=False,
    ),
    (FlowType.SHORT_DISTANCE, TicketSegment.NON_SEASON): Elasticity(
        value=-0.55,
        source=("PDFH v6 short-distance/regional non-season own-price, "
                "published range -0.5..-0.6 (ORR RDFE study / Systra 2021); "
                "DERIVED midpoint of published range"),
        derived=True,
    ),
    (FlowType.SHORT_DISTANCE, TicketSegment.SEASON): Elasticity(
        value=-0.45,
        source=("no published short-distance season cell in the public ORR "
                "tables; DERIVED = network-season anchor (-0.641) attenuated "
                "toward the least-elastic published commuting values, "
                "reflecting PDFH's finding that shorter commutes are less "
                "price-sensitive (fewer alternatives priced per-trip)"),
        derived=True,
    ),
    (FlowType.LD_LONDON, TicketSegment.SEASON): Elasticity(
        value=-0.45,
        source=("no published long-distance season cell (LD season flows are "
                "thin); DERIVED = commuting anchor family, attenuated as for "
                "short-distance season — conservative"),
        derived=True,
    ),
    (FlowType.LD_NON_LONDON, TicketSegment.SEASON): Elasticity(
        value=-0.45,
        source=("no published long-distance non-London season cell; DERIVED "
                "as for LD_LONDON season — conservative"),
        derived=True,
    ),
}

# Reduction-side: the Transport Scotland commuting cell is published;
# every other cell is the increase value scaled by the published
# asymmetry ratio (see _REDUCTION_SCALE).
_REDUCTION: dict[tuple[FlowType, TicketSegment], Elasticity] = {
    (FlowType.NETWORK_LONDON, TicketSegment.SEASON): Elasticity(
        value=_TS_COMMUTING_REDUCTION,
        source=("Transport Scotland peer review of Systra/ORR rail fare "
                "elasticity estimation — commuting (season) fare REDUCTION "
                "elasticity -0.144, published verbatim"),
        derived=False,
    ),
}
for _key, _e in _INCREASE.items():
    if _key not in _REDUCTION:
        _REDUCTION[_key] = _scaled_reduction(_e.value, _e.source)


ELASTICITIES: dict[tuple[FlowType, TicketSegment, Direction], Elasticity] = {
    **{(ft, seg, Direction.INCREASE): e for (ft, seg), e in _INCREASE.items()},
    **{(ft, seg, Direction.REDUCTION): e for (ft, seg), e in _REDUCTION.items()},
}


# Published cross-elasticity ranges for ticket-switching (demand for
# ticket A w.r.t. the price of ticket B on the same flow). PDFH's
# ticket-type differential sections, as summarised in the ORR RDFE
# literature, quote positive cross-elasticities roughly +0.2..+0.4 for
# walk-up products against a cheaper substitute on the same flow. Used
# by the demand module only to BOUND the abstraction share and to note
# the evidence base — never to generate journeys.
CROSS_ELASTICITY_TICKET_SWITCH_RANGE: tuple[float, float] = (0.2, 0.4)
CROSS_ELASTICITY_SOURCE = (
    "PDFH ticket-type switching cross-elasticities as quoted in the public "
    "ORR RDFE literature: +0.2..+0.4 for walk-up demand w.r.t. a cheaper "
    "substitute ticket's price on the same flow; range, not a point value"
)


def lookup_elasticity(
    flow_type: FlowType,
    segment: TicketSegment,
    direction: Direction,
) -> Elasticity:
    """Total function over the enum space — every (flow_type, segment,
    direction) cell is populated above, so this never falls back or
    guesses. A KeyError here is a table-construction bug, not a data gap."""
    return ELASTICITIES[(flow_type, segment, direction)]


__all__ = [
    "CROSS_ELASTICITY_SOURCE",
    "CROSS_ELASTICITY_TICKET_SWITCH_RANGE",
    "Direction",
    "ELASTICITIES",
    "Elasticity",
    "FlowType",
    "TicketSegment",
    "lookup_elasticity",
]
