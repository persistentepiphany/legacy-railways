"""Smoke tests for the RSPS5046 CIF timetable parser.

Tightly scoped: assert the parser loads on the real .MCA snapshot, builds
a TIPLOC + schedule index, and surfaces the well-known WCML calling points
on the MAN->EUS corridor. No exhaustive validation — the heavy battery
(NRE cross-check, day-of-week masking) is a separate session.

Marked `@pytest.mark.slow`. Skipped if no `RJTTF*.MCA` lives in `data/`,
mirroring the resolver-test skip pattern so CI installs without the feed
still pass.

Run with:  pytest tests/test_timetable_parser.py -m slow
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ingest.timetable import (
    TimetableIndex,
    intermediate_calls,
    load_timetable_index,
    trains_serving_corridor,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"


@pytest.fixture(scope="module")
def mca_path() -> Path:
    candidates = sorted(DATA.glob("RJTTF*.MCA"))
    if not candidates:
        pytest.skip(
            "no RJTTF*.MCA in data/; fetch the RSPS5046 timetable bundle to run "
            "this test (see plan: NRDP authenticate -> /api/staticfeeds/3.0/timetable)"
        )
    return candidates[-1]


@pytest.fixture(scope="module")
def idx(mca_path: Path) -> TimetableIndex:
    return load_timetable_index(mca_path)


# --- 1. Parser loads + structural sanity ----------------------------------


@pytest.mark.slow
def test_load_timetable_index_smoke(idx: TimetableIndex) -> None:
    """Index loads non-empty and the bedrock WCML stations resolve TIPLOC<->CRS.
    No exact counts: TIPLOC and schedule cardinalities drift with each snapshot.
    """
    assert isinstance(idx, TimetableIndex)
    assert len(idx.tiplocs) > 1000, (
        f"expected thousands of TIPLOCs in a national CIF; got {len(idx.tiplocs)}"
    )
    assert len(idx.schedules) > 100, (
        f"expected many passenger schedules; got {len(idx.schedules)}"
    )
    # The CIF schedule list MUST include a non-trivial calling sequence on
    # average. A near-empty calling list means LO/LI/LT parsing broke.
    avg_calls = sum(len(s.calling_points) for s in idx.schedules) / len(idx.schedules)
    assert avg_calls > 2, (
        f"average calling-point count {avg_calls:.2f} is too low; LO/LI/LT parsing likely broken"
    )
    # Spot-check the headline WCML stations resolve through the reverse map.
    for crs in ("MAN", "EUS", "RUG", "MKC", "CRE", "SOT", "STA"):
        assert crs in idx.crs_to_tiplocs, (
            f"CRS {crs!r} missing from timetable index; TI record parse likely off"
        )


# --- 2. Corridor lookup: real calling points include known WCML stops -----


@pytest.mark.slow
def test_intermediate_calls_man_eus_includes_known_wcml_stops(
    idx: TimetableIndex,
) -> None:
    """The MAN->EUS corridor's calling-point union must include the textbook
    Avanti WCML stops. We don't pin an exact set (services vary by day and
    snapshot); we assert the corridor is served and the well-known stops
    are present."""
    serving = trains_serving_corridor(idx, "MAN", "EUS")
    assert len(serving) > 0, (
        "no passenger trains found serving MAN->EUS in this snapshot; "
        "either the snapshot doesn't cover this corridor (unlikely) or "
        "the subsequence lookup is broken"
    )

    inter = set(intermediate_calls(idx, "MAN", "EUS"))
    # Endpoints must NEVER appear in the intermediate set.
    assert "MAN" not in inter and "EUS" not in inter

    # Rugby is the canonical Avanti calling point we already use as the
    # split showpiece; if this is missing the CRS join broke.
    assert "RUG" in inter, (
        f"expected RUG in MAN->EUS intermediate calls; got {sorted(inter)[:30]}"
    )
    # At least one other well-known WCML stop should also fire (don't
    # require ALL — different services skip different stops).
    other_wcml = {"MKC", "CRE", "SOT", "STA", "WFJ", "NMP"} & inter
    assert other_wcml, (
        f"expected at least one of MKC/CRE/SOT/STA/WFJ/NMP in MAN->EUS "
        f"intermediates; got {sorted(inter)[:30]}"
    )
