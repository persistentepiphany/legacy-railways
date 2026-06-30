"""Split-ticket / fare-arbitrage detection.

First plugin built on the modular ImpactReport contract. Rides on the
existing deterministic resolver — no new data sources — and reports where
buying two tickets (origin→intermediate + intermediate→dest) is cheaper
than the through-ticket on a corridor.

The CHANGE-PATH twist: given a ChangeRequest, compute splits twice (pre
and post-change) and diff them, surfacing the split opportunities the
change CREATES (didn't exist before, exist now) or CLOSES (existed
before, gone now). This is the unique angle vs every retail splitter:
we see how a regulatory/commercial change moves the splittability of
the network.

Honest constraints surfaced in `notes` on every result:
  - A legally valid split requires the train to actually call at the
    split station (NRCoT Cond. 14). We don't have calling-pattern data
    wired in yet, so candidates are constrained to LOC-known NLCs near
    the corridor; full call-pattern validity is DEFERRED, not faked.
  - Through-price uses the same disambiguation as the resolver. If a
    split point's leg fails to resolve (no_flow / no_fare /
    contradiction / suppressed), that candidate is marked
    `unresolvable` — never silently dropped, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.ingest.inspect import (
    LocationMeta,
    load_loc_meta,
    load_ticket_type_meta,
)
from src.ingest.timetable import (
    TimetableIndex,
    intermediate_calls,
    load_timetable_index,
)
from src.resolver.resolve import ProvenanceStep, ResolvedFare, resolve_fare

from src.impact.change_request import ChangeRequest
from src.impact.feed_paths import FeedPaths
from src.impact.synthetic_railcard import apply_synthetic_railcard


SplitStatus = Literal[
    "opportunity",      # saving_pence > 0
    "no_saving",        # split sum >= through (no arbitrage on this candidate)
    "unresolvable",     # one or more legs (or the through) couldn't be resolved
]


# Demo corridor intermediates (Manchester Piccadilly <-> London Euston,
# Avanti West Coast main line). Real, well-known split points on the
# WCML; constrained to NLCs that exist in .LOC at compute time, dropped
# otherwise. Source of truth for these NLCs is .LOC; this list is the
# whitelist of CANDIDATES to consider — NOT a claim the train calls at
# all of them (that's the NRCoT Cond. 14 deferral). v2 will derive
# candidates from a timetable feed.
DEMO_CORRIDOR_INTERMEDIATES: tuple[str, ...] = (
    "1243",   # Crewe (CRE)
    "1314",   # Stoke-on-Trent (SOT)
    "1268",   # Stafford (STA)
    "1087",   # Rugby (RUG)
    "1378",   # Milton Keynes Central (MKC)
    "2771",   # Stockport (SPT) — first stop out of MAN on WCML
)


@dataclass(frozen=True)
class SplitCandidate:
    """One intermediate evaluated for through-vs-split arbitrage."""
    intermediate_nlc: str
    ticket_code: str
    route_code: str | None
    through_price_pence: int | None
    leg1_price_pence: int | None        # origin -> intermediate
    leg2_price_pence: int | None        # intermediate -> dest
    split_total_pence: int | None       # leg1 + leg2 (None if either leg unresolvable)
    saving_pence: int                   # through - split_total; 0 if either is None
    status: SplitStatus
    provenance: tuple[ProvenanceStep, ...]   # concat of the 3 resolver calls
    explanation: str                    # human-readable summary for the UI


@dataclass(frozen=True)
class SplitOpportunityResult:
    """A SplitOpportunityResult block on an ImpactReport.

    `pre_change` is splits on the baseline. `post_change` is the same
    candidates re-evaluated with the ChangeRequest's discount applied to
    any leg whose ticket falls in the change's scope. `created` and
    `closed` are the diff: where the status flipped to/from `opportunity`."""
    corridor_origin_nlc: str
    corridor_dest_nlc: str
    ticket_code: str
    route_code: str | None
    pre_change: tuple[SplitCandidate, ...]
    post_change: tuple[SplitCandidate, ...]
    created: tuple[SplitCandidate, ...]    # opportunities that exist post-change only
    closed: tuple[SplitCandidate, ...]     # opportunities that existed pre-change only
    notes: tuple[str, ...]


# NRCoT Cond. 14 — split legality requires the train to actually call at
# the split point. Two forms of disclosure, mutually exclusive: the
# deferred form when no timetable is wired in, the verified form when the
# RSPS5046 CIF feed has been loaded and call patterns confirm the points.
_NRCOT_14_DEFERRED = (
    "Split validity NOT verified: NRCoT Cond. 14 requires the train to "
    "actually call at the split point. Calling-pattern (timetable) data is "
    "not loaded for this snapshot, so listed opportunities are arbitrage "
    "candidates only. Candidates are constrained to LOC-known NLCs; unknown "
    "NLCs are dropped, never substituted."
)


def _nrcot_14_verified(snapshot_filename: str) -> str:
    return (
        "Split validity call-pattern-verified per NRCoT Cond. 14: every "
        f"intermediate listed is a CRS where a passenger train serving the "
        f"corridor calls in the RSPS5046 timetable snapshot "
        f"{snapshot_filename!r}. STP overlays / cancellations and "
        "association (joining/dividing) records are NOT merged in this "
        "snapshot — calling pattern reflects the Permanent (P) and STP-new "
        "(N) schedules only; day-of-week and date-specific masks are not "
        "applied (i.e. 'ever calls at', not 'calls at on date X')."
    )


def _filter_intermediates(
    intermediates: tuple[str, ...],
    loc: dict[str, LocationMeta],
    corridor_o: str,
    corridor_d: str,
) -> tuple[list[str], list[str]]:
    """Drop intermediates that are unknown, equal to the corridor endpoints,
    or duplicates. Returns (kept, dropped_reasons)."""
    kept: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for nlc in intermediates:
        if nlc in seen:
            continue
        seen.add(nlc)
        if nlc in (corridor_o, corridor_d):
            dropped.append(f"{nlc} (= corridor endpoint)")
            continue
        if nlc not in loc:
            dropped.append(f"{nlc} (not in .LOC)")
            continue
        kept.append(nlc)
    return kept, dropped


def _resolve_leg(
    o: str, d: str, ticket: str, fp: FeedPaths,
    *, route_code: str | None, railcard_code: str | None,
) -> ResolvedFare:
    """Thin wrapper so the call site reads as 3 symmetric resolver calls."""
    return resolve_fare(
        o, d, ticket, fp.ffl,
        loc_path=fp.loc, fsc_path=fp.fsc, nfo_path=fp.nfo,
        rlc_path=fp.rlc, dis_path=fp.dis, rcm_path=fp.rcm,
        frr_path=fp.frr, tty_path=fp.tty,
        route_code=route_code, railcard_code=railcard_code,
    )


def _candidate(
    *,
    intermediate: str,
    ticket: str,
    route_code: str | None,
    through: ResolvedFare,
    leg1: ResolvedFare,
    leg2: ResolvedFare,
) -> SplitCandidate:
    """Assemble one SplitCandidate from three resolver outcomes."""
    provenance = tuple(through.provenance) + tuple(leg1.provenance) + tuple(leg2.provenance)
    through_p = through.price_pence
    leg1_p = leg1.price_pence
    leg2_p = leg2.price_pence
    if through_p is None or leg1_p is None or leg2_p is None:
        # Any unresolvable leg poisons the comparison. Surface the
        # statuses so the UI shows WHICH leg failed (audit trail).
        explanation = (
            f"unresolvable: through={through.status}, "
            f"leg1={leg1.status}, leg2={leg2.status}"
        )
        return SplitCandidate(
            intermediate_nlc=intermediate,
            ticket_code=ticket,
            route_code=route_code,
            through_price_pence=through_p,
            leg1_price_pence=leg1_p,
            leg2_price_pence=leg2_p,
            split_total_pence=None,
            saving_pence=0,
            status="unresolvable",
            provenance=provenance,
            explanation=explanation,
        )
    split_total = leg1_p + leg2_p
    saving = through_p - split_total
    if saving > 0:
        status: SplitStatus = "opportunity"
        explanation = (
            f"split via {intermediate} saves {saving}p "
            f"(through {through_p}p vs split {split_total}p = "
            f"{leg1_p}p + {leg2_p}p)"
        )
    else:
        status = "no_saving"
        explanation = (
            f"no saving via {intermediate} "
            f"(through {through_p}p <= split {split_total}p)"
        )
    return SplitCandidate(
        intermediate_nlc=intermediate,
        ticket_code=ticket,
        route_code=route_code,
        through_price_pence=through_p,
        leg1_price_pence=leg1_p,
        leg2_price_pence=leg2_p,
        split_total_pence=split_total,
        saving_pence=saving,
        status=status,
        provenance=provenance,
        explanation=explanation,
    )


def _intermediates_from_timetable(
    loc: dict[str, LocationMeta],
    timetable_idx: TimetableIndex,
    corridor_origin_nlc: str,
    corridor_dest_nlc: str,
) -> tuple[tuple[str, ...], str | None]:
    """Derive corridor intermediates from real CIF calling patterns.

    Resolves the corridor endpoints to CRS via .LOC, queries the timetable
    index for every CRS called at between them across all passenger
    services, then maps those CRSes back to NLCs via a reverse-LOC scan.
    Returns (nlcs_in_loc_order, reason_if_empty).
    """
    crs_to_nlc: dict[str, str] = {}
    o_crs: str | None = None
    d_crs: str | None = None
    for nlc, meta in loc.items():
        if meta.crs:
            crs_to_nlc.setdefault(meta.crs, nlc)
        if nlc == corridor_origin_nlc:
            o_crs = meta.crs or None
        if nlc == corridor_dest_nlc:
            d_crs = meta.crs or None

    # Cluster/group NLCs (e.g. London Terminals 1072) have no CRS of their
    # own — their members do. If the corridor endpoint is a cluster NLC,
    # pick a representative member's CRS instead.
    if o_crs is None:
        for nlc, meta in loc.items():
            if meta.group_nlc == corridor_origin_nlc and meta.crs:
                o_crs = meta.crs
                break
    if d_crs is None:
        for nlc, meta in loc.items():
            if meta.group_nlc == corridor_dest_nlc and meta.crs:
                d_crs = meta.crs
                break

    if o_crs is None or d_crs is None:
        return ((), f"corridor endpoints have no CRS in .LOC (origin={o_crs!r}, dest={d_crs!r})")

    crses = intermediate_calls(timetable_idx, o_crs, d_crs)
    if not crses:
        return ((), f"no passenger train calls between {o_crs}->{d_crs} in timetable snapshot")

    derived: list[str] = []
    skipped_crs: list[str] = []
    for crs in crses:
        nlc = crs_to_nlc.get(crs)
        if nlc is None:
            skipped_crs.append(crs)
            continue
        derived.append(nlc)
    if skipped_crs:
        # Surface dropped CRSes in the caller's notes via the empty-reason
        # mechanism? No — we already returned a non-empty list; surface
        # via the kept tuple. Caller appends its own note about LOC drops.
        pass
    return (tuple(derived), None)


def detect_splits(
    corridor_origin_nlc: str,
    corridor_dest_nlc: str,
    ticket_code: str,
    feed_paths: FeedPaths,
    *,
    intermediates: tuple[str, ...] | None = None,
    route_code: str | None = None,
    railcard_code: str | None = None,
) -> SplitOpportunityResult:
    """Baseline (no change applied) split detection on one corridor + ticket.

    Intermediate-resolution order:
      1. Explicit `intermediates` arg (tests pass a known fixture).
      2. Timetable-derived if `feed_paths.timetable_mca` exists and the
         corridor has CRS endpoints.
      3. Hardcoded `DEMO_CORRIDOR_INTERMEDIATES` whitelist.
    The resulting `notes` carries the NRCoT Cond. 14 disclosure that
    matches the source actually used — verified or deferred.

    Performs at most `1 + 2N` resolver calls (through-fare reused across
    candidates). Output is deterministic: candidates sorted by NLC.
    """
    loc = load_loc_meta(feed_paths.loc)

    source_intermediates: tuple[str, ...]
    source_note: str
    timetable_attempted = False
    if intermediates is not None:
        source_intermediates = intermediates
        source_note = _NRCOT_14_DEFERRED
    elif feed_paths.timetable_mca is not None and feed_paths.timetable_mca.exists():
        timetable_attempted = True
        idx = load_timetable_index(feed_paths.timetable_mca)
        derived, reason = _intermediates_from_timetable(
            loc, idx, corridor_origin_nlc, corridor_dest_nlc,
        )
        if derived:
            source_intermediates = derived
            source_note = _nrcot_14_verified(idx.source_file)
        else:
            # Timetable wired but corridor unrepresented in this snapshot —
            # fall back to whitelist with an honest reason in notes.
            source_intermediates = DEMO_CORRIDOR_INTERMEDIATES
            source_note = (
                _NRCOT_14_DEFERRED
                + f" (timetable snapshot loaded but {reason}; falling back to whitelist)"
            )
    else:
        source_intermediates = DEMO_CORRIDOR_INTERMEDIATES
        source_note = _NRCOT_14_DEFERRED

    kept, dropped = _filter_intermediates(
        source_intermediates, loc, corridor_origin_nlc, corridor_dest_nlc,
    )

    notes: list[str] = [source_note]
    if dropped:
        notes.append(
            "dropped intermediate(s) from the candidate list: "
            + ", ".join(dropped)
        )
    if not timetable_attempted and intermediates is None:
        notes.append(
            "timetable .MCA not present in data/; intermediates are the "
            "hardcoded WCML whitelist. Drop a RJTTF*.MCA into data/ for "
            "call-pattern-verified candidates."
        )

    through = _resolve_leg(
        corridor_origin_nlc, corridor_dest_nlc, ticket_code, feed_paths,
        route_code=route_code, railcard_code=railcard_code,
    )

    candidates: list[SplitCandidate] = []
    for inter in sorted(kept):
        leg1 = _resolve_leg(
            corridor_origin_nlc, inter, ticket_code, feed_paths,
            route_code=None, railcard_code=railcard_code,
        )
        leg2 = _resolve_leg(
            inter, corridor_dest_nlc, ticket_code, feed_paths,
            route_code=None, railcard_code=railcard_code,
        )
        candidates.append(_candidate(
            intermediate=inter, ticket=ticket_code, route_code=route_code,
            through=through, leg1=leg1, leg2=leg2,
        ))

    return SplitOpportunityResult(
        corridor_origin_nlc=corridor_origin_nlc,
        corridor_dest_nlc=corridor_dest_nlc,
        ticket_code=ticket_code,
        route_code=route_code,
        pre_change=tuple(candidates),
        post_change=(),
        created=(),
        closed=(),
        notes=tuple(notes),
    )


def _apply_change_to_candidate(
    cand: SplitCandidate,
    change: ChangeRequest,
    ticket_in_scope: bool,
) -> SplitCandidate:
    """Re-derive a candidate with the ChangeRequest's discount applied to
    any in-scope leg. The resolver re-runs are not needed (the synthetic
    discount is a pure function of pence + discount_pct); we apply it to
    the already-resolved prices. Provenance gets one extra step per
    repriced leg, marked `synthetic_railcard_apply` to mirror the
    affected-set pipeline."""
    if not ticket_in_scope or cand.status == "unresolvable":
        # Out of scope or no usable prices to reprice — return unchanged.
        return cand
    # The change discounts the SAME ticket on every leg (same ticket_code
    # by construction). Apply to all three pence values.
    assert cand.through_price_pence is not None
    assert cand.leg1_price_pence is not None
    assert cand.leg2_price_pence is not None
    new_through, prov_through = apply_synthetic_railcard(cand.through_price_pence, change)
    new_leg1, prov_leg1 = apply_synthetic_railcard(cand.leg1_price_pence, change)
    new_leg2, prov_leg2 = apply_synthetic_railcard(cand.leg2_price_pence, change)
    split_total = new_leg1 + new_leg2
    saving = new_through - split_total
    if saving > 0:
        status: SplitStatus = "opportunity"
        explanation = (
            f"post-change split via {cand.intermediate_nlc} saves {saving}p "
            f"(through {new_through}p vs split {split_total}p)"
        )
    else:
        status = "no_saving"
        explanation = (
            f"post-change no saving via {cand.intermediate_nlc} "
            f"(through {new_through}p <= split {split_total}p)"
        )
    return SplitCandidate(
        intermediate_nlc=cand.intermediate_nlc,
        ticket_code=cand.ticket_code,
        route_code=cand.route_code,
        through_price_pence=new_through,
        leg1_price_pence=new_leg1,
        leg2_price_pence=new_leg2,
        split_total_pence=split_total,
        saving_pence=saving,
        status=status,
        provenance=cand.provenance + (prov_through, prov_leg1, prov_leg2),
        explanation=explanation,
    )


def splits_for_change(
    change: ChangeRequest,
    feed_paths: FeedPaths,
    *,
    ticket_code: str = "SOS",
    intermediates: tuple[str, ...] | None = None,
    route_code: str | None = None,
) -> SplitOpportunityResult:
    """Pre-vs-post-change split detection on the change's corridor.

    Default ticket = SOS (Standard Off-Peak Single) — the most common
    walk-up on the demo corridor. Caller can override.

    The post-change pricing: if the chosen ticket's .TTY DISCOUNT_CATEGORY
    is in the change's scope, apply the synthetic discount to every leg's
    baseline price; otherwise post-change == pre-change (no flip). This
    matches the affected-set logic exactly (same `apply_synthetic_railcard`)
    so the same change can't produce divergent answers across modules."""
    pre = detect_splits(
        change.corridor_origin_nlc,
        change.corridor_dest_nlc,
        ticket_code,
        feed_paths,
        intermediates=intermediates,
        route_code=route_code,
    )

    tty = load_ticket_type_meta(feed_paths.tty)
    ttyr = tty.get(ticket_code)
    in_scope = (
        ttyr is not None and ttyr.discount_category in change.discount_categories
    )

    post = tuple(
        _apply_change_to_candidate(c, change, in_scope) for c in pre.pre_change
    )
    pre_opportunities = {
        c.intermediate_nlc for c in pre.pre_change if c.status == "opportunity"
    }
    post_opportunities = {
        c.intermediate_nlc for c in post if c.status == "opportunity"
    }
    created = tuple(
        c for c in post if c.intermediate_nlc in (post_opportunities - pre_opportunities)
    )
    closed = tuple(
        c for c in pre.pre_change
        if c.intermediate_nlc in (pre_opportunities - post_opportunities)
    )

    extra_notes: list[str] = []
    if not in_scope:
        extra_notes.append(
            f"ticket {ticket_code!r} discount_category not in change scope "
            f"{list(change.discount_categories)}; post-change splits identical to pre-change "
            "(no created/closed opportunities possible)"
        )

    return SplitOpportunityResult(
        corridor_origin_nlc=change.corridor_origin_nlc,
        corridor_dest_nlc=change.corridor_dest_nlc,
        ticket_code=ticket_code,
        route_code=route_code,
        pre_change=pre.pre_change,
        post_change=post,
        created=created,
        closed=closed,
        notes=pre.notes + tuple(extra_notes),
    )


__all__ = [
    "DEMO_CORRIDOR_INTERMEDIATES",
    "SplitCandidate",
    "SplitOpportunityResult",
    "SplitStatus",
    "detect_splits",
    "splits_for_change",
]
