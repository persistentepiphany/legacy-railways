"""FastAPI app for the fares-cockpit frontend.

Endpoints (all under /api):

  GET  /api/resolve                       → ResolvedFareModel
  POST /api/impact                        → ImpactReportModel
  POST /api/staging/propose               → ProposalOutcomeModel
  GET  /api/staging                       → StagingLayerModel
  GET  /api/staging/{card_id}             → ApprovalCardModel
  POST /api/staging/{card_id}/approve     → ProposalOutcomeModel
  POST /api/staging/reset                 → StagingLayerModel  (dev-only)

State on `app.state`:
  feed_paths     — FeedPaths singleton built at startup
  staging        — current StagingLayer (reassigned under lock; persistent-style)
  staging_lock   — asyncio.Lock guarding the reassignment

Discipline: validate at the boundary (engine raises ValueError → 400);
typed engine states like ResolveStatus="contradiction" or Escalation are
NOT HTTP errors — they're successful responses with a typed status field
(mirrors the engine's "never crash, return a verdict" rule)."""

from __future__ import annotations

import asyncio
import json
import logging
import fcntl
import os
import threading
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter

from src.api.geo import StationCoord, default_msn_path, load_station_coords
from src.api.schemas import (
    AcceptedModel,
    ApprovalCardModel,
    ChangeRequestModel,
    CorridorModel,
    CorridorStatsModel,
    EscalationModel,
    ImpactReportModel,
    OverviewCorridorModel,
    OverviewModel,
    PerformanceResultModel,
    ProposalOutcomeModel,
    RailcardMetaModel,
    ResolvedFareModel,
    RouteModel,
    SnapshotModel,
    StagingLayerModel,
    StationModel,
    TicketMetaModel,
    TocModel,
    ValidityVerdictModel,
    card_to_model,
    impact_to_model,
    layer_to_model,
    outcome_to_model,
    perf_to_model,
    resolved_to_model,
    validity_to_model,
)
from src.ingest.inspect import (
    load_ffl_indexes,
    load_fsc_clusters,
    load_loc_meta,
    load_railcards,
    load_ticket_type_meta,
)
from src.impact import (
    DEFAULT_INCLUDE,
    KNOWN_INCLUDE_KEYS,
    ChangeRequest,
    FeedPaths,
    compute_impact,
)
from src.perf import fetch_performance
from src.resolver.resolve import resolve_fare
from src.routeing.engine import JourneyQuery, check_validity
from src.staging import (
    Accepted,
    StagingLayer,
    approve as staging_approve,
    propose as staging_propose,
)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PERF_CACHE_DIR = REPO_ROOT / "data" / "perf_cache"
PERF_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "hsp"


def _load_dotenv() -> None:
    """Minimal `.env` loader. Parses `KEY=VALUE` lines from `REPO_ROOT/.env`
    and populates `os.environ` (without overriding existing vars).

    Intentionally tiny: no python-dotenv dep, no interpolation, no quoting
    rules beyond stripping a single pair of surrounding single/double quotes.
    Lines starting with `#` and blank lines are ignored."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except OSError:
        # Best-effort: a broken .env shouldn't crash the API.
        pass


_load_dotenv()
DATA_DIR = Path(os.environ.get("FARES_DATA_DIR", REPO_ROOT / "data"))

# --- Staging persistence (journal + deterministic replay) ------------------
# The StagingLayer itself stays a pure in-memory value; persistence lives at
# this API boundary as an append-only NDJSON journal of the *inputs*
# (ChangeRequests and approvals). Because the impact engine is deterministic,
# replaying the journal on boot reconstructs an identical layer — we never
# serialize ImpactReport/provenance to disk.
STAGING_JOURNAL = DATA_DIR / "staging_journal.ndjson"
STAGING_JOURNAL_LOCK = DATA_DIR / "staging_journal.lock"
# "off" disables journaling entirely (tests run TestClient against the real
# data dir and must never touch the dev server's journal).
JOURNAL_ENABLED = os.environ.get("FARES_STAGING_JOURNAL", "on") != "off"

_log = logging.getLogger("fares.api")

# Discriminated-union adapter for validating raw dicts from the journal +
# any other non-endpoint entrypoint (endpoints get the routing "for free"
# via FastAPI's request-body parser). `ChangeRequestModel` is an
# `Annotated[Union[...], Field(discriminator="kind")]`, which TypeAdapter
# handles at runtime; the type annotation is elided because Pyright treats
# TypeAdapter's `T` as invariant against the Annotated form.
_change_request_adapter = TypeAdapter(ChangeRequestModel)


def _acquire_journal_lock() -> int | None:
    """Exclusive advisory flock so only ONE app instance owns the journal.

    Multiple dev servers (or a stray warm thread) sharing the journal was
    observed corrupting it: each boot replay may quarantine the file and each
    reset truncates it. Non-owners run with persistence disabled and an
    empty staging layer rather than fighting over the file."""
    if not JOURNAL_ENABLED:
        return None
    fd = None
    try:
        STAGING_JOURNAL_LOCK.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(STAGING_JOURNAL_LOCK, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        if fd is not None:
            os.close(fd)
        _log.warning(
            "another instance holds %s; staging persistence disabled here",
            STAGING_JOURNAL_LOCK,
        )
        return None


def _journal_append(entry: dict) -> None:
    """Append one mutation to the staging journal. Callers hold staging_lock,
    so appends are ordered exactly like the in-memory layer's history."""
    try:
        STAGING_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with STAGING_JOURNAL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError as exc:
        # Never silent (CLAUDE.md) — but a full disk must not 500 a propose.
        _log.warning("staging journal append failed: %s", exc)


def _journal_truncate() -> None:
    try:
        if STAGING_JOURNAL.exists():
            STAGING_JOURNAL.unlink()
    except OSError as exc:
        _log.warning("staging journal truncate failed: %s", exc)


