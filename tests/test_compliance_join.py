"""Compliance join: AffectedFare × RegulationMap → ComplianceVerdict.

Three slices:
  - Fast unit tests on `check_compliance` and `attach_compliance` using
    in-memory RegulationMap fixtures (no FFL scan, no @slow marker).
  - @slow end-to-end tests using the headline demo ChangeRequest
    (`discount_categories=('01',)` — locked by the existing impact test).
    On MAN-EUS the cat '01' tickets are SOR/SOS/FOR/FOS/VCJ — none are
    regulated walk-ups under REGULATION.md §1, so the correct hand-
    computed verdict is `regulated_count == 0` and every row classified
    `not_regulated`. This is itself a load-bearing test (no false
    positives).
  - @slow tests on a "broad scope" Student-railcard change that includes
    DISCOUNT_CATEGORY '03' (SVR Off-Peak Return, the headline regulated
    walk-up) so the regulated/compliant path is exercised end-to-end.

The deliberate-breach scenario is covered twice: a FAST unit slice that
constructs an AffectedFare above an in-memory cap, and a @slow end-to-end
slice using the `raise_price` ChangeRequest kind (the cockpit's "Fare
rise" change type) — a rise on SVR/OPR is exactly what the §3 freeze
forbids, so it must produce organic breaches.

Run with:   pytest tests/test_compliance_join.py
            pytest tests/test_compliance_join.py -m slow
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.impact import (
    AffectedFare,
    AffectedSet,
    ChangeRequest,
    ComplianceVerdict,
    FeedPaths,
    ImpactReport,
    attach_compliance,
    check_compliance,
    compute_impact,
)
from src.regulation import (
    RegulationCitation,
    RegulationEntry,
    RegulationMap,
)
from src.resolver.resolve import ProvenanceStep


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

MAN_PICC_NLC = "2968"
EUSTON_NLC = "1444"


# --- Fast in-memory fixtures (no FFL scan) -----------------------------------


def _citation(section: str = "§1", rule_text: str = "Off-Peak Return walk-up") -> RegulationCitation:
    return RegulationCitation(
        section=section,
        rule_text=rule_text,
        evidence={"ticket_code": "SVR", "tkt_class": "2", "tkt_group": "S"},
    )


def _affected_fare(
    *,
    flow_id: str = "X1",
    ticket_code: str = "SVR",
    origin: str = MAN_PICC_NLC,
    dest: str = EUSTON_NLC,
    old_pence: int = 10000,
    new_pence: int = 9000,
) -> AffectedFare:
    """A minimal AffectedFare for unit-testing the compliance join."""
    return AffectedFare(
        flow_id=flow_id,
        ticket_code=ticket_code,
        route_code="00000",
        representative_origin_nlc=origin,
        representative_dest_nlc=dest,
        status="resolved",
        old_price_pence=old_pence,
        new_price_pence=new_pence,
        discount_category="01",
        provenance=(
            ProvenanceStep(
                step="affected_set_pick",
                source="(test)",
                detail={"ticket_code": ticket_code},
            ),
        ),
        blast_radius_pairs=((origin, dest),),
    )


def _regmap_with(*entries: RegulationEntry, notes: tuple[str, ...] = ()) -> RegulationMap:
    return RegulationMap(
        entries={(e.origin_nlc, e.dest_nlc, e.ticket_code): e for e in entries},
        notes=notes,
    )


# --- Test: deliberate breach (the spec's key verification) -------------------


def test_check_compliance_flags_deliberate_breach() -> None:
    """A regulated row whose new_price exceeds cap_price_2025_pence must be
    flagged as a breach with the citation reproduced. This is the second
    half of the user-stated Part A verification ('a deliberately-constructed
    change that does breach a cap ... is flagged breach')."""
    cit = _citation()
    entry = RegulationEntry(
        origin_nlc=MAN_PICC_NLC, dest_nlc=EUSTON_NLC, ticket_code="SVR",
        regulated=True, cap_price_2025_pence=10000, citation=cit,
    )
    regmap = _regmap_with(entry)
    # new_price 12000 > cap 10000 -> breach
    fare = _affected_fare(old_pence=10000, new_pence=12000)
    verdict = check_compliance(
        fare, regmap,
        corridor_origin_nlc=MAN_PICC_NLC, corridor_dest_nlc=EUSTON_NLC,
    )

    assert verdict.status == "breach"
    assert verdict.cap_price_2025_pence == 10000
    assert verdict.new_price_pence == 12000
    assert verdict.citation is cit, "citation must be reproduced verbatim"
    # Explanation must cite both the cap and the overage so the UI/JSON
    # consumer can render the breach card without re-joining.
    assert "10000" in verdict.explanation
    assert "12000" in verdict.explanation or "2000" in verdict.explanation
    assert "§1" in verdict.explanation


def test_check_compliance_compliant_when_at_cap() -> None:
    """Cap is a ceiling — equality is compliant (the boundary check is
    strict `>`, not `>=`). REGULATION.md §3: 'a regulated fare may not
    EXCEED its 1 March 2025 price.'"""
    entry = RegulationEntry(
        origin_nlc=MAN_PICC_NLC, dest_nlc=EUSTON_NLC, ticket_code="SVR",
        regulated=True, cap_price_2025_pence=10000, citation=_citation(),
    )
    regmap = _regmap_with(entry)
    fare = _affected_fare(old_pence=10000, new_pence=10000)  # exactly at cap
    verdict = check_compliance(
        fare, regmap,
        corridor_origin_nlc=MAN_PICC_NLC, corridor_dest_nlc=EUSTON_NLC,
    )
    assert verdict.status == "compliant"
    assert verdict.cap_price_2025_pence == 10000


def test_check_compliance_compliant_when_below_cap() -> None:
    """Standard discount path: new_price below cap → compliant."""
    entry = RegulationEntry(
        origin_nlc=MAN_PICC_NLC, dest_nlc=EUSTON_NLC, ticket_code="SVR",
        regulated=True, cap_price_2025_pence=10000, citation=_citation(),
    )
    regmap = _regmap_with(entry)
    fare = _affected_fare(old_pence=10000, new_pence=6700)
    verdict = check_compliance(
        fare, regmap,
        corridor_origin_nlc=MAN_PICC_NLC, corridor_dest_nlc=EUSTON_NLC,
    )
    assert verdict.status == "compliant"


def test_check_compliance_discount_on_pricier_route_not_breach() -> None:
    """The §4 fallback map cap is the corridor-CHEAPEST fare per ticket, so
    a fare on a pricier route sits above it even before the change. Its own
    current price is its fallback baseline: a discount must be compliant,
    reported against the effective cap max(map cap, old price)."""
    entry = RegulationEntry(
        origin_nlc=MAN_PICC_NLC, dest_nlc=EUSTON_NLC, ticket_code="SVR",
        regulated=True, cap_price_2025_pence=5650, citation=_citation(),
    )
    regmap = _regmap_with(entry)
    # Route-00129 SVR: 16740p discounted to 11145p — above the corridor-
    # cheapest 5650p cap but a decrease against its own price.
    fare = _affected_fare(old_pence=16740, new_pence=11145)
    verdict = check_compliance(
        fare, regmap,
        corridor_origin_nlc=MAN_PICC_NLC, corridor_dest_nlc=EUSTON_NLC,
    )
    assert verdict.status == "compliant"
    assert verdict.cap_price_2025_pence == 16740  # effective cap = old price


def test_check_compliance_increase_on_pricier_route_still_breach() -> None:
    """The effective cap only lifts to the fare's own current price — an
    INCREASE above that price still breaches the 0% freeze."""
    entry = RegulationEntry(
        origin_nlc=MAN_PICC_NLC, dest_nlc=EUSTON_NLC, ticket_code="SVR",
        regulated=True, cap_price_2025_pence=5650, citation=_citation(),
    )
    regmap = _regmap_with(entry)
    fare = _affected_fare(old_pence=16740, new_pence=17000)
    verdict = check_compliance(
        fare, regmap,
        corridor_origin_nlc=MAN_PICC_NLC, corridor_dest_nlc=EUSTON_NLC,
    )
    assert verdict.status == "breach"
    assert verdict.cap_price_2025_pence == 16740


def test_check_compliance_not_regulated_for_advance() -> None:
    """An entry with regulated=False (Advance, First Class, etc.) returns
    not_regulated. The citation is still echoed so the UI can show *why*
    it's unregulated (R2 Advance rule vs honest-gap MISSING)."""
    cit = RegulationCitation(
        section="§1", rule_text="Advance fare (excluded from regulated set)",
        evidence={"description": "ADVANCE"},
    )
    entry = RegulationEntry(
        origin_nlc=MAN_PICC_NLC, dest_nlc=EUSTON_NLC, ticket_code="C1S",
        regulated=False, cap_price_2025_pence=None, citation=cit,
    )
    regmap = _regmap_with(entry)
    fare = _affected_fare(ticket_code="C1S", old_pence=5000, new_pence=3400)
    verdict = check_compliance(
        fare, regmap,
        corridor_origin_nlc=MAN_PICC_NLC, corridor_dest_nlc=EUSTON_NLC,
    )
    assert verdict.status == "not_regulated"
    assert verdict.cap_price_2025_pence is None
    assert verdict.citation is cit


def test_check_compliance_not_regulated_when_entry_missing() -> None:
    """If the regmap has no entry at all, treat as not_regulated (citation
    None). This is the honest fallback for tickets the corridor scan
    didn't touch."""
    regmap = _regmap_with()  # empty
    fare = _affected_fare(ticket_code="ZZZ")
    verdict = check_compliance(
        fare, regmap,
        corridor_origin_nlc=MAN_PICC_NLC, corridor_dest_nlc=EUSTON_NLC,
    )
    assert verdict.status == "not_regulated"
    assert verdict.citation is None


def test_check_compliance_lookup_keyed_by_corridor_not_representative() -> None:
    """The lookup key uses the CORRIDOR NLCs, not the AffectedFare's
    representative_origin/dest_nlc. This is the bug fix that motivates
    passing corridor_origin_nlc/corridor_dest_nlc through: an AffectedFare
    produced by LOC group fan-out carries the GROUP NLC ('0438', '1072'),
    while the regulation map is keyed by the corridor NLCs ('2968', '1444').
    Looking up by the representative pair misses every group-fanned row."""
    cit = _citation()
    # Map is keyed by the corridor (2968, 1444), not by the group (0438, 1072).
    entry = RegulationEntry(
        origin_nlc=MAN_PICC_NLC, dest_nlc=EUSTON_NLC, ticket_code="SVR",
        regulated=True, cap_price_2025_pence=10000, citation=cit,
    )
    regmap = _regmap_with(entry)
    # The fare's representative pair is the group NLC, not the corridor.
    fare = _affected_fare(origin="0438", dest="1072", new_pence=9000)
    verdict = check_compliance(
        fare, regmap,
        corridor_origin_nlc=MAN_PICC_NLC, corridor_dest_nlc=EUSTON_NLC,
    )
    assert verdict.status == "compliant", (
        "lookup must use the corridor NLCs, not the fare's representative pair"
    )
    assert verdict.cap_price_2025_pence == 10000


# --- attach_compliance: preserves provenance, returns new set ----------------


def test_attach_compliance_preserves_provenance() -> None:
    """attach_compliance must NOT modify the resolver-provenance chain.
    Compliance is a separate field; the chain describes how the price was
    computed, compliance is a downstream classification. Guards the
    `tests/test_impact_demo_corridor.py:test_demo_change_provenance_shape`
    assertion that steps[-1] == 'synthetic_railcard_apply'."""
    entry = RegulationEntry(
        origin_nlc=MAN_PICC_NLC, dest_nlc=EUSTON_NLC, ticket_code="SVR",
        regulated=True, cap_price_2025_pence=10000, citation=_citation(),
    )
    regmap = _regmap_with(entry)
    fares = (
        _affected_fare(flow_id="A"),
        _affected_fare(flow_id="B"),
        _affected_fare(flow_id="C", ticket_code="XYZ"),
    )
    before = AffectedSet(
        canonical=fares, skipped=(), blast_radius=(), notes=(),
    )
    before_provs = [tuple(f.provenance) for f in before.canonical]

    after = attach_compliance(
        before, regmap,
        corridor_origin_nlc=MAN_PICC_NLC, corridor_dest_nlc=EUSTON_NLC,
    )

    after_provs = [tuple(f.provenance) for f in after.canonical]
    assert before_provs == after_provs
    for fare in after.canonical:
        assert fare.compliance is not None
        assert isinstance(fare.compliance, ComplianceVerdict)


def test_attach_compliance_returns_new_set_input_unchanged() -> None:
    """attach_compliance is pure — the input AffectedSet's rows must still
    have compliance=None after the call (the new set is a copy)."""
    entry = RegulationEntry(
        origin_nlc=MAN_PICC_NLC, dest_nlc=EUSTON_NLC, ticket_code="SVR",
        regulated=True, cap_price_2025_pence=10000, citation=_citation(),
    )
    regmap = _regmap_with(entry)
    before = AffectedSet(
        canonical=(_affected_fare(),), skipped=(), blast_radius=(), notes=(),
    )
    _ = attach_compliance(
        before, regmap,
        corridor_origin_nlc=MAN_PICC_NLC, corridor_dest_nlc=EUSTON_NLC,
    )
    assert before.canonical[0].compliance is None


# --- Slow end-to-end tests (real FFL scan) ----------------------------------


@pytest.fixture(scope="module")
def feed_paths() -> FeedPaths:
    paths = FeedPaths.default_for_data_dir(DATA)
    missing = paths.missing()
    if missing:
        pytest.skip(f"missing feed file(s): {missing}")
    return paths


@pytest.fixture(scope="module")
def demo_change() -> ChangeRequest:
    """The headline demo change (locked scope per
    tests/test_impact_demo_corridor.py). discount_categories=('01',) covers
    SOR/SOS/FOR/FOS/VCJ — none are §1 regulated walk-ups, so the correct
    hand-computed compliance result on this scope is all-not_regulated."""
    return ChangeRequest(
        kind="add_railcard",
        railcard_code="STU",
        discount_pct=1.0 / 3.0,
        discount_categories=("01",),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=True,
        description="Add Student railcard, 1/3 off, peak-valid on MAN->EUS",
    )


@pytest.fixture(scope="module")
def demo_report(feed_paths: FeedPaths, demo_change: ChangeRequest) -> ImpactReport:
    return compute_impact(demo_change, feed_paths)


@pytest.fixture(scope="module")
def regulated_scope_change() -> ChangeRequest:
    """A Student-railcard change with a BROADER scope that includes
    DISCOUNT_CATEGORY '03' (SVR Off-Peak Return) and '05' (seasons) — the
    regulated walk-ups under REGULATION.md §1. This is what exercises the
    regulated/compliant path end-to-end. The narrow `demo_change` above is
    locked by the impact test; this is the compliance-demo variant."""
    return ChangeRequest(
        kind="add_railcard",
        railcard_code="STX",                 # different code to avoid fixture confusion
        discount_pct=1.0 / 3.0,
        discount_categories=("01", "03", "05", "08"),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=True,
        description="Add Student railcard (broad scope), MAN->EUS",
    )


@pytest.fixture(scope="module")
def regulated_scope_report(
    feed_paths: FeedPaths,
    regulated_scope_change: ChangeRequest,
) -> ImpactReport:
    return compute_impact(regulated_scope_change, feed_paths)


# --- Headline demo: all rows correctly not-regulated -------------------------


@pytest.mark.slow
def test_demo_change_carries_compliance_verdict(demo_report: ImpactReport) -> None:
    """Every canonical row in the demo report has a non-None compliance
    verdict. On the narrow cat '01' scope, all 13 rows are SOR/SOS/FOR/
    FOS/VCJ — none are §1 regulated walk-ups, so regulated_count==0 is
    correct hand-computed. breach_count==0 because there are no caps to
    breach."""
    assert len(demo_report.canonical_affected) > 0
    for fare in demo_report.canonical_affected:
        assert fare.compliance is not None
        assert fare.compliance.status in ("compliant", "breach", "not_regulated")

    assert demo_report.compliance is not None
    assert demo_report.compliance.regulated_count == 0
    assert demo_report.compliance.breach_count == 0
    # Sanity: breaches tuple matches the count.
    assert len(demo_report.compliance.breaches) == 0


@pytest.mark.slow
def test_demo_change_first_class_classified_not_regulated(demo_report: ImpactReport) -> None:
    """FOR (First Class Return) is in the demo scope. Must classify as
    not_regulated under §1 (First Class excluded from the regulated set)
    with a non-None citation echoing the rule — proves the join hit the
    regmap, not a missing-entry fallthrough."""
    for_rows = [f for f in demo_report.canonical_affected if f.ticket_code == "FOR"]
    assert for_rows, "expected at least one FOR canonical row on MAN-EUS cat '01'"
    fare = for_rows[0]
    assert fare.compliance is not None
    assert fare.compliance.status == "not_regulated"
    assert fare.compliance.citation is not None, (
        "FOR must have a citation — proves the regmap entry was hit, not "
        "fallthrough to 'no entry'"
    )
    assert fare.compliance.citation.section == "§1"
    assert "First Class" in fare.compliance.citation.rule_text


# --- Broad scope: regulated/compliant path -----------------------------------


@pytest.mark.slow
def test_regulated_scope_has_at_least_one_regulated_row(
    regulated_scope_report: ImpactReport,
) -> None:
    """The broader scope (cats '01', '03', '05', '08') must hit at least
    one regulated walk-up. SVR is the headline (Off-Peak Return,
    REGULATION.md §1) and lives in DISCOUNT_CATEGORY '03'."""
    assert regulated_scope_report.compliance is not None
    assert regulated_scope_report.compliance.regulated_count >= 1, (
        "broad-scope Student-railcard change should touch at least one "
        f"regulated walk-up; got {regulated_scope_report.compliance.regulated_count}"
    )


@pytest.mark.slow
def test_regulated_scope_svr_rows_classify_with_off_peak_return_citation(
    regulated_scope_report: ImpactReport,
) -> None:
    """Every SVR row must classify as regulated (status in {compliant,
    breach}, never not_regulated) and cite §1 Off-Peak Return — proves the
    regmap join hit and used the right rule.

    The map's §4 fallback cap is the CHEAPEST current SVR on the corridor,
    but the check uses max(map cap, the row's own old price) as the
    effective cap — so a discounted SVR on a pricier route is compliant
    (its own current price is its fallback baseline), not a false breach.
    Sourcing the true 1 Mar 2025 reference remains the v2 fix."""
    svr_rows = [
        f for f in regulated_scope_report.canonical_affected
        if f.ticket_code == "SVR"
    ]
    assert svr_rows, "expected at least one SVR canonical row on broad-scope change"
    for fare in svr_rows:
        assert fare.compliance is not None
        assert fare.compliance.status in ("compliant", "breach")
        assert fare.compliance.cap_price_2025_pence is not None
        assert fare.compliance.cap_price_2025_pence > 0
        assert fare.compliance.citation is not None
        assert fare.compliance.citation.section == "§1"
        assert "Off-Peak Return" in fare.compliance.citation.rule_text


@pytest.mark.slow
def test_regulated_scope_compliance_decision_matches_arithmetic(
    regulated_scope_report: ImpactReport,
) -> None:
    """For every row that came back with a cap, the status decision must
    match the new_price-vs-cap arithmetic (strict `>` boundary). This
    guards the compliance check against drift between the in-engine
    decision and the documented rule."""
    for fare in regulated_scope_report.canonical_affected:
        if fare.compliance is None or fare.compliance.cap_price_2025_pence is None:
            continue
        cap = fare.compliance.cap_price_2025_pence
        assert fare.new_price_pence is not None
        if fare.new_price_pence > cap:
            assert fare.compliance.status == "breach", (
                f"row {fare.flow_id}/{fare.ticket_code}: "
                f"new {fare.new_price_pence} > cap {cap} but status "
                f"{fare.compliance.status!r}"
            )
        else:
            assert fare.compliance.status == "compliant", (
                f"row {fare.flow_id}/{fare.ticket_code}: "
                f"new {fare.new_price_pence} <= cap {cap} but status "
                f"{fare.compliance.status!r}"
            )


@pytest.mark.slow
def test_regulated_scope_breach_carries_full_evidence(
    regulated_scope_report: ImpactReport,
) -> None:
    """When the §4 fallback flags a breach, the breach row's compliance
    must carry: a citation, the cap, the new_price, and an explanation
    that cites both numbers. This is what the demo's red-card UI binds to.

    Skipped if no breaches fired on this snapshot (in which case the fast
    deliberate-breach unit test still covers the path)."""
    assert regulated_scope_report.compliance is not None
    if regulated_scope_report.compliance.breach_count == 0:
        pytest.skip("no organic breaches on this snapshot")
    for fare in regulated_scope_report.compliance.breaches:
        assert fare.compliance is not None
        assert fare.compliance.status == "breach"
        assert fare.compliance.cap_price_2025_pence is not None
        assert fare.compliance.new_price_pence > fare.compliance.cap_price_2025_pence
        assert fare.compliance.citation is not None
        assert "BREACH" in fare.compliance.explanation
        assert str(fare.compliance.cap_price_2025_pence) in fare.compliance.explanation


# --- raise_price: the organic-breach path -------------------------------------


@pytest.fixture(scope="module")
def raise_change() -> ChangeRequest:
    """A 5% across-the-board rise on the walk-up scope (cats '01','03','08').
    SVR/OPR are regulated under §1 and any increase breaches the 0% freeze
    (effective cap = max(map cap, the fare's own old price))."""
    return ChangeRequest(
        kind="raise_price",
        railcard_code="RSE",
        discount_pct=0.05,
        discount_categories=("01", "03", "08"),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=True,
        description="Raise walk-up fares 5% on MAN->EUS",
    )


@pytest.fixture(scope="module")
def raise_report(feed_paths: FeedPaths, raise_change: ChangeRequest) -> ImpactReport:
    return compute_impact(raise_change, feed_paths)


def test_raise_price_math_is_an_increase() -> None:
    """apply_synthetic_railcard with kind='raise_price' must move the price
    UP by floor(old * pct) before rounding — sign-flipped railcard math."""
    from src.impact.synthetic_railcard import apply_synthetic_railcard

    change = ChangeRequest(
        kind="raise_price",
        railcard_code="RSE",
        discount_pct=0.05,
        discount_categories=("01",),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=False,
        description="unit-test rise",
        rounding_rule="none",
    )
    new, step = apply_synthetic_railcard(10000, change)
    assert new == 10500
    assert step.step == "synthetic_railcard_apply"
    assert step.detail["kind"] == "raise_price"


@pytest.mark.slow
def test_raise_price_produces_organic_breaches(raise_report: ImpactReport) -> None:
    """A rise on the regulated walk-ups MUST come back as breaches: every
    regulated row's new price exceeds its own old price (the §4 effective
    cap), so regulated_count == breach_count > 0. This is the red-card demo
    scenario the cockpit's 'Fare rise' change type drives."""
    assert raise_report.compliance is not None
    comp = raise_report.compliance
    assert comp.regulated_count > 0, "rise scope must touch SVR/OPR"
    assert comp.breach_count == comp.regulated_count, (
        "every regulated row under a rise must breach the 0% freeze; "
        f"regulated={comp.regulated_count}, breaches={comp.breach_count}"
    )
    for fare in comp.breaches:
        assert fare.old_price_pence is not None
        assert fare.new_price_pence is not None
        assert fare.new_price_pence > fare.old_price_pence
        assert fare.compliance is not None
        assert fare.compliance.status == "breach"
        assert "BREACH" in fare.compliance.explanation


@pytest.mark.slow
def test_raise_price_skips_rlc_collision_check(feed_paths: FeedPaths) -> None:
    """raise_price creates no railcard, so a code that exists in .RLC must
    NOT be rejected (the collision check is add_railcard-only)."""
    from src.impact.change_request import validate_against_feed

    change = ChangeRequest(
        kind="raise_price",
        railcard_code="YNG",   # a real .RLC code — would fail for add_railcard
        discount_pct=0.05,
        discount_categories=("01",),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=False,
        description="rise with a feed railcard code",
    )
    outcome = validate_against_feed(change, feed_paths)
    assert outcome.ok, f"unexpected errors: {outcome.errors}"


# --- Disclosure invariants --------------------------------------------------


@pytest.mark.slow
def test_compute_impact_notes_include_baseline_disclosure(demo_report: ImpactReport) -> None:
    """The regmap's §4 baseline-fallback disclosure must surface on
    regulation_map_notes so the UI/judges see that cap_price = current
    snapshot (not the true 1 Mar 2025 reference)."""
    assert demo_report.compliance is not None
    joined = " | ".join(demo_report.compliance.regulation_map_notes)
    assert "1 March 2025" in joined or "REGULATION.md §4" in joined, (
        f"expected baseline disclosure in regulation_map_notes; got: {joined!r}"
    )


@pytest.mark.slow
def test_compute_impact_notes_include_london_inference_caveat(
    demo_report: ImpactReport,
) -> None:
    """The is_london_flow hardcoded-set inference must be disclosed in
    notes so it's visible to a reviewer (an honest gap, not silent magic)."""
    joined = " | ".join(demo_report.notes)
    assert "is_london_flow" in joined or "London-terminals" in joined


@pytest.mark.slow
def test_breaches_subset_of_canonical_affected(demo_report: ImpactReport) -> None:
    """Structural invariant: every entry in `breaches` must be in
    `canonical_affected` with compliance.status == 'breach'."""
    assert demo_report.compliance is not None
    canonical_ids = {(f.flow_id, f.ticket_code) for f in demo_report.canonical_affected}
    for f in demo_report.compliance.breaches:
        assert (f.flow_id, f.ticket_code) in canonical_ids
        assert f.compliance is not None and f.compliance.status == "breach"
