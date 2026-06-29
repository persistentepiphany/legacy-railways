"""BRFares oracle: guard the 5/5 SOR match on every run, not once.

Loads the captured BRFares JSON for MAN→EUS and, for every SOR row, asserts
the resolver returns the same price to the penny when given the same route.
Marked `@pytest.mark.slow` because it builds full FFL/LOC/FSC indexes
(~3 GB scanned on the first call; subsequent calls hit the in-process cache).

Run with:   pytest -m slow
Skipped automatically when the feed snapshot or BRFares JSON aren't present
(so a CI host without `data/` still passes the fast suite).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.resolver.resolve import resolve_fare

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

FEED = DATA_DIR / "RJFAF805.FFL"
LOC = DATA_DIR / "RJFAF805.LOC"
FSC = DATA_DIR / "RJFAF805.FSC"
NFO = DATA_DIR / "RJFAF805.NFO"
RLC = DATA_DIR / "RJFAF805.RLC"
DIS = DATA_DIR / "RJFAF805.DIS"
RCM = DATA_DIR / "RJFAF805.RCM"
FRR = DATA_DIR / "RJFAF805.FRR"
TTY = DATA_DIR / "RJFAF805.TTY"
BRFARES_MAN_EUS = DATA_DIR / "brfares_man_eus.json"

# MAN Piccadilly individual NLC, EUS individual NLC — station-level queries
# exercise the full LOC+FSC fan-out.
MAN_PICC_NLC = "2968"
EUSTON_NLC = "1444"


def _load_sor_oracle() -> list[dict]:
    """Return the SOR rows from the captured BRFares MAN→EUS payload."""
    if not BRFARES_MAN_EUS.exists():
        pytest.skip(f"missing oracle JSON: {BRFARES_MAN_EUS}")
    payload = json.loads(BRFARES_MAN_EUS.read_text())
    return [f for f in payload["fares"] if f.get("ticket", {}).get("code") == "SOR"]


def _require_feed() -> None:
    for p in (FEED, LOC, FSC, NFO):
        if not p.exists():
            pytest.skip(f"missing feed file: {p}")


@pytest.mark.slow
@pytest.mark.parametrize("row_idx", range(5))
def test_brfares_sor_match_per_route(row_idx: int) -> None:
    """For each BRFares SOR route, resolve(2968, 1444, SOR, route=R) == raw_price."""
    _require_feed()
    sor = _load_sor_oracle()
    if row_idx >= len(sor):
        pytest.skip(f"only {len(sor)} SOR rows in oracle; index {row_idx} OOB")
    row = sor[row_idx]
    route_int = row["route"]["code"]
    route_str = f"{route_int:05d}"
    expected = row["raw_price"]

    result = resolve_fare(
        MAN_PICC_NLC, EUSTON_NLC, "SOR", FEED,
        loc_path=LOC, fsc_path=FSC, nfo_path=NFO, route_code=route_str,
    )
    assert result.status == "resolved", (
        f"route {route_str} ({row['route']['name']}): expected resolved, got "
        f"{result.status}; last provenance step = "
        f"{result.provenance[-1] if result.provenance else 'NONE'}"
    )
    assert result.price_pence == expected, (
        f"route {route_str} ({row['route']['name']}): "
        f"BRFares={expected}p, resolver={result.price_pence}p "
        f"(delta {(result.price_pence or 0) - expected:+d}p)"
    )


def _expected_yng_pence(adult_pence: int) -> int:
    """Compute YNG-discounted SOR price using the *feed*-derived chain (not
    the marketing-published 1/3 rule, which differs by ~0.1pp from the feed):

      .RLC YNG ADULT_STATUS = 003
      .TTY SOR DISCOUNT_CATEGORY = 01
      .DIS (003, 01) DISCOUNT_INDICATOR='0' DISCOUNT_PERCENTAGE=334 (33.4%)
      .RCM (YNG, SOR) MINIMUM_FARE = 1200p (£12; binds when discounted < £12)
      .FRR rule 01 selects the 5p band for ordinary fares (max_amount=£999k);
      direction is FLOOR (matches BRFares empirically; see railcard.py for the
      spec-vs-oracle note).

    The math: discount = ceil(adult * 334 / 1000); net = adult - discount;
    if net < 1200, net = 1200; floor to 5p.
    """
    discount = (adult_pence * 334 + 999) // 1000
    net = adult_pence - discount
    if net < 1200:
        net = 1200
    if net % 5:
        net = (net // 5) * 5
    return net


@pytest.mark.slow
@pytest.mark.parametrize("row_idx", range(5))
def test_yng_railcard_sor_internally_consistent(row_idx: int) -> None:
    """For each route, the YNG-discounted SOR matches the *feed-derived* chain
    (33.4% per .DIS, £12 floor per .RCM, 5p rounding per .FRR) applied to the
    resolver's own adult SOR. Also asserts every railcard provenance step
    cites a `data/RJFAF805.*` line — proving the chain is honest, not from a
    hardcoded rule.

    Regression guard, NOT a BRFares cross-check. To upgrade to a real BRFares
    oracle: capture data/brfares_man_eus_railcard_YNG.json via
    `python tools/fetch_brfares.py --session-token <JWT> --railcard YNG`."""
    _require_feed()
    sor = _load_sor_oracle()
    if row_idx >= len(sor):
        pytest.skip(f"only {len(sor)} SOR rows; OOB")
    row = sor[row_idx]
    route_str = f"{row['route']['code']:05d}"

    adult_expected = row["raw_price"]
    yng_expected = _expected_yng_pence(adult_expected)

    result = resolve_fare(
        MAN_PICC_NLC, EUSTON_NLC, "SOR", FEED,
        loc_path=LOC, fsc_path=FSC, nfo_path=NFO,
        rlc_path=RLC, dis_path=DIS, rcm_path=RCM, frr_path=FRR, tty_path=TTY,
        route_code=route_str, railcard_code="YNG",
    )
    assert result.status == "resolved"
    assert result.price_pence == yng_expected, (
        f"route {route_str}: adult={adult_expected}p, expected YNG={yng_expected}p "
        f"(adult - ceil(adult*334/1000), floor 1200, round up 5p), "
        f"resolver={result.price_pence}p"
    )

    # Provenance must show every railcard step came from a feed file, not a
    # constant — this is what makes the chain reviewable.
    railcard_steps = {
        s.step: s for s in result.provenance
        if s.step in {"railcard_lookup", "discount_category_lookup",
                      "discount_lookup", "discount_apply", "rounding"}
    }
    for name in ("railcard_lookup", "discount_category_lookup",
                 "discount_lookup", "discount_apply", "rounding"):
        assert name in railcard_steps, (
            f"missing {name!r} provenance step; got steps: "
            f"{[s.step for s in result.provenance]}"
        )
        assert "RJFAF805" in railcard_steps[name].source, (
            f"{name!r} source {railcard_steps[name].source!r} should cite a "
            "data/RJFAF805.* feed line, not a URL or constant"
        )