def _replay_journal(feed_paths: FeedPaths) -> StagingLayer:
    """Rebuild the StagingLayer by replaying journaled propose/approve ops.

    Any unreplayable line (corrupt JSON, engine rejection, unknown op)
    quarantines the WHOLE journal to `<name>.bad` and returns an empty layer —
    a partial replay would silently misrepresent what the analyst approved."""
    layer = StagingLayer.empty()
    if not STAGING_JOURNAL.exists():
        return layer
    try:
        for raw in STAGING_JOURNAL.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            entry = json.loads(raw)
            op = entry.get("op")
            if op == "propose":
                # ChangeRequestModel is a discriminated union — routing by
                # `kind` happens inside TypeAdapter, which yields the right
                # variant subclass with its own .to_dataclass().
                change_model = _change_request_adapter.validate_python(
                    entry["change"]
                )
                change = change_model.to_dataclass()
                report = compute_impact(change, feed_paths)
                outcome = staging_propose(layer, change, report)
            elif op == "approve":
                outcome = staging_approve(layer, entry["card_id"])
            else:
                raise ValueError(f"unknown journal op {op!r}")
            if not isinstance(outcome, Accepted):
                raise ValueError(
                    f"journaled {op} escalated on replay: {outcome.reason}"
                )
            layer = outcome.layer
        _log.info(
            "staging journal replayed: %d approved, %d pending",
            len(layer.approved), len(layer.pending),
        )
        return layer
    except Exception as exc:  # noqa: BLE001 — quarantine, never guess
        bad = STAGING_JOURNAL.with_suffix(".ndjson.bad")
        try:
            STAGING_JOURNAL.rename(bad)
        except OSError:
            pass
        _log.warning(
            "staging journal unreplayable (%s); quarantined to %s and "
            "starting from an empty layer", exc, bad,
        )
        return StagingLayer.empty()


def _publish_event(app: FastAPI, source: str, tag: str, text: str,
                   sev: str = "info") -> None:
    """Fan an event out to every connected /api/events subscriber."""
    payload = {
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "source": source,
        "tag": tag,
        "text": text,
        "sev": sev,
    }
    for q in list(app.state.event_subs):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # slow consumer drops events; the ticker is cosmetic


def _snapshot_from_ffl(ffl_path: Path) -> dict:
    """Best-effort snapshot metadata derived from the .FFL filename + header.

    Filename convention `<FEED><NNNN>.FFL` — e.g. `RJFAF805.FFL` yields
    feed='RJFAF', sequence='805'. Header lines start with `/!! Key: value`;
    we lift Generated + Records + set kind out of them. Nothing here fabricates
    on missing fields — the returned dict has empty strings where the header
    was silent."""
    stem = ffl_path.stem
    feed = "".join(c for c in stem if not c.isdigit())
    sequence = "".join(c for c in stem if c.isdigit())
    generated_at = ""
    records = 0
    set_kind = ""
    date = ""
    if ffl_path.exists():
        # Skim the header comment block for metadata; give up after 30 lines
        # (real headers are ~10 lines).
        with ffl_path.open("r", encoding="latin-1") as fh:
            for _ in range(30):
                line = fh.readline()
                if not line or not line.startswith("/!!"):
                    break
                low = line.lower()
                if "generated:" in low:
                    generated_at = line.split(":", 1)[1].strip()
                    # dd/mm/yyyy → yyyy-mm-dd where possible
                    parts = generated_at.split("/")
                    if len(parts) == 3 and all(p.strip().isdigit() for p in parts[:3]):
                        d, m, y = parts[0].strip(), parts[1].strip(), parts[2].strip()[:4]
                        date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
                elif "records:" in low:
                    tail = line.split(":", 1)[1].strip()
                    if tail.isdigit():
                        records = int(tail)
                elif "content type" in low or "record set" in low:
                    set_kind = line.split(":", 1)[1].strip()
        # If the header didn't declare Records, count record lines cheaply
        # (comment/header lines start with '/').
        if records == 0:
            with ffl_path.open("rb") as fh:
                records = sum(1 for line in fh if not line.startswith(b"/"))
    return {
        "id": stem,
        "date": date,
        "feed": feed,
        "sequence": sequence,
        "records": records,
        "generated_at": generated_at,
        "set_kind": set_kind or ("full refresh (F)" if stem.endswith(("_F",)) else ""),
    }


def _load_corridors(data_dir: Path) -> list[dict]:
    path = data_dir / "corridors.json"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh).get("corridors", [])


def _load_railcard_display(data_dir: Path) -> list[dict]:
    path = data_dir / "railcard_display.json"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh).get("whitelist", [])


