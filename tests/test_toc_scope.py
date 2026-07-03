"""Operator (TOC) scope: ChangeRequest validation + compute_impact bounding.

Fast tests exercise the shape-only `__post_init__` rules (no feed needed).
Slow tests run the full TOC-scope pipeline on the real RJFAF805 feed and pin
the NTH (Northern) fingerprints — exact counts tied to the snapshot, so a
feed refresh fails loudly and we re-snapshot deliberately (same convention
as tests/test_impact_demo_corridor.py).

Run slow tests with:   pytest tests/test_toc_scope.py -m slow
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.impact import ChangeRequest, FeedPaths, ImpactReport, compute_impact
from src.impact.affected import AffectedFare, compute_affected_set

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"


def _toc_change(**overrides: Any) -> ChangeRequest:
    kwargs: dict[str, Any] = dict(
        kind="add_railcard",
        railcard_code="STU",
        discount_pct=1.0 / 3.0,
        discount_categories=("01",),
        corridor_origin_nlc="",
        corridor_dest_nlc="",
        peak_valid=True,
        description="Add Student railcard, 1/3 off, all Northern flows",
        scope="toc",
        toc_code="NTH",
    )
    kwargs.update(overrides)
    return ChangeRequest(**kwargs)


# --- Fast: shape validation (__post_init__) ---------------------------------


def test_toc_change_constructs() -> None:
    ch = _toc_change()
    assert ch.scope == "toc"
    assert ch.toc_code == "NTH"


def test_default_scope_is_corridor() -> None:
    ch = ChangeRequest(
        kind="add_railcard",
        railcard_code="STU",
        discount_pct=0.5,
        discount_categories=("01",),
        corridor_origin_nlc="2968",
        corridor_dest_nlc="1444",
        peak_valid=False,
        description="corridor default",
    )
    assert ch.scope == "corridor"
    assert ch.toc_code is None


def test_toc_scope_requires_toc_code() -> None:
    with pytest.raises(ValueError, match="requires a 2-3 alnum toc_code"):
        _toc_change(toc_code=None)


@pytest.mark.parametrize("bad", ["N", "NORT", "N-H", ""])
def test_toc_scope_rejects_malformed_toc_code(bad: str) -> None:
    with pytest.raises(ValueError, match="2-3 alnum toc_code"):
        _toc_change(toc_code=bad)


def test_toc_scope_rejects_corridor_nlcs() -> None:
    with pytest.raises(ValueError, match="empty corridor_origin_nlc"):
        _toc_change(corridor_origin_nlc="2968", corridor_dest_nlc="1444")


def test_corridor_scope_rejects_toc_code() -> None:
    with pytest.raises(ValueError, match="only valid with scope='toc'"):
        ChangeRequest(
            kind="add_railcard",
            railcard_code="STU",
            discount_pct=0.5,
            discount_categories=("01",),
            corridor_origin_nlc="2968",
            corridor_dest_nlc="1444",
            peak_valid=False,
            description="corridor with stray toc_code",
            toc_code="NTH",
        )


# --- Slow: real-feed NTH fingerprints ---------------------------------------


@pytest.fixture(scope="module")
def feed_paths() -> FeedPaths:
    paths = FeedPaths.default_for_data_dir(DATA)
    missing = paths.missing()
    if missing:
        pytest.skip(f"missing feed file(s): {missing}")
    return paths


@pytest.fixture(scope="module")
def nth_report(feed_paths: FeedPaths) -> ImpactReport:
    return compute_impact(
        _toc_change(),
        feed_paths,
        include={"revenue", "compliance", "anomalies", "splits", "performance"},
    )


# Fingerprints measured on the RJFAF805 snapshot. If the feed changes these
# fail loudly — re-derive them, don't loosen the assertions.
NTH_FLOWS_TOTAL = 26_445
NTH_FLOWS_ACTUAL = 24_612
NTH_GENERATED_SKIPPED = 1_833
NTH_CANONICAL_TOTAL = 45_931
NTH_BLAST_PAIRS_TOTAL = 330_773
ROW_CAP = 200
BLAST_PAIR_CAP = 5_000


@pytest.mark.slow
def test_nth_scope_stats_fingerprint(nth_report: ImpactReport) -> None:
    st = nth_report.scope_stats
    assert st is not None
    assert st.scope == "toc"
    assert st.toc_code == "NTH"
    assert st.flows_total == NTH_FLOWS_TOTAL
    assert st.flows_actual == NTH_FLOWS_ACTUAL
    assert st.flows_generated_skipped == NTH_GENERATED_SKIPPED
    assert st.flows_total == st.flows_actual + st.flows_generated_skipped
    assert st.canonical_total == NTH_CANONICAL_TOTAL
    assert st.canonical_returned == ROW_CAP
    assert st.blast_pairs_total == NTH_BLAST_PAIRS_TOTAL
    assert st.blast_pairs_returned == BLAST_PAIR_CAP
    assert st.truncated is True
    assert 0 < len(st.toc_station_nlcs) <= 2_500


@pytest.mark.slow
def test_nth_rows_truncated_to_top_delta(
    nth_report: ImpactReport, feed_paths: FeedPaths,
) -> None:
    """The retained 200 rows are the top of the FULL canonical set by |Δ|:
    min |Δ| kept >= max |Δ| dropped (compute_affected_set returns the
    untruncated set; the FFL index is already cached so this is cheap)."""
    rows = nth_report.canonical_affected
    assert len(rows) == ROW_CAP

    def delta(f: AffectedFare) -> int:
        assert f.new_price_pence is not None and f.old_price_pence is not None
        return abs(f.new_price_pence - f.old_price_pence)

    full = compute_affected_set(_toc_change(), feed_paths)
    assert len(full.canonical) == NTH_CANONICAL_TOTAL
    kept_keys = {(f.flow_id, f.ticket_code) for f in rows}
    dropped_max = max(
        delta(f) for f in full.canonical
        if (f.flow_id, f.ticket_code) not in kept_keys
    )
    assert min(delta(f) for f in rows) >= dropped_max


@pytest.mark.slow
def test_nth_revenue_over_full_set(
    nth_report: ImpactReport, feed_paths: FeedPaths,
) -> None:
    """Revenue exposure aggregates the FULL canonical set (before row
    truncation) — an independent sum over compute_affected_set must match."""
    assert nth_report.revenue is not None
    full = compute_affected_set(_toc_change(), feed_paths)
    independent = sum(
        f.new_price_pence - f.old_price_pence
        for f in full.canonical
        if f.new_price_pence is not None and f.old_price_pence is not None
    )
    assert nth_report.revenue.per_flow_exposure_pence == independent
    assert independent < 0  # it's a discount


@pytest.mark.slow
def test_nth_blast_pairs_reference_retained_rows(nth_report: ImpactReport) -> None:
    n = len(nth_report.canonical_affected)
    pairs = nth_report.blast_radius_pairs
    assert len(pairs) == BLAST_PAIR_CAP
    assert all(0 <= p.canonical_index < n for p in pairs)


@pytest.mark.slow
def test_nth_modules_honest_at_operator_scope(nth_report: ImpactReport) -> None:
    """Splits + performance are disabled at TOC scope with explicit notes;
    compliance runs over the retained rows only and says so (partial=True)."""
    assert nth_report.splits is None
    assert nth_report.performance is None
    notes = "\n".join(nth_report.notes)
    assert "splits not computed at operator scope" in notes
    assert "performance block skipped at operator scope" in notes
    assert nth_report.compliance is not None
    assert nth_report.compliance.partial is True


@pytest.mark.slow
def test_nth_rows_carry_raw_ffl_record(nth_report: ImpactReport) -> None:
    """Every retained row's affected_set_pick step carries the raw .FFL
    T-record (fare record) fetched via the sparse-offset reader."""
    for fare in nth_report.canonical_affected:
        pick = next(s for s in fare.provenance if s.step == "affected_set_pick")
        assert pick.raw_record is not None, (fare.flow_id, fare.ticket_code)
        assert pick.raw_record.startswith("RT"), pick.raw_record[:20]
        assert fare.flow_id in pick.raw_record


@pytest.mark.slow
def test_unknown_toc_code_lists_known_tocs(feed_paths: FeedPaths) -> None:
    with pytest.raises(ValueError) as exc:
        compute_impact(_toc_change(toc_code="ZZZ"), feed_paths, include=set())
    msg = str(exc.value)
    assert "toc_code 'ZZZ' has no flows in .FFL" in msg
    assert "NTH" in msg  # the error lists the known codes
