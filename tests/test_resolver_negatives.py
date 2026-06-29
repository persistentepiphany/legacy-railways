"""Fast tests for resolver negative cases: no_flow, no_fare, bad inputs.

These run against the real feed but are still fast because:
  - no_flow: a corridor with no F-records short-circuits after the index lookup.
  - no_fare: a corridor that resolves but with an unknown ticket code returns
    after one dict lookup against the cached fare index.
  - input validation: never touches the feed at all.

Skipped automatically when the feed isn't present.
"""

from __future__ import annotations

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


def _require_feed() -> None:
    if not FEED.exists():
        pytest.skip(f"missing feed file: {FEED}")


def _require_railcard_feed() -> None:
    for p in (FEED, LOC, FSC, NFO, RLC, DIS, RCM, FRR, TTY):
        if not p.exists():
            pytest.skip(f"missing feed file: {p}")


def test_no_flow_returns_explanation() -> None:
    """Querying a corridor that doesn't exist returns status=no_flow with a
    provenance step explaining the miss (instead of a fabricated 0 or None)."""
    _require_feed()
    result = resolve_fare("9999", "9998", "SOR", FEED)
    assert result.status == "no_flow"
    assert result.price_pence is None
    # Provenance must contain a step explaining why we didn't find anything.
    explanations = [
        s.detail.get("explanation", "") for s in result.provenance
    ]
    assert any("no F-record" in e for e in explanations), (
        f"expected explanation about missing F-record, got provenance:\n"
        + "\n".join(f"- {s.step}: {s.detail}" for s in result.provenance)
    )


def test_no_fare_lists_available_tickets() -> None:
    """An unknown ticket code on a real corridor returns no_fare and the
    provenance lists the tickets that *are* on the flow — so the user sees
    what they could have asked for instead of being silently empty."""
    _require_feed()
    # Group-level query (no LOC) to keep this fast; the corridor exists.
    result = resolve_fare("0438", "1072", "ZZZ", FEED)
    assert result.status == "no_fare"
    assert result.price_pence is None
    available = next(
        (s.detail.get("available_tickets", "") for s in result.provenance
         if s.step == "fare_lookup_result"),
        "",
    )
    assert "SOR" in available, f"expected SOR among available tickets, got {available!r}"


def test_invalid_nlc_raises() -> None:
    """5-char NLC at the boundary fails fast with a clear error, not garbage."""
    with pytest.raises(ValueError, match="origin_nlc"):
        resolve_fare("99999", "1444", "SOR", FEED)
    with pytest.raises(ValueError, match="dest_nlc"):
        resolve_fare("2968", "144", "SOR", FEED)


def test_invalid_ticket_code_raises() -> None:
    with pytest.raises(ValueError, match="ticket_code"):
        resolve_fare("2968", "1444", "TOOLONG", FEED)


def test_invalid_route_code_raises() -> None:
    with pytest.raises(ValueError, match="route_code"):
        resolve_fare("2968", "1444", "SOR", FEED, route_code="00")


@pytest.mark.slow
def test_unknown_railcard_quarantines_with_feed_lookup() -> None:
    """An unknown railcard returns no_fare; the provenance contains a
    `railcard_lookup` step that names the missing .RLC entry, instead of
    silently falling back to the adult price."""
    _require_railcard_feed()
    result = resolve_fare(
        "2968", "1444", "SOR", FEED,
        loc_path=LOC, fsc_path=FSC, nfo_path=NFO,
        rlc_path=RLC, dis_path=DIS, rcm_path=RCM, frr_path=FRR, tty_path=TTY,
        railcard_code="ZZZ",
    )
    assert result.status == "no_fare"
    assert result.price_pence is None
    miss = next(
        (s for s in result.provenance if s.step == "railcard_lookup"),
        None,
    )
    assert miss is not None, "expected a railcard_lookup provenance step"
    assert miss.detail.get("found") == "no"
    assert "RJFAF805.RLC" in miss.source