def _compute_overview_baseline(fp: FeedPaths, corridors: list[dict]) -> dict:
    """One overview row per curated corridor: headline baseline fares, the
    R1/R2/R3 inversion scan on baseline prices, train count and ODM volumes.
    Runs on the warm thread once per boot; everything here is baseline-only
    (immutable within a session) — staging counts are overlaid per request.

    Returns a plain dict (rows keyed by corridor id) so the endpoint can
    model_validate after overlaying live staging counts."""
    from dataclasses import asdict

    from src.impact.baseline_scan import baseline_affected
    from src.impact.inversions import detect_inversions
    from src.ingest.inspect import load_ticket_type_meta

    notes: list[str] = []
    tty = load_ticket_type_meta(fp.tty)

    tt_idx = None
    tt_source = None
    mca = fp.timetable_mca
    if mca is not None and mca.exists():
        from src.ingest.timetable import load_timetable_index
        tt_idx = load_timetable_index(mca)
        tt_source = tt_idx.source_file
    else:
        notes.append("no RSPS5046 timetable — train counts unavailable")

    odm = None
    if fp.odm_csv is not None and fp.odm_csv.exists():
        from src.impact.odm import load_odm_index_cached
        odm = load_odm_index_cached(fp.odm_csv, loc=load_loc_meta(fp.loc))
    else:
        notes.append("no ODM at data/odm/odm.csv — passenger volumes "
                     "unavailable; run tools/fetch_odm.py")

    rows: list[dict] = []
    for c in corridors:
        o_nlc, d_nlc = c["origin_nlc"], c["dest_nlc"]
        row_notes: list[str] = []

        affected = baseline_affected(o_nlc, d_nlc, fp)
        inversions = detect_inversions(affected, fp)

        # Headline pricing: the corridor's default ticket plus the cheapest
        # and dearest baseline products on the direct flow walk.
        best_by_ticket: dict[str, int] = {}
        for f in affected:
            if f.old_price_pence is None:
                continue
            cur = best_by_ticket.get(f.ticket_code)
            if cur is None or f.old_price_pence < cur:
                best_by_ticket[f.ticket_code] = f.old_price_pence
        key_fares: list[dict] = []

        def desc(tc: str) -> str:
            m = tty.get(tc)
            return m.description.strip() if m else tc

        default_tc = c.get("default_ticket")
        if default_tc and default_tc in best_by_ticket:
            key_fares.append({
                "ticket_code": default_tc, "description": desc(default_tc),
                "price_pence": best_by_ticket[default_tc], "label": "default"})
        if best_by_ticket:
            lo = min(best_by_ticket, key=lambda t: best_by_ticket[t])
            hi = max(best_by_ticket, key=lambda t: best_by_ticket[t])
            for tc, label in ((lo, "cheapest"), (hi, "dearest")):
                if not any(k["ticket_code"] == tc for k in key_fares):
                    key_fares.append({
                        "ticket_code": tc, "description": desc(tc),
                        "price_pence": best_by_ticket[tc], "label": label})
        else:
            row_notes.append("no baseline flow fares found for the direct "
                             "pair — corridor may price via clusters only")

        train_count = None
        if tt_idx is not None:
            from src.ingest.timetable import trains_serving_corridor
            seen = {(s.train_uid, s.stp_indicator)
                    for s in (*trains_serving_corridor(tt_idx, c["origin_crs"], c["dest_crs"]),
                              *trains_serving_corridor(tt_idx, c["dest_crs"], c["origin_crs"]))}
            train_count = len(seen)

        j_out = j_back = None
        if odm is not None:
            j_out = odm.by_pair.get((o_nlc, d_nlc))
            j_back = odm.by_pair.get((d_nlc, o_nlc))
            if j_out is None and j_back is None:
                row_notes.append("no ODM row for this pair")

        rows.append({
            "id": c["id"], "name": c["name"], "sub": c.get("sub"),
            "toc": c.get("toc"),
            "origin_crs": c["origin_crs"], "dest_crs": c["dest_crs"],
            "origin_nlc": o_nlc, "dest_nlc": d_nlc,
            "key_fares": key_fares,
            "fares_scanned": len(affected),
            "aberration_count": len(inversions),
            "aberrations": [asdict(i) for i in inversions],
            "train_count": train_count,
            "odm_journeys_out": j_out,
            "odm_journeys_back": j_back,
            "notes": row_notes,
        })

    notes.append("aberrations = structural inversions (return < single, "
                 "discounted < child, 1st <= std) detected on BASELINE "
                 "prices — present in the feed today, not caused by any "
                 "proposed change")
    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "odm_period_label": odm.period_label if odm is not None else None,
        "timetable_source": tt_source,
        "rows": rows,
        "notes": notes,
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    feed_paths = FeedPaths.default_for_data_dir(DATA_DIR)
    app.state.feed_paths = feed_paths
    app.state.staging = StagingLayer.empty()
    app.state.staging_lock = asyncio.Lock()
    # Mutating staging endpoints wait on this until the journal replay in
    # _warm() lands — otherwise a propose racing the replay would be dropped
    # when the replayed layer is swapped in.
    app.state.staging_ready = asyncio.Event()
    # Journal ownership: only the flock holder replays/appends/truncates.
    app.state.journal_fd = _acquire_journal_lock()
    app.state.journal_owner = app.state.journal_fd is not None
    # SSE fan-out: one asyncio.Queue per connected /api/events client.
    app.state.event_subs = set()
    _loop = asyncio.get_running_loop()
    # Metadata caches — computed once at startup so /api/snapshot etc. are
    # dict lookups (no per-request feed I/O).
    app.state.snapshot_meta = _snapshot_from_ffl(feed_paths.ffl)
    app.state.corridors = _load_corridors(DATA_DIR)
    app.state.railcard_display = _load_railcard_display(DATA_DIR)
    # Station coord table — MSN join. Empty dict when MSN absent (map still
    # renders, just without dots).
    msn = default_msn_path(DATA_DIR)
    app.state.stations = load_station_coords(msn) if msn else {}
    # Boot fingerprint for the topbar "fetched Xs ago" indicator.
    app.state.booted_at = datetime.now(timezone.utc).isoformat()
    # Network-overview baseline — filled by the warm thread; None = not ready.
    app.state.overview = None
    # Warm the big feed indexes off-thread so the first /api/resolve or
    # /api/impact doesn't pay the multi-minute .FFL parse interactively.
    app.state.warm = False

    def _warm() -> None:
        try:
            load_ffl_indexes(feed_paths.ffl)
            load_loc_meta(feed_paths.loc)
            load_fsc_clusters(feed_paths.fsc)
        except FileNotFoundError:
            pass  # missing feed surfaces as a clean 400 on first API call
        app.state.warm = True
        # Replay AFTER the indexes are warm so each compute_impact is fast.
        replayed = StagingLayer.empty()
        if app.state.journal_owner:
            try:
                replayed = _replay_journal(feed_paths)
            except Exception as exc:  # noqa: BLE001 — a broken replay must not kill boot
                _log.warning("staging journal replay crashed: %s", exc)
        app.state.staging = replayed
        _loop.call_soon_threadsafe(app.state.staging_ready.set)
        # Timetable index last (it's the biggest parse and nothing above
        # needs it) — so the first /api/route or splits-included impact
        # reads a warm cache instead of blocking on a ~600MB CIF parse.
        mca = feed_paths.timetable_mca
        if mca is not None and mca.exists():
            try:
                from src.ingest.timetable import load_timetable_index
                load_timetable_index(mca)
            except Exception as exc:  # noqa: BLE001 — warm-up only, never fatal
                _log.warning("timetable warm failed: %s", exc)
        # ODM index (~1.4M rows of pandas parse) — warm it so the first
        # demand/revenue_odm-included impact reads the cache.
        if feed_paths.odm_csv is not None and feed_paths.odm_csv.exists():
            try:
                from src.impact.odm import load_odm_index_cached
                load_odm_index_cached(
                    feed_paths.odm_csv, loc=load_loc_meta(feed_paths.loc))
            except Exception as exc:  # noqa: BLE001 — warm-up only, never fatal
                _log.warning("ODM warm failed: %s", exc)
        # Network-overview baseline (pricing + inversion scan + volumes per
        # curated corridor). The baseline is immutable within a session, so
        # computing once here and serving the cache is safe; staging counts
        # are overlaid live per request in /api/overview.
        try:
            app.state.overview = _compute_overview_baseline(
                feed_paths, app.state.corridors)
        except Exception as exc:  # noqa: BLE001 — warm-up only, never fatal
            _log.warning("overview baseline compute failed: %s", exc)

    threading.Thread(target=_warm, name="feed-warm", daemon=True).start()
    # Missing feed files are not fatal: /api/resolve and /api/impact will
    # surface the underlying FileNotFoundError as a clean 400 when called.
    yield
    if app.state.journal_fd is not None:
        try:
            fcntl.flock(app.state.journal_fd, fcntl.LOCK_UN)
            os.close(app.state.journal_fd)
        except OSError:
            pass


