"""REGULATION.md §5 — the 5-case verification test.

Pass condition for the regulation map: all five cases classify with the
expected (regulated, citation) pair on the current feed snapshot. This is
the guardrail that prevents the compliance feature from being built on
hallucinated classifications.

Marked `@pytest.mark.slow` because it builds the FFL index (~250 MB scanned
on first call; cached after).

Run with:   pytest tests/test_regulation_map.py -m slow
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.regulation import (
    CorridorSpec,
    RegulationEntry,
    RegulationMap,
    build_regulation_map,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

FFL = DATA / "RJFAF805.FFL"
LOC = DATA / "RJFAF805.LOC"
TTY = DATA / "RJFAF805.TTY"
FSC = DATA / "RJFAF805.FSC"

# NLCs verified from the .LOC file (see tools/classify_corridor.py for the
# inline reader). MAN PICC / EUSTON / STOKE-ON-TRENT are the §5 corridors.
MAN_PICC_NLC = "2968"
EUSTON_NLC = "1444"
STOKE_NLC = "1314"

# Tickets referenced by §5 cases that may not be on the corridor's .FFL
# (e.g. SDR on MAN-EUS — Anytime Day Return is a London-zone walk-up not
# present on this long-distance corridor). Passed via extra_tickets so the
# map records an HONEST GAP entry rather than silently omitting them.
EXTRA_TICKETS = ("SDR",)


@pytest.fixture(scope="module")
def regmap() -> RegulationMap:
    """Build the §5 corridor regulation map once per test module."""
    for p in (FFL, LOC, TTY, FSC):
        if not p.exists():
            pytest.skip(f"missing feed file: {p}")
    return build_regulation_map(
        [
            CorridorSpec("MAN-EUS", MAN_PICC_NLC, EUSTON_NLC, is_london_flow=True),
            CorridorSpec("SOT-MAN", STOKE_NLC, MAN_PICC_NLC, is_london_flow=False),
        ],
        ffl_path=FFL, loc_path=LOC, tty_path=TTY, fsc_path=FSC,
        extra_tickets=EXTRA_TICKETS,
    )


def _get(regmap: RegulationMap, o: str, d: str, t: str) -> RegulationEntry:
    entry = regmap.get(o, d, t)
    assert entry is not None, (
        f"no map entry for ({o},{d},{t}); the corridor may not be in the "
        f"built map (entries: {len(regmap.entries)})"
    )
    return entry


# --- REGULATION.md §5 Case 1: MAN<->EUS Off-Peak Return -> Regulated ---------


@pytest.mark.slow
def test_case_1_man_eus_off_peak_return_svr_is_regulated(regmap: RegulationMap) -> None:
    """SVR = OFF-PEAK R on MAN-EUS. Should be regulated under §1
    (Off-Peak Return on long-distance flow + Standard class)."""
    entry = _get(regmap, MAN_PICC_NLC, EUSTON_NLC, "SVR")
    assert entry.regulated is True, (
        f"SVR should be regulated; got {entry.regulated}, "
        f"citation: {entry.citation.section} {entry.citation.rule_text!r}"
    )
    assert entry.citation.section == "§1"
    assert "Off-Peak Return" in entry.citation.rule_text
    assert entry.cap_price_2025_pence is not None and entry.cap_price_2025_pence > 0


# --- REGULATION.md §5 Case 2: SOT<->MAN Off-Peak Return -> Regulated ---------


@pytest.mark.slow
def test_case_2_sot_man_off_peak_return_svr_is_regulated(regmap: RegulationMap) -> None:
    """SVR on SOT-MAN. Should be regulated under §1 (long-distance Off-Peak
    Return). Stoke COUNTY='24' (Staffordshire, England) — not devolved."""
    entry = _get(regmap, STOKE_NLC, MAN_PICC_NLC, "SVR")
    assert entry.regulated is True
    assert entry.citation.section == "§1"
    assert "Off-Peak Return" in entry.citation.rule_text
    assert entry.cap_price_2025_pence is not None and entry.cap_price_2025_pence > 0


# --- REGULATION.md §5 Case 3: MAN<->EUS Anytime Day Return -> Regulated ------
# Per user decision (plan: cosmic-twirling-noodle.md): the §5 doc says SDR
# is regulated on London-area flows, but the canonical Anytime Day Return
# code SDR isn't on the MAN-EUS .FFL (long-distance corridor, Anytime Day
# Returns are a London-zone concept). The expected outcome is HONEST GAP:
# the map records a MISSING entry whose citation explains the gap, rather
# than silently classifying it regulated=False.


@pytest.mark.slow
def test_case_3_man_eus_anytime_day_return_sdr_is_honest_gap(regmap: RegulationMap) -> None:
    """SDR (ANYTIME DAY R) not present on MAN-EUS .FFL. The map must surface
    this as an HONEST GAP — citation starts with 'MISSING:' — and must NOT
    silently classify it as regulated=False without the marker."""
    entry = _get(regmap, MAN_PICC_NLC, EUSTON_NLC, "SDR")
    # The marker is mandatory — this is what stops the compliance check
    # from quietly skipping a regulated ticket because it happens to be absent.
    assert entry.citation.rule_text.startswith("MISSING:"), (
        f"expected MISSING marker in citation; got {entry.citation.rule_text!r}"
    )
    # By convention a MISSING entry is regulated=False (we cannot assert
    # regulated without seeing a price), but the marker is what proves
    # the gap is acknowledged, not guessed.
    assert entry.regulated is False
    assert entry.cap_price_2025_pence is None
    # Section is whichever rule the base classifier hit on .TTY-only data;
    # the test doesn't pin it (the marker text is the load-bearing assertion).


# --- REGULATION.md §5 Case 4: MAN<->EUS Advance -> NOT regulated -------------


@pytest.mark.slow
def test_case_4_man_eus_advance_c1s_is_not_regulated(regmap: RegulationMap) -> None:
    """C1S (.TTY DESCRIPTION='ADVANCE') on MAN-EUS. Should be NOT regulated
    under §1 (Advance fares excluded). Citation must fire the ADVANCE rule
    specifically — not e.g. 'not Standard class' (C1S is Standard)."""
    entry = _get(regmap, MAN_PICC_NLC, EUSTON_NLC, "C1S")
    assert entry.regulated is False
    assert entry.citation.section == "§1"
    assert "Advance" in entry.citation.rule_text
    assert entry.cap_price_2025_pence is None


# --- REGULATION.md §5 Case 5: MAN<->EUS First Class Return -> NOT regulated --


@pytest.mark.slow
def test_case_5_man_eus_first_class_return_for_is_not_regulated(regmap: RegulationMap) -> None:
    """FOR (ANYTIME 1R, TKT_CLASS=1) on MAN-EUS via the group flow
    (0438->1072). Should be NOT regulated under §1 (First Class excluded).
    Citation must fire the First Class rule, not the default fall-through."""
    entry = _get(regmap, MAN_PICC_NLC, EUSTON_NLC, "FOR")
    assert entry.regulated is False
    assert entry.citation.section == "§1"
    assert "First Class" in entry.citation.rule_text
    assert entry.cap_price_2025_pence is None


# --- Map-level invariants ----------------------------------------------------


@pytest.mark.slow
def test_map_notes_disclose_cap_price_assumption(regmap: RegulationMap) -> None:
    """The map's notes list must disclose the REGULATION.md §4 fallback
    (cap_price = current snapshot, not 1 Mar 2025 reference). The UI greps
    for this so a reviewer can challenge the assumption."""
    joined = " | ".join(regmap.notes)
    assert "1 March 2025" in joined or "REGULATION.md §4" in joined
    assert "NFO overrides not applied" in joined


@pytest.mark.slow
def test_map_has_corridor_coverage(regmap: RegulationMap) -> None:
    """Sanity floor: the corridor scan should pick up dozens of tickets
    (Standard walk-ups, advances, season tickets via the group flow). A
    drastically smaller number would mean the fan-out broke."""
    assert len(regmap.entries) >= 50, (
        f"unexpectedly small map ({len(regmap.entries)} entries); "
        f"check LOC group fan-out in build_regulation_map"
    )
