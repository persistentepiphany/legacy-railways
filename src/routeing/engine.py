"""Deterministic routeing / validity engine.

`check_validity(query, feed_paths)` returns a ValidityVerdict for a given
journey: is it permitted under the National Routeing Guide, which
easements fire, and where in the .RG* files did each decision come from.

Contract (mirrors the fare resolver's discipline):

  * Pure and side-effect-free (no network, no LLM at runtime).
  * Provenance is part of the return type from line one; never bolted on.
  * Never silently guesses.  When feed data is missing (routeing bundle
    not yet downloaded) we return `status="unknown_no_data"` with an
    explicit note.  When positive and negative easements collide we
    return `status="contradiction"` and escalate.
  * The base permitted-routes table is authoritative for a normal
    journey; positive easements can grant permission the base table
    denies; negative easements deny permission the base table grants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Literal

from src.impact.feed_paths import FeedPaths
from src.ingest.routeing import (
    PermittedRoute,
    load_easement_texts,
    load_easement_tocs,
    load_easements,
    load_permitted_routes,
    load_routeing_points,
    load_station_groups,
    load_stations,
)
from src.routeing.easements import EasementMatch, match_easement


ValidityStatus = Literal[
    "permitted",             # base permitted route exists (and no negative easement blocks it)
    "permitted_by_easement", # only a positive easement grants permission
    "not_permitted",         # no route and no positive easement
    "denied_by_easement",    # base route exists but a negative easement blocks it
    "contradiction",         # positive AND negative easements both fire — escalate
    "unknown_no_data",       # routeing bundle absent from data/ — cannot answer
    "unknown_origin",        # origin CRS not in .RGS
    "unknown_dest",          # dest CRS not in .RGS
]


@dataclass(frozen=True)
class ProvenanceLine:
    step: str
    source: str
    detail: dict[str, str]


@dataclass(frozen=True)
class JourneyQuery:
    origin_crs: str
    dest_crs: str
    via_crs: tuple[str, ...] = ()
    ticket_code: str | None = None
    route_code: str | None = None
    toc: str | None = None
    train_uid: str | None = None
    query_date: _date | None = None
    query_time_hhmm: str | None = None


@dataclass(frozen=True)
class ValidityVerdict:
    query: JourneyQuery
    status: ValidityStatus
    origin_routeing_points: tuple[str, ...] = ()
    dest_routeing_points: tuple[str, ...] = ()
    permitted_routes: tuple[PermittedRoute, ...] = ()
    firing_positive: tuple[EasementMatch, ...] = ()
    firing_negative: tuple[EasementMatch, ...] = ()
    considered_easement_refs: tuple[str, ...] = ()
    easement_texts: dict[str, str] = field(default_factory=dict)
    provenance: tuple[ProvenanceLine, ...] = ()
    notes: tuple[str, ...] = ()


# --- Public entry point ---------------------------------------------------


def check_validity(query: JourneyQuery, feed_paths: FeedPaths) -> ValidityVerdict:
    """Deterministic routeing verdict with provenance.  See module docstring."""
    prov: list[ProvenanceLine] = []
    notes: list[str] = []

    # Cheap guard: is the routeing bundle present at all?
    required = (feed_paths.rgs, feed_paths.rgr, feed_paths.rgf)
    if any(p is None for p in required):
        notes.append(
            "routeing bundle absent from data/: at least one of .RGS/.RGR/.RGF "
            "is missing. Pull RSPS5047 via NRDP (see docs) to enable the engine."
        )
        return ValidityVerdict(
            query=query,
            status="unknown_no_data",
            provenance=tuple(prov),
            notes=tuple(notes),
        )
    assert feed_paths.rgs is not None
    assert feed_paths.rgr is not None
    assert feed_paths.rgf is not None

    stations = load_stations(feed_paths.rgs)
    groups = (
        load_station_groups(feed_paths.rgg) if feed_paths.rgg is not None else {}
    )
    permitted = load_permitted_routes(feed_paths.rgr)
    routeing_pts = (
        load_routeing_points(feed_paths.rgp) if feed_paths.rgp is not None else frozenset()
    )
    easements = load_easements(feed_paths.rgf)
    texts = (
        load_easement_texts(feed_paths.rge) if feed_paths.rge is not None else {}
    )
    toc_map = (
        load_easement_tocs(feed_paths.rgh) if feed_paths.rgh is not None else {}
    )

    # --- Step 1: resolve endpoints to routeing points --------------------
    origin_rps, origin_note = _resolve_routeing_points(
        query.origin_crs, stations, routeing_pts, feed_paths.rgs, prov, role="origin",
    )
    if origin_note:
        notes.append(origin_note)
    if not origin_rps:
        return ValidityVerdict(
            query=query, status="unknown_origin",
            provenance=tuple(prov), notes=tuple(notes),
        )

    dest_rps, dest_note = _resolve_routeing_points(
        query.dest_crs, stations, routeing_pts, feed_paths.rgs, prov, role="dest",
    )
    _ = groups  # station-group main-station table retained for future UI use
    if dest_note:
        notes.append(dest_note)
    if not dest_rps:
        return ValidityVerdict(
            query=query, status="unknown_dest",
            origin_routeing_points=tuple(origin_rps),
            provenance=tuple(prov), notes=tuple(notes),
        )

    # --- Step 2: look up permitted routes for every RP cross-product -----
    routes = _lookup_permitted_routes(
        origin_rps, dest_rps, permitted, feed_paths.rgr, prov,
    )

    # --- Step 3: evaluate every easement against the journey -------------
    firing_pos: list[EasementMatch] = []
    firing_neg: list[EasementMatch] = []
    considered: list[str] = []
    text_out: dict[str, str] = {}

    for ref, bundle in easements.items():
        m = match_easement(
            bundle,
            origin_crs=query.origin_crs,
            dest_crs=query.dest_crs,
            via_crs=query.via_crs,
            ticket_code=query.ticket_code,
            route_code=query.route_code,
            toc=query.toc,
            train_uid=query.train_uid,
            query_date=query.query_date,
            query_time_hhmm=query.query_time_hhmm,
        )
        # We only record considered easements whose scope was even
        # plausible for this journey (i.e. not "outside_window"), so the
        # UI trace isn't polluted with 50k irrelevant refs.
        if m.outcome == "outside_window":
            continue
        considered.append(ref)
        if m.outcome != "match":
            continue
        # Attach text + publishing TOC where available for the UI trace.
        text_ref = bundle.header.text_ref
        text_row = texts.get(text_ref)
        if text_row is not None:
            text_out[ref] = text_row.text
        prov.append(ProvenanceLine(
            step="easement_fires",
            source=f"{feed_paths.rgf.name}:E-record for {ref}",
            detail={
                "easement_ref": ref,
                "class": "positive" if m.is_positive else "negative",
                "text_ref": text_ref,
                "publishing_toc": toc_map.get(text_ref, ""),
                "why": " | ".join(m.reasons),
            },
        ))
        if m.is_positive:
            firing_pos.append(m)
        else:
            firing_neg.append(m)

    # --- Step 4: resolve to a verdict ------------------------------------
    status = _verdict(routes, firing_pos, firing_neg, prov)

    return ValidityVerdict(
        query=query,
        status=status,
        origin_routeing_points=tuple(origin_rps),
        dest_routeing_points=tuple(dest_rps),
        permitted_routes=routes,
        firing_positive=tuple(firing_pos),
        firing_negative=tuple(firing_neg),
        considered_easement_refs=tuple(considered),
        easement_texts=text_out,
        provenance=tuple(prov),
        notes=tuple(notes),
    )


# --- Step helpers ---------------------------------------------------------


def _resolve_routeing_points(
    crs: str,
    stations: dict,
    routeing_pts: frozenset[str],
    rgs_path: Path,
    prov: list[ProvenanceLine],
    *,
    role: str,
) -> tuple[list[str], str | None]:
    """Return the routeing points a station resolves to per RSPS5047 § 6.2.

    Spec § 6.2.1.2: "Stations that are routeing points, OR members of
    station groups which are routeing points, will have no related
    routeing points."  So an empty `routeing_points` field can mean
    either:
      (a) the station itself is in .RGP -> use its CRS, or
      (b) the station is in a group whose group_id is in .RGP -> use
          the group_id (that is the .RGR lookup key, e.g. G01 for
          London Terminals, G20 for Manchester).

    We consult .RGP to distinguish.  If neither the CRS nor the group is
    a routeing point in its own right, we fall back to the CRS (with a
    note) rather than fabricate."""
    rec = stations.get(crs)
    if rec is None:
        return [], f"{role} CRS {crs!r} not in .RGS (RSPS5047 § 6.2)"

    if rec.routeing_points:
        rps = list(rec.routeing_points)
        rp_source = ".RGS related RPs"
    elif rec.station_group_id and rec.station_group_id in routeing_pts:
        # Case (b) — the group IS the routeing point.
        rps = [rec.station_group_id]
        rp_source = f".RGS group_id={rec.station_group_id} (in .RGP)"
    elif crs in routeing_pts:
        # Case (a) — the CRS itself is in .RGP.
        rps = [crs]
        rp_source = ".RGS self-CRS (in .RGP)"
    else:
        # Neither: return the CRS but flag it — the .RGR lookup will
        # almost certainly miss, but we do not silently invent a group.
        rps = [crs]
        rp_source = ".RGS fallback (neither CRS nor group in .RGP — expect no route)"

    prov.append(ProvenanceLine(
        step=f"{role}_routeing_points",
        source=f"{rgs_path.name}:{crs}",
        detail={
            "crs": crs,
            "routeing_points": ",".join(rps),
            "group_id": rec.station_group_id or "",
            "resolution": rp_source,
        },
    ))
    return rps, None


def _lookup_permitted_routes(
    origin_rps: list[str],
    dest_rps: list[str],
    permitted: dict[tuple[str, str], tuple[PermittedRoute, ...]],
    rgr_path: Path,
    prov: list[ProvenanceLine],
) -> tuple[PermittedRoute, ...]:
    """Union of permitted routes across every (origin_rp, dest_rp) pair."""
    all_routes: list[PermittedRoute] = []
    pairs_checked: list[str] = []
    for o in origin_rps:
        for d in dest_rps:
            pairs_checked.append(f"{o}-{d}")
            hits = permitted.get((o, d), ())
            all_routes.extend(hits)
    prov.append(ProvenanceLine(
        step="permitted_routes_lookup",
        source=f"{rgr_path.name}",
        detail={
            "pairs_checked": ",".join(pairs_checked),
            "routes_found": str(len(all_routes)),
        },
    ))
    return tuple(all_routes)


def _verdict(
    routes: tuple[PermittedRoute, ...],
    firing_pos: list[EasementMatch],
    firing_neg: list[EasementMatch],
    prov: list[ProvenanceLine],
) -> ValidityStatus:
    """Combine base permission + firing easements into a final status.

    Precedence (documented so the UI trace can restate it):
      1. Positive AND negative easements both fire => contradiction.
      2. Any negative easement fires => denied_by_easement (regardless of
         base permission).
      3. Base permitted route exists => permitted.
      4. Positive easement fires only => permitted_by_easement.
      5. Otherwise => not_permitted.
    """
    if firing_pos and firing_neg:
        prov.append(ProvenanceLine(
            step="verdict_contradiction",
            source="engine",
            detail={
                "positive_refs": ",".join(m.easement_ref for m in firing_pos),
                "negative_refs": ",".join(m.easement_ref for m in firing_neg),
            },
        ))
        return "contradiction"
    if firing_neg:
        prov.append(ProvenanceLine(
            step="verdict_denied_by_easement",
            source="engine",
            detail={"negative_refs": ",".join(m.easement_ref for m in firing_neg)},
        ))
        return "denied_by_easement"
    if routes:
        prov.append(ProvenanceLine(
            step="verdict_permitted",
            source="engine",
            detail={"base_route_count": str(len(routes))},
        ))
        return "permitted"
    if firing_pos:
        prov.append(ProvenanceLine(
            step="verdict_permitted_by_easement",
            source="engine",
            detail={"positive_refs": ",".join(m.easement_ref for m in firing_pos)},
        ))
        return "permitted_by_easement"
    prov.append(ProvenanceLine(
        step="verdict_not_permitted",
        source="engine",
        detail={},
    ))
    return "not_permitted"


__all__ = [
    "JourneyQuery",
    "ProvenanceLine",
    "ValidityStatus",
    "ValidityVerdict",
    "check_validity",
]