app = FastAPI(
    title="Fares-Change Cockpit API",
    version="0.1.0",
    description="HTTP surface over the deterministic fares resolver + impact engine.",
    lifespan=lifespan,
)

# Permissive CORS for local frontend dev only. Tighten before any non-local
# deployment (the engine has no auth surface).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ValueError)
async def _value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(KeyError)
async def _key_error(_: Request, exc: KeyError) -> JSONResponse:
    # KeyError(...) stringifies with quotes; unwrap the single arg.
    msg = exc.args[0] if exc.args else "not found"
    return JSONResponse(status_code=404, content={"detail": str(msg)})


@app.exception_handler(FileNotFoundError)
async def _file_not_found(_: Request, exc: FileNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"detail": f"feed file missing: {exc.filename or str(exc)}"},
    )


# --- 1. Resolve -----------------------------------------------------------


@app.get("/api/resolve", response_model=ResolvedFareModel)
def api_resolve(
    request: Request,
    origin: str = Query(..., min_length=4, max_length=4),
    dest: str = Query(..., min_length=4, max_length=4),
    ticket: str = Query(..., min_length=3, max_length=3),
    route: str | None = Query(None, min_length=5, max_length=5),
    railcard: str | None = Query(None, min_length=3, max_length=3),
) -> ResolvedFareModel:
    fp: FeedPaths = request.app.state.feed_paths
    result = resolve_fare(
        origin, dest, ticket, fp.ffl,
        loc_path=fp.loc, fsc_path=fp.fsc, nfo_path=fp.nfo,
        rlc_path=fp.rlc, dis_path=fp.dis, rcm_path=fp.rcm,
        frr_path=fp.frr, tty_path=fp.tty,
        route_code=route, railcard_code=railcard,
        on_date=dt_date.today(),
    )
    return resolved_to_model(result)


# --- 2. Impact ------------------------------------------------------------


def _parse_include(raw: str | None) -> frozenset[str]:
    """Parse the `?include=` CSV query param into a validated frozenset.

    None → DEFAULT_INCLUDE (compliance + anomalies + revenue; splits is opt-in).
    Empty string → empty set (compute substrate only — affected set + blast
    radius + notes, no analysis blocks). Unknown keys raise at the
    boundary so callers see a 400, not a silently-dropped block."""
    if raw is None:
        return DEFAULT_INCLUDE
    requested = frozenset(
        token.strip() for token in raw.split(",") if token.strip()
    )
    unknown = requested - KNOWN_INCLUDE_KEYS
    if unknown:
        raise ValueError(
            f"unknown include key(s): {sorted(unknown)}; "
            f"valid keys are {sorted(KNOWN_INCLUDE_KEYS)}"
        )
    return requested


@app.post("/api/impact", response_model=ImpactReportModel)
async def api_impact(
    body: ChangeRequestModel,
    request: Request,
    include: str | None = Query(
        None,
        description=(
            "Comma-separated analysis blocks to compute. Valid keys: "
            "compliance, anomalies, revenue, revenue_odm, splits, performance, "
            "demand, carbon (ESTIMATE blocks). Default: "
            "compliance,anomalies,revenue; everything else is opt-in."
        ),
    ),
    eligible_share: float | None = Query(
        None,
        gt=0.0,
        le=1.0,
        description=(
            "Override the demand block's eligible-share assumption (default "
            "0.15): the share of existing passengers who adopt a new "
            "discounted product. Scales demand, carbon and revenue_odm "
            "adoption consistently. The value used is always disclosed in "
            "the blocks themselves."
        ),
    ),
) -> ImpactReportModel:
    change: ChangeRequest = body.to_dataclass()
    fp: FeedPaths = request.app.state.feed_paths
    requested = _parse_include(include)
    # Heavy pure-Python compute (seconds at operator scope) — run off the
    # event loop so health checks and metadata calls stay responsive
    # (same pattern as /api/staging/propose).
    report = await asyncio.to_thread(
        lambda: compute_impact(
            change, fp, include=requested, eligible_share=eligible_share))
    return impact_to_model(report)


# --- 2b. Performance (dedicated endpoint) ---------------------------------


@app.get("/api/performance", response_model=PerformanceResultModel)
def api_performance(
    from_crs: str = Query(..., min_length=3, max_length=3, description="Origin CRS"),
    to_crs: str = Query(..., min_length=3, max_length=3, description="Destination CRS"),
    from_date: str = Query(..., description="YYYY-MM-DD inclusive lower bound"),
    to_date: str = Query(..., description="YYYY-MM-DD inclusive upper bound"),
    days: str = Query(
        "WEEKDAY",
        description="Day type: WEEKDAY, SATURDAY, or SUNDAY",
        pattern="^(WEEKDAY|SATURDAY|SUNDAY)$",
    ),
) -> PerformanceResultModel:
    """HSP serviceMetrics for a corridor + window. Three-mode (live -> cached
    -> fixture); never 500s. A missing fixture (last fall-through) becomes a
    400 via the FileNotFoundError handler."""
    result = fetch_performance(
        from_crs, to_crs, from_date, to_date, days,  # type: ignore[arg-type]
        cache_dir=PERF_CACHE_DIR,
        fixture_dir=PERF_FIXTURE_DIR,
    )
    return perf_to_model(result)


# --- 2c. Validity (routeing / easements) ----------------------------------


