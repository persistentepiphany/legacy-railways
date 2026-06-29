"""Compute the two revenue exposure numbers from an affected set.

Both numbers are STRUCTURAL SUMS, not demand-weighted forecasts. The
distinction matters and is surfaced in field docstrings:

  per_flow_exposure_pence
      Sum of (new_price - old_price) over distinct repriced fares
      (canonical_affected). Each fare counts once. This is the honest
      "static-demand" exposure: if every existing booking is rebooked at
      the new price and no behaviour changes, this is the revenue delta.

  per_pair_exposure_pence
      Sum of (new - old) over blast_radius_pairs — i.e., the per-flow
      delta multiplied by how many station pairs each fare governs via
      cluster fan-out. NEVER use as revenue; it's the GB-map headline
      number ('this change touches N station pairs') multiplied by the
      delta per pair. Useful for the map showpiece, dangerous as a
      financial figure if misread.

Sign convention: discounts produce NEGATIVE exposure (revenue down)."""

from __future__ import annotations

from src.impact.affected import AffectedFare


def per_flow_exposure(affected: tuple[AffectedFare, ...]) -> int:
    """Sum of (new - old) over distinct repriced fares. The honest answer."""
    total = 0
    for fare in affected:
        if fare.old_price_pence is None or fare.new_price_pence is None:
            continue
        total += fare.new_price_pence - fare.old_price_pence
    return total


def per_pair_exposure(affected: tuple[AffectedFare, ...]) -> int:
    """Sum of (new - old) × blast-radius-pair-count.

    NEVER cite as revenue. It's the cluster-expansion view used by the
    GB-map showpiece ('this many station-pair journeys would see a price
    change'). Two scenarios:
      - A canonical row on a direct flow covers 1 pair → contributes once.
      - A canonical row on a group flow covers many pairs → contributes N times.
    """
    total = 0
    for fare in affected:
        if fare.old_price_pence is None or fare.new_price_pence is None:
            continue
        delta = fare.new_price_pence - fare.old_price_pence
        total += delta * len(fare.blast_radius_pairs)
    return total


__all__ = ["per_flow_exposure", "per_pair_exposure"]
