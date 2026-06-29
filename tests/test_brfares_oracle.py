"""BRFares correctness oracle for the Manchester <-> London corridor.

From CLAUDE.md: "Validate resolver output against BRFares (brfares.com) for the
demo corridor before trusting it."

This harness lets the real resolver plug in via `diff_against_oracle`. Expected
prices are still TODOs to be captured from brfares.com.

NLCs (verified against RJFAF805.LOC):
  - 0438 = MANCHESTER STNS  (group; member stations include Piccadilly/Victoria/
    Oxford Rd/Deansgate). Long-distance flows are indexed on this group NLC.
  - 1072 = LONDON TERMINALS (group; member terminals include Euston, Kings X,
    Paddington, etc.). Same — flows go via the group.
  - 1444 = LONDON EUSTON    (individual; GROUP_NLC field = 1072).

The thin-slice resolver looks up exact-match group<->group flows (0438<->1072)
because no direct 0438<->1444 flow exists in the feed; reaching the individual
station NLC requires .FSC cluster fan-out, which is a deferred slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class OracleRow:
    origin_nlc: str
    dest_nlc: str
    ticket_code: str
    description: str
    expected_pence: Optional[int]  # None = not yet captured from brfares.com


# Capture date: TODO(fill from brfares.com when prices are pulled). Snapshot
# the whole table on one day so we can re-pull cleanly if BRFares numbers shift.
MANCHESTER_LONDON_ORACLE: list[OracleRow] = [
    OracleRow(
        origin_nlc="0438", dest_nlc="1072", ticket_code="SOR",
        description="Standard Off-Peak Return MAN<->LON (regulated walk-up under §3 freeze)",
        expected_pence=None,  # TODO: brfares.com MAN->LON SOR
    ),
    OracleRow(
        origin_nlc="0438", dest_nlc="1072", ticket_code="SVR",
        description="Super Off-Peak Return MAN<->LON",
        expected_pence=None,  # TODO: brfares.com MAN->LON SVR
    ),
    OracleRow(
        origin_nlc="0438", dest_nlc="1072", ticket_code="SOS",
        description="Standard Off-Peak Single MAN<->LON",
        expected_pence=None,  # TODO: brfares.com MAN->LON SOS
    ),
    OracleRow(
        origin_nlc="0438", dest_nlc="1072", ticket_code="SVS",
        description="Super Off-Peak Single MAN<->LON",
        expected_pence=None,  # TODO: brfares.com MAN->LON SVS
    ),
    OracleRow(
        origin_nlc="0438", dest_nlc="1072", ticket_code="FOR",
        description="First Open Return MAN<->LON (NOT regulated)",
        expected_pence=None,  # TODO: brfares.com MAN->LON FOR
    ),
]


@dataclass(frozen=True)
class Mismatch:
    origin_nlc: str
    dest_nlc: str
    ticket_code: str
    expected_pence: int
    got_pence: Optional[int]
    note: str = ""


# A resolver_fn takes (origin_nlc, dest_nlc, ticket_code) -> pence (or None
# if the resolver can't price the fare). The real resolver's return type is a
# `ResolvedFare` with full provenance; `price_only_adapter` below collapses
# that to the bare-int signature the oracle harness wants.
ResolverFn = Callable[[str, str, str], Optional[int]]


def price_only_adapter(feed_path: Path) -> ResolverFn:
    """Wrap `src.resolver.resolve_fare(..., feed_path)` into a ResolverFn.

    The oracle harness wants `(o, d, t) -> Optional[int]`. The real resolver
    returns a `ResolvedFare` with provenance; this adapter discards everything
    but the price. Provenance is still available via the underlying resolver
    for any test that wants to assert on it directly."""
    from src.resolver.resolve import resolve_fare

    def _call(origin_nlc: str, dest_nlc: str, ticket_code: str) -> Optional[int]:
        result = resolve_fare(origin_nlc, dest_nlc, ticket_code, feed_path)
        return result.price_pence

    return _call


def diff_against_oracle(
    resolver_fn: ResolverFn,
    oracle: list[OracleRow] = MANCHESTER_LONDON_ORACLE,
) -> list[Mismatch]:
    """Run `resolver_fn` over every row in the oracle and return mismatches.
    Skips rows where `expected_pence is None` (i.e. not yet captured)."""
    mismatches: list[Mismatch] = []
    for row in oracle:
        if row.expected_pence is None:
            continue
        got = resolver_fn(row.origin_nlc, row.dest_nlc, row.ticket_code)
        if got != row.expected_pence:
            mismatches.append(
                Mismatch(
                    origin_nlc=row.origin_nlc,
                    dest_nlc=row.dest_nlc,
                    ticket_code=row.ticket_code,
                    expected_pence=row.expected_pence,
                    got_pence=got,
                    note=row.description,
                )
            )
    return mismatches


# --- Tests -----------------------------------------------------------------
# Until the resolver lands, this only enforces the SHAPE of the harness.


def test_oracle_shape_is_valid() -> None:
    assert len(MANCHESTER_LONDON_ORACLE) > 0
    for row in MANCHESTER_LONDON_ORACLE:
        assert row.origin_nlc and row.dest_nlc and row.ticket_code
        assert row.description
        assert row.expected_pence is None or row.expected_pence > 0


def test_diff_skips_uncaptured_rows() -> None:
    """A resolver that always returns 0 should still produce zero mismatches
    while every expected_pence is None — we don't flag what hasn't been captured."""
    mismatches = diff_against_oracle(lambda o, d, t: 0)
    assert mismatches == []