@app.get("/api/validity", response_model=ValidityVerdictModel)
def api_validity(
    request: Request,
    origin: str = Query(..., min_length=3, max_length=3, description="Origin CRS (3 chars)"),
    dest: str = Query(..., min_length=3, max_length=3, description="Destination CRS (3 chars)"),
    via: str | None = Query(
        None,
        description="Comma-separated CRS codes the journey passes through (optional)",
    ),
    ticket: str | None = Query(None, min_length=3, max_length=3, description="Ticket code"),
    route: str | None = Query(None, min_length=5, max_length=5, description="Route code"),
    toc: str | None = Query(None, min_length=2, max_length=2, description="TOC code"),
    train_uid: str | None = Query(None, description="Train UID (for UID-scoped easements)"),
    date: str | None = Query(None, description="YYYY-MM-DD; default = today"),
    time: str | None = Query(None, description="HHMM; default = none"),
) -> ValidityVerdictModel:
    """Routeing / validity check against RSPS5047 permitted routes + easements.

    Returns typed `status` — the engine never crashes; a missing routeing
    bundle surfaces as `status="unknown_no_data"` with a note, not a 500.
    Every considered easement carries its `.RGE` English text so the
    frontend can render the human intent alongside the structured trace."""
    fp: FeedPaths = request.app.state.feed_paths
    query_date = None
    if date:
        from datetime import date as _date
        y, m, d = date.split("-")
        query_date = _date(int(y), int(m), int(d))
    via_tuple: tuple[str, ...] = ()
    if via:
        via_tuple = tuple(x.strip().upper() for x in via.split(",") if x.strip())
    q = JourneyQuery(
        origin_crs=origin.upper(),
        dest_crs=dest.upper(),
        via_crs=via_tuple,
        ticket_code=ticket.upper() if ticket else None,
        route_code=route if route else None,
        toc=toc.upper() if toc else None,
        train_uid=train_uid if train_uid else None,
        query_date=query_date,
        query_time_hhmm=time if time else None,
    )
    verdict = check_validity(q, fp)
    return validity_to_model(verdict)


# --- 3. Staging -----------------------------------------------------------


@app.post("/api/staging/propose", response_model=ProposalOutcomeModel)
async def api_propose(
    body: ChangeRequestModel, request: Request,
) -> AcceptedModel | EscalationModel:
    change: ChangeRequest = body.to_dataclass()
    fp: FeedPaths = request.app.state.feed_paths
    # Off-thread: a propose must not stall the event loop (SSE ticker,
    # parallel resolves) while the impact engine runs.
    report = await asyncio.to_thread(compute_impact, change, fp)
    await request.app.state.staging_ready.wait()
    lock: asyncio.Lock = request.app.state.staging_lock
    async with lock:
        layer: StagingLayer = request.app.state.staging
        outcome = staging_propose(layer, change, report)
        if isinstance(outcome, Accepted):
            request.app.state.staging = outcome.layer
            if request.app.state.journal_owner:
                _journal_append({"op": "propose", "change": body.model_dump()})
            _publish_event(
                request.app, "STAG", "PROP",
                f"{outcome.card.card_id} staged: {change.description}",
            )
        else:
            _publish_event(
                request.app, "STAG", "ESC",
                f"proposal '{change.description}' escalated: "
                f"{len(outcome.contradictions)} contradiction(s)",
                sev="warn",
            )
        return outcome_to_model(outcome)


@app.get("/api/staging", response_model=StagingLayerModel)
def api_staging_list(request: Request) -> StagingLayerModel:
    return layer_to_model(request.app.state.staging)


@app.get("/api/staging/{card_id}", response_model=ApprovalCardModel)
def api_staging_get(card_id: str, request: Request) -> ApprovalCardModel:
    layer: StagingLayer = request.app.state.staging
    for c in layer.all_cards():
        if c.card_id == card_id:
            return card_to_model(c)
    raise KeyError(f"no card {card_id!r}")


@app.post("/api/staging/{card_id}/approve", response_model=ProposalOutcomeModel)
async def api_approve(
    card_id: str, request: Request,
) -> AcceptedModel | EscalationModel:
    await request.app.state.staging_ready.wait()
    lock: asyncio.Lock = request.app.state.staging_lock
    async with lock:
        layer: StagingLayer = request.app.state.staging
        outcome = staging_approve(layer, card_id)
        if isinstance(outcome, Accepted):
            request.app.state.staging = outcome.layer
            if request.app.state.journal_owner:
                _journal_append({"op": "approve", "card_id": card_id})
            _publish_event(
                request.app, "STAG", "APPR",
                f"{card_id} approved: {outcome.card.change.description}",
                sev="ok",
            )
        else:
            _publish_event(
                request.app, "STAG", "ESC",
                f"approving {card_id} escalated: "
                f"{len(outcome.contradictions)} contradiction(s)",
                sev="warn",
            )
        return outcome_to_model(outcome)


@app.post("/api/staging/reset", response_model=StagingLayerModel)
async def api_staging_reset(request: Request) -> StagingLayerModel:
    """Dev/demo helper — wipe staging back to empty. Not a production verb."""
    await request.app.state.staging_ready.wait()
    lock: asyncio.Lock = request.app.state.staging_lock
    async with lock:
        request.app.state.staging = StagingLayer.empty()
        if request.app.state.journal_owner:
            _journal_truncate()
        _publish_event(request.app, "STAG", "RST", "staging layer reset", sev="warn")
        return layer_to_model(request.app.state.staging)


# --- 4. Metadata surface for the cockpit UI -------------------------------


@app.get("/api/health")
def api_health(request: Request) -> dict:
    """Readiness probe for the cockpit boot veil. Pure state lookups — must
    stay fast even while the warm thread is parsing the .FFL, so the UI can
    show an honest "warming feed indexes" status instead of hanging."""
    return {
        "status": "ok",
        "warm": request.app.state.warm,
        "staging_ready": request.app.state.staging_ready.is_set(),
        "snapshot_id": request.app.state.snapshot_meta.get("id", ""),
        "booted_at": request.app.state.booted_at,
    }


@app.get("/api/snapshot", response_model=SnapshotModel)
def api_snapshot(request: Request) -> SnapshotModel:
    """Topbar metadata — what feed snapshot the running session is pinned to.
    Read at startup, returned unchanged for the life of the process."""
    return SnapshotModel(**request.app.state.snapshot_meta)


@app.get("/api/corridors", response_model=list[CorridorModel])
def api_corridors(request: Request) -> list[CorridorModel]:
    """Curated corridor list for the left-panel picker. Sourced from
    `data/corridors.json`. Free-form journeys still go through
    `GET /api/resolve` directly."""
    return [CorridorModel(**c) for c in request.app.state.corridors]


