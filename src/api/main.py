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
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.schemas import (
    AcceptedModel,
    ApprovalCardModel,
    ChangeRequestModel,
    EscalationModel,
    ImpactReportModel,
    PerformanceResultModel,
    ProposalOutcomeModel,
    ResolvedFareModel,
    StagingLayerModel,
    card_to_model,
    impact_to_model,
    layer_to_model,
    outcome_to_model,
    perf_to_model,
    resolved_to_model,
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    feed_paths = FeedPaths.default_for_data_dir(DATA_DIR)
    app.state.feed_paths = feed_paths
    app.state.staging = StagingLayer.empty()
    app.state.staging_lock = asyncio.Lock()
    # Missing feed files are not fatal: /api/resolve and /api/impact will
    # surface the underlying FileNotFoundError as a clean 400 when called.
    yield


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
def api_impact(
    body: ChangeRequestModel,
    request: Request,
    include: str | None = Query(
        None,
        description=(
            "Comma-separated analysis blocks to compute. Valid keys: "
            "compliance, anomalies, revenue, splits, performance. Default: "
            "compliance,anomalies,revenue (splits and performance are opt-in)."
        ),
    ),
) -> ImpactReportModel:
    change: ChangeRequest = body.to_dataclass()
    fp: FeedPaths = request.app.state.feed_paths
    requested = _parse_include(include)
    report = compute_impact(change, fp, include=requested)
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


# --- 3. Staging -----------------------------------------------------------


@app.post("/api/staging/propose", response_model=ProposalOutcomeModel)
async def api_propose(
    body: ChangeRequestModel, request: Request,
) -> AcceptedModel | EscalationModel:
    change: ChangeRequest = body.to_dataclass()
    fp: FeedPaths = request.app.state.feed_paths
    report = compute_impact(change, fp)
    lock: asyncio.Lock = request.app.state.staging_lock
    async with lock:
        layer: StagingLayer = request.app.state.staging
        outcome = staging_propose(layer, change, report)
        if isinstance(outcome, Accepted):
            request.app.state.staging = outcome.layer
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
    lock: asyncio.Lock = request.app.state.staging_lock
    async with lock:
        layer: StagingLayer = request.app.state.staging
        outcome = staging_approve(layer, card_id)
        if isinstance(outcome, Accepted):
            request.app.state.staging = outcome.layer
        return outcome_to_model(outcome)


@app.post("/api/staging/reset", response_model=StagingLayerModel)
async def api_staging_reset(request: Request) -> StagingLayerModel:
    """Dev/demo helper — wipe staging back to empty. Not a production verb."""
    lock: asyncio.Lock = request.app.state.staging_lock
    async with lock:
        request.app.state.staging = StagingLayer.empty()
        return layer_to_model(request.app.state.staging)


__all__ = ["app"]
