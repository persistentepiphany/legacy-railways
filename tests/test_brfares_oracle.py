"""BRFares correctness oracle for the Manchester <-> London corridor.

From CLAUDE.md: "Validate resolver output against BRFares (brfares.com) for the
demo corridor before trusting it."

This file is a SHAPE stub. The real resolver is not yet wired; the harness
exists so that the moment we have one, plugging it into `diff_against_oracle`
gives us a single-row-per-fare correctness diff. Expected fares are TODOs to be
filled from brfares.com — see the comment on each row for the lookup.

Manchester Piccadilly NLC: 2960    London Euston NLC: 1444
(Confirm both via .LOC when the feed is in place; treated as TODOs below.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class OracleRow:
    origin_nlc: str
    dest_nlc: str
    ticket_code: str
    description: str
    expected_pence: Optional[int]  # None = not yet captured from brfares.com


# Capture date: TODO(fill from brfares.com, e.g. "2026-06-28"). Snapshot the
# whole table on one day so we can re-pull cleanly if BRFares numbers shift.
MANCHESTER_LONDON_ORACLE: list[OracleRow] = [
    OracleRow(
        origin_nlc="2960", dest_nlc="1444", ticket_code="SOR",
        description="Off-Peak Return MAN<->EUS (regulated walk-up under §3 freeze)",
        expected_pence=None,  # TODO: brfares.com MAN->EUS SOR
    ),
    OracleRow(
        origin_nlc="2960", dest_nlc="1444", ticket_code="SVR",
        description="Super Off-Peak Return MAN<->EUS",
        expected_pence=None,  # TODO: brfares.com MAN->EUS SVR
    ),
    OracleRow(
        origin_nlc="2960", dest_nlc="1444", ticket_code="SDR",
        description="Anytime Day Return MAN<->EUS (regulated for London-flow commuter cases)",
        expected_pence=None,  # TODO: brfares.com MAN->EUS SDR
    ),
    OracleRow(
        origin_nlc="2960", dest_nlc="1444", ticket_code="SDS",
        description="Anytime Day Single MAN<->EUS",
        expected_pence=None,  # TODO: brfares.com MAN->EUS SDS
    ),
    OracleRow(
        origin_nlc="2960", dest_nlc="1444", ticket_code="FOR",
        description="First Open Return MAN<->EUS (NOT regulated)",
        expected_pence=None,  # TODO: brfares.com MAN->EUS FOR
    ),
    OracleRow(
        origin_nlc="2960", dest_nlc="1444", ticket_code="7DS",
        description="Weekly Season MAN<->EUS Standard (regulated)",
        expected_pence=None,  # TODO: brfares.com MAN->EUS 7DS
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
# if the resolver can't price the fare). Signature matches what src/resolver/
# is being built toward; the *return* type on the real resolver will be a
# ResolvedFare-with-provenance, of which `.fare_pence` is one field.
ResolverFn = Callable[[str, str, str], Optional[int]]


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