@app.get("/api/route", response_model=RouteModel)
def api_route(
    request: Request,
    origin: str = Query(..., min_length=3, max_length=3),
    dest: str = Query(..., min_length=3, max_length=3),
) -> RouteModel:
    """Timetable-derived route for a free-form OD pair the curated corridor
    list doesn't cover. The path is the calling sequence of a REAL service
    from the RSPS5046 snapshot (the through train with the most public calls
    between the endpoints — it traces the physical line best). If no through
    service links the pair in either direction, that's a typed `found=False`
    miss; we never stitch or fabricate a path."""
    o, d = origin.upper(), dest.upper()
    fp: FeedPaths = request.app.state.feed_paths

    def miss(reason: str) -> RouteModel:
        return RouteModel(found=False, reason=reason, origin_crs=o, dest_crs=d)

    if o == d:
        return miss("origin and destination are the same station")
    mca = fp.timetable_mca
    if mca is None or not mca.exists():
        return miss("no RSPS5046 timetable (.MCA) in data/ — cannot derive a route")

    from src.ingest.timetable import load_timetable_index, trains_serving_corridor

    idx = load_timetable_index(mca)
    trains = trains_serving_corridor(idx, o, d)
    reversed_path = False
    a, b = o, d
    if not trains:
        trains = trains_serving_corridor(idx, d, o)
        reversed_path = True
        a, b = d, o
    if not trains:
        return miss(f"no through service links {o} and {d} in timetable "
                    f"snapshot {idx.source_file}")

    best: list[str] = []
    for s in trains:
        seq = [cp.crs for cp in s.calling_points if cp.crs]
        try:
            i = seq.index(a)
            j = seq.index(b, i + 1)
        except ValueError:
            continue
        sl = seq[i:j + 1]
        if len(sl) > len(best):
            best = sl
    if reversed_path:
        best = list(reversed(best))

    # NLCs scope the impact engine; a station without a fares NLC can't
    # anchor a ChangeRequest corridor.
    crs_to_nlc: dict[str, str] = {}
    if fp.loc.exists():
        for nlc, meta in load_loc_meta(fp.loc).items():
            crs = getattr(meta, "crs", None)
            if crs and crs not in crs_to_nlc:
                crs_to_nlc[crs] = nlc
    o_nlc, d_nlc = crs_to_nlc.get(o), crs_to_nlc.get(d)
    if not o_nlc or not d_nlc:
        missing = o if not o_nlc else d
        return miss(f"{missing} has no fares NLC in .LOC — cannot scope an "
                    "impact corridor to it")

    stations: dict[str, StationCoord] = request.app.state.stations
    def disp(crs: str) -> str:
        st = stations.get(crs)
        return st.name.title() if st and st.name else crs
    return RouteModel(
        found=True,
        origin_crs=o, dest_crs=d,
        origin_nlc=o_nlc, dest_nlc=d_nlc,
        name=f"{disp(o)} \u2013 {disp(d)}",
        sub=f"Custom \u00b7 {len(trains)} through trains \u00b7 timetable-derived",
        path_crs=best,
        direct_trains=len(trains),
        reversed_path=reversed_path,
        source=idx.source_file,
    )


@app.get("/api/corridor/stats", response_model=CorridorStatsModel)
def api_corridor_stats(
    request: Request,
    origin: str = Query(..., min_length=3, max_length=3),
    dest: str = Query(..., min_length=3, max_length=3),
) -> CorridorStatsModel:
    """Route fact sheet: trains, traction, ODM volumes, distance, carbon.
    Deterministic — every figure comes from a named source file and carries
    its basis in `notes`; missing sources degrade to None + a note."""
    o, d = origin.upper(), dest.upper()
    fp: FeedPaths = request.app.state.feed_paths
    notes: list[str] = []
    out = CorridorStatsModel(origin_crs=o, dest_crs=d, notes=notes)
    notes = out.notes  # Pydantic copies the list at validation — append to the model's own
    if o == d:
        notes.append("origin and destination are the same station")
        return out

    # -- Timetable: distinct schedules ever calling at both endpoints -------
    mca = fp.timetable_mca
    if mca is not None and mca.exists():
        from src.ingest.timetable import (
            intermediate_calls,
            load_timetable_index,
            traction_mix,
            trains_serving_corridor,
        )
        idx = load_timetable_index(mca)
        fwd = trains_serving_corridor(idx, o, d)
        bwd = trains_serving_corridor(idx, d, o)
        seen: set[tuple[str, str]] = set()
        for s in (*fwd, *bwd):
            seen.add((s.train_uid, s.stp_indicator))
        out.train_count = len(seen)
        out.timetable_source = idx.source_file
        notes.append(
            f"train count = {len(seen)} distinct schedules (train_uid, STP) in "
            f"CIF {idx.source_file} calling at both {o} and {d} in either "
            "direction, ever-calls semantics — not trains/day on a specific date")
        mix = traction_mix(idx, o, d)
        out.electric_pct = mix.electric_pct
        out.diesel_pct = mix.diesel_pct
        notes.extend(mix.notes)
        out.intermediate_call_count = len(intermediate_calls(idx, o, d))
    else:
        notes.append("no RSPS5046 timetable (.MCA) in data/ — train count, "
                     "traction mix and intermediate calls unavailable")

    # -- CRS → NLC (needed by ODM) -------------------------------------------
    crs_to_nlc: dict[str, str] = {}
    if fp.loc.exists():
        for nlc, meta in load_loc_meta(fp.loc).items():
            crs = getattr(meta, "crs", None)
            if crs and crs not in crs_to_nlc:
                crs_to_nlc[crs] = nlc
    o_nlc, d_nlc = crs_to_nlc.get(o), crs_to_nlc.get(d)

    # -- ODM journeys --------------------------------------------------------
    if fp.odm_csv is not None and fp.odm_csv.exists() and o_nlc and d_nlc:
        from src.impact.odm import load_odm_index_cached
        odm = load_odm_index_cached(fp.odm_csv, loc=load_loc_meta(fp.loc))
        out.odm_journeys_out = odm.by_pair.get((o_nlc, d_nlc))
        out.odm_journeys_back = odm.by_pair.get((d_nlc, o_nlc))
        out.odm_period_label = odm.period_label
        out.implied_yield_pence = odm.yield_pence(o_nlc, d_nlc)
        notes.append(
            f"journeys are per publication period of {odm.period_label}, "
            "station-pair totals across all ticket types")
        if out.implied_yield_pence is None:
            notes.append("this ODM release has no revenue column — implied "
                         "yield unavailable")
        if out.odm_journeys_out is None and out.odm_journeys_back is None:
            notes.append(f"no ODM row for {o}↔{d} — pair not in the matrix "
                         "(low-volume pairs are suppressed at source)")
    else:
        notes.append("no ODM at data/odm/odm.csv (or endpoint lacks a fares "
                     "NLC) — passenger volumes unavailable; run "
                     "tools/fetch_odm.py")

    # -- Distance + per-passenger carbon (rail vs car) -----------------------
    from src.impact.carbon import _corridor_rail_factor
    from src.impact.carbon_factors import car_factor_per_passenger_km
    from src.impact.distance import flow_distance_km
    msn = default_msn_path(DATA_DIR)
    dist = flow_distance_km(o, d, rgd_path=fp.rgd, msn_path=msn)
    if dist is not None:
        out.distance_km = dist.km
        out.distance_method = dist.method
        notes.extend(dist.notes)
        rail_factor, desc, _e, _d, cnotes = _corridor_rail_factor(fp, o, d)
        notes.append(desc)
        notes.extend(cnotes)
        out.rail_kgco2e_per_journey = round(rail_factor * dist.km, 2)
        out.car_kgco2e_per_journey = round(
            car_factor_per_passenger_km() * dist.km, 2)
        out.carbon_saving_per_journey_kg = round(
            out.car_kgco2e_per_journey - out.rail_kgco2e_per_journey, 2)
        notes.append(
            "car comparison: DEFRA average-car kgCO2e/vkm over average "
            "occupancy, same route distance — a like-for-like per-passenger "
            "journey, not a fleet claim")
    else:
        notes.append("no distance source (.RGD shortest path or MSN "
                     "great-circle) for this pair — carbon per journey "
                     "unavailable")
    return out


