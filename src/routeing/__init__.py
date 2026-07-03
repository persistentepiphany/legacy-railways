"""Routeing / validity engine.

Given a journey query (origin CRS, dest CRS, optional ticket / route /
TOC / date / time), return a deterministic verdict on whether the
journey is permitted under the National Routeing Guide, with a full
provenance chain citing the .RGS / .RGR / .RGF / .RGE records that
produced the verdict.

Pure, side-effect-free, no LLM at runtime.  The LLM is used offline to
translate .RGE English into structured predicates (see
`src.routeing.translator`); the runtime engine consumes that cache, or
degrades to text-only easement notes if the cache is missing.

Public entry point: `check_validity` from `src.routeing.engine`."""

from src.routeing.engine import (
    JourneyQuery,
    ProvenanceLine,
    ValidityStatus,
    ValidityVerdict,
    check_validity,
)


__all__ = [
    "JourneyQuery",
    "ProvenanceLine",
    "ValidityStatus",
    "ValidityVerdict",
    "check_validity",
]
