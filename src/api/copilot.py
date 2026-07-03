"""POST /api/copilot/query — the copilot's single HTTP surface.

Thin by design: build a CopilotState once per process (vocabulary from the
same app.state the rest of the API serves), then delegate to
src.copilot.dispatch.answer(). Read-only — the endpoint never stages or
mutates anything; every number in an answer comes from the deterministic
engine. Schemas live here, not in schemas.py (concurrent-session boundary).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from src.copilot.dispatch import CopilotState, answer
from src.copilot.grammar import build_vocabulary

router = APIRouter()


class CopilotContextModel(BaseModel):
    corridor_id: str | None = None


class CopilotQueryModel(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    context: CopilotContextModel | None = None


class UiCommandModel(BaseModel):
    event: str
    payload: dict[str, Any]


class CopilotAnswerModel(BaseModel):
    intent: str
    confidence: float
    answer_text: str
    ui_commands: list[UiCommandModel]


def _build_state(app: Any) -> CopilotState:
    cached = getattr(app.state, "copilot", None)
    if cached is not None:
        return cached

    fp = app.state.feed_paths
    corridors = list(app.state.corridors)
    stations = app.state.stations  # CRS → StationCoord (MSN names)

    names: dict[str, str] = {}                       # NLC → display
    station_names: dict[str, tuple[str, str]] = {}   # display → (NLC, display)
    crs_to_nlc: dict[str, str] = {}
    if fp.loc.exists():
        from src.ingest.inspect import load_loc_meta
        for nlc, meta in load_loc_meta(fp.loc).items():
            crs = (meta.crs or "").strip()
            disp = (meta.station_name or "").strip().title()
            if crs in stations and stations[crs].name:
                disp = stations[crs].name.title()
            if disp:
                names[nlc] = disp
                station_names.setdefault(disp, (nlc, disp))
            if crs:
                crs_to_nlc.setdefault(crs, nlc)

    # Railcard vocabulary: display names for codes present in the loaded .RLC
    # only — the resolver can't price a display-only railcard, so the grammar
    # shouldn't offer it.
    railcards: dict[str, str] = {}
    feed_codes: set[str] = set()
    if fp.rlc.exists():
        from src.ingest.inspect import load_railcards
        feed_codes = set(load_railcards(fp.rlc).keys())
    for entry in getattr(app.state, "railcard_display", None) or []:
        if entry.get("code") in feed_codes:
            railcards[entry["display"]] = entry["code"]

    state = CopilotState(
        fp=fp,
        vocab=build_vocabulary(corridors, station_names, crs_to_nlc, railcards),
        names=names,
    )
    app.state.copilot = state
    return state


@router.post("/api/copilot/query", response_model=CopilotAnswerModel)
async def api_copilot_query(body: CopilotQueryModel,
                            request: Request) -> CopilotAnswerModel:
    state = _build_state(request.app)
    ctx = body.context.model_dump() if body.context else None
    # Engine calls (resolve/impact) are seconds of pure compute — off the
    # event loop, same pattern as /api/impact.
    result = await asyncio.to_thread(answer, state, body.text, ctx)
    return CopilotAnswerModel(**result)