@app.get("/api/overview", response_model=OverviewModel)
def api_overview(request: Request) -> OverviewModel:
    """Network master view — one row per curated corridor: baseline pricing,
    structural aberrations, service level, in-flight staged changes. Baseline
    figures come from the startup cache; staging counts are read live."""
    cache = request.app.state.overview
    if cache is None:
        return OverviewModel(
            ready=False, corridors=[],
            notes=["overview baseline still computing on the warm thread — "
                   "poll again shortly"])

    # Live overlay: staged cards touching each corridor (unordered NLC pair;
    # TOC-scoped changes count against every corridor of that TOC).
    layer: StagingLayer = request.app.state.staging
    pending: dict[frozenset, int] = {}
    approved: dict[frozenset, int] = {}
    pending_toc: dict[str, int] = {}
    approved_toc: dict[str, int] = {}
    for card in layer.all_cards():
        ch = card.change
        tgt = pending if card.status == "pending" else approved
        tgt_toc = pending_toc if card.status == "pending" else approved_toc
        if getattr(ch, "scope", "corridor") == "toc" and ch.toc_code:
            tgt_toc[ch.toc_code] = tgt_toc.get(ch.toc_code, 0) + 1
        else:
            key = frozenset((ch.corridor_origin_nlc, ch.corridor_dest_nlc))
            tgt[key] = tgt.get(key, 0) + 1

    corridors: list[OverviewCorridorModel] = []
    for row in cache["rows"]:
        key = frozenset((row["origin_nlc"], row["dest_nlc"]))
        toc = row.get("toc") or ""
        corridors.append(OverviewCorridorModel(
            **row,
            pending_changes=pending.get(key, 0) + pending_toc.get(toc, 0),
            approved_changes=approved.get(key, 0) + approved_toc.get(toc, 0),
        ))
    return OverviewModel(
        ready=True,
        computed_at=cache["computed_at"],
        odm_period_label=cache["odm_period_label"],
        timetable_source=cache["timetable_source"],
        corridors=corridors,
        notes=cache["notes"],
    )


@app.get("/api/stations", response_model=list[StationModel])
def api_stations(request: Request) -> list[StationModel]:
    """CRS → SVG-projected coordinates for the map panel. Joins .LOC to
    supply the NLC where one exists (some CRS codes — bus interchanges,
    tram stops — appear in MSN without a corresponding fares NLC; those
    ship with `nlc=None` and the map still renders them)."""
    stations: dict[str, StationCoord] = request.app.state.stations
    if not stations:
        return []
    # Build CRS→NLC lookup lazily from .LOC. Cached inside load_loc_meta.
    fp: FeedPaths = request.app.state.feed_paths
    crs_to_nlc: dict[str, str] = {}
    if fp.loc.exists():
        for nlc, meta in load_loc_meta(fp.loc).items():
            crs = getattr(meta, "crs", None)
            if crs and crs not in crs_to_nlc:
                crs_to_nlc[crs] = nlc
    out: list[StationModel] = []
    for crs, coord in stations.items():
        out.append(StationModel(
            crs=coord.crs,
            nlc=crs_to_nlc.get(coord.crs),
            name=coord.name,
            x=coord.x,
            y=coord.y,
            easting=coord.easting,
            northing=coord.northing,
        ))
    return out


@app.get("/api/tickets", response_model=list[TicketMetaModel])
def api_tickets(request: Request) -> list[TicketMetaModel]:
    """The full .TTY ticket-type catalogue for the Author's Adjust-fares +
    Withdraw-product forms. Sorted by (tkt_class, tkt_type, code) so the UI
    can group first-class / standard walk-ups / advances without a client-
    side re-sort. `load_ticket_type_meta` is the same loader the resolver
    reads — a single source of truth per feed snapshot."""
    fp: FeedPaths = request.app.state.feed_paths
    tty = load_ticket_type_meta(fp.tty)
    rows = [
        TicketMetaModel(
            code=code,
            description=rec.description,
            tkt_class=rec.tkt_class,
            tkt_type=rec.tkt_type,
            tkt_group=rec.tkt_group,
            discount_category=rec.discount_category,
        )
        for code, rec in tty.items()
    ]
    rows.sort(key=lambda r: (r.tkt_class, r.tkt_type, r.code))
    return rows


@app.get("/api/railcards", response_model=list[RailcardMetaModel])
def api_railcards(request: Request) -> list[RailcardMetaModel]:
    """Curated passenger-railcard list. Whitelist lives in
    `data/railcard_display.json`; we tag each with `in_feed` so the UI can
    show a small hint when a display-only railcard isn't actually available
    in the loaded snapshot."""
    fp: FeedPaths = request.app.state.feed_paths
    feed_codes: set[str] = set()
    if fp.rlc.exists():
        feed_codes = set(load_railcards(fp.rlc).keys())
    out: list[RailcardMetaModel] = []
    for entry in request.app.state.railcard_display:
        out.append(RailcardMetaModel(
            code=entry["code"],
            display=entry["display"],
            hint_pct=float(entry["hint_pct"]),
            off_peak_only=bool(entry["off_peak_only"]),
            sub=entry["sub"],
            national=bool(entry["national"]),
            in_feed=entry["code"] in feed_codes,
        ))
    return out


_TOC_STATION_CAP = 2_500  # keep the map payload bounded (GWR alone touches ~10k NLCs)


@app.get("/api/tocs", response_model=list[TocModel])
def api_tocs(request: Request) -> list[TocModel]:
    """Fare-TOC list for the operator-scope picker.

    Names come from the optional .TOC file; flow counts and the station-NLC
    union come from the FFL indexes. Before the warm thread finishes we
    return names only (counts/stations None) rather than blocking the
    request thread on the ~30s .FFL parse — the UI polls /api/health and
    refetches once warm. Results are cached on app.state after first warm
    build (the baseline is immutable within a session)."""
    fp: FeedPaths = request.app.state.feed_paths
    cached = getattr(request.app.state, "toc_list", None)
    if cached is not None:
        return cached

    toc_meta = {}
    if fp.toc is not None and fp.toc.exists():
        from src.ingest.inspect import load_toc_meta
        toc_meta = load_toc_meta(fp.toc)

    if not request.app.state.warm:
        # Cold: .TOC names only, no counts — honest partial, never cached.
        return [
            TocModel(code=code, toc_2char=rec.toc_2char, name=rec.name,
                     flow_count=None, actual_flow_count=None)
            for code, rec in sorted(toc_meta.items())
        ]

    by_toc = load_ffl_indexes(fp.ffl).flows_by_toc
    out: list[TocModel] = []
    for code in sorted(by_toc):
        flows = by_toc[code]
        stations: set[str] = set()
        actual = 0
        for f in flows:
            if f.usage_code != "A":
                continue
            actual += 1
            if len(stations) < _TOC_STATION_CAP:
                stations.add(f.origin_nlc)
            if len(stations) < _TOC_STATION_CAP:
                stations.add(f.dest_nlc)
        meta = toc_meta.get(code)
        out.append(TocModel(
            code=code,
            toc_2char=meta.toc_2char if meta else None,
            name=meta.name if meta else None,
            flow_count=len(flows),
            actual_flow_count=actual,
            station_nlcs=sorted(stations),
        ))
    request.app.state.toc_list = out
    return out


# --- 5. Settlement-feed SSE ----------------------------------------------


@app.get("/api/events")
async def api_events(request: Request):
    """Server-sent events for the bottom-of-cockpit ticker.

    Two modes selected by the `X-Fares-Mode` header (falls back to `demo`):
      - `demo`: replays `data/demo_feed.ndjson` at scripted cadence in a
        loop. Zero-network, deterministic, safe for on-stage demos.
      - `live`: emits a heartbeat every 3s tagged with the current snapshot
        id. Full wiring to `staging_propose` / resolver quarantine hooks is
        deferred — the demo mode is what the judging demo runs on.

    Each event is UTF-8 with a `data:` prefix and a blank-line terminator,
    per the SSE spec. The client can filter on `sev` / `source` / `tag`
    fields carried in the JSON payload."""
    # EventSource cannot set headers, so the mode may also arrive as ?mode=.
    mode = (
        request.query_params.get("mode")
        or request.headers.get("x-fares-mode", "demo")
    ).lower()
    # Real events (staging propose/approve/reset) are pushed onto this queue
    # by _publish_event and take priority over the demo tape / heartbeat,
    # which act as filler between real events.
    bus: asyncio.Queue = asyncio.Queue(maxsize=64)
    request.app.state.event_subs.add(bus)

    async def _pause(seconds: float):
        """Sleep up to `seconds`, but yield any real event that arrives."""
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                evt = await asyncio.wait_for(bus.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            yield f"data: {json.dumps(evt)}\n\n"

    async def gen():
        try:
            if mode == "demo":
                tape = DATA_DIR / "demo_feed.ndjson"
                if not tape.exists():
                    yield "data: {\"ts\":\"--:--:--\",\"source\":\"SYS\",\"tag\":\"ERR\",\"text\":\"demo feed tape missing\",\"sev\":\"warn\"}\n\n"
                    return
                while True:
                    # Disconnect check — SSE clients close on tab unload.
                    if await request.is_disconnected():
                        return
                    with tape.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            if await request.is_disconnected():
                                return
                            line = line.strip()
                            if not line:
                                continue
                            yield f"data: {line}\n\n"
                            async for real in _pause(1.4):
                                yield real
            else:
                snap = request.app.state.snapshot_meta.get("id", "?")
                counter = 0
                while True:
                    if await request.is_disconnected():
                        return
                    counter += 1
                    payload = json.dumps({
                        "ts":  datetime.now(timezone.utc).strftime("%H:%M:%S"),
                        "source": "SYS",
                        "tag": "HB",
                        "text": f"live heartbeat #{counter} against {snap}",
                        "sev": "info",
                        "lat": "0ms",
                    })
                    yield f"data: {payload}\n\n"
                    async for real in _pause(3.0):
                        yield real
        finally:
            request.app.state.event_subs.discard(bus)

    return StreamingResponse(gen(), media_type="text/event-stream")


# --- Routers (merge point: visual-copilot-agent session) -------------------

from src.api.corridor import router as corridor_router  # noqa: E402
from src.api.copilot import router as copilot_router  # noqa: E402

app.include_router(corridor_router)
app.include_router(copilot_router)


# --- 6. Static mount for the cockpit UI -----------------------------------

_FRONTEND_LIVE = REPO_ROOT / "frontend" / "live"
if _FRONTEND_LIVE.exists():
    app.mount("/live", StaticFiles(directory=_FRONTEND_LIVE, html=True), name="live")


__all__ = ["app"]
