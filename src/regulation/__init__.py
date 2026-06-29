"""Regulation map — answers 'is this fare regulated?' deterministically.

The feed has no regulated flag (REGULATION.md §0). This package builds the
map from the §1/§4 inference rules so the impact engine can join against it
when wiring the 0% freeze compliance check (deferred to the next slice).

Public API:
    classify_ticket   — pure classifier function (no I/O)
    build_regulation_map — load feed indexes, classify a corridor
    CorridorSpec       — one corridor to classify
    RegulationEntry    — one map row
    RegulationCitation — the rule that decided a row
    RegulationMap      — the built map (lookup by (origin, dest, ticket))
"""

from src.regulation.classify import (
    REGULATED_SEASONS,
    REGULATED_WALKUPS_LONDON,
    REGULATED_WALKUPS_LONG,
    classify_ticket,
)
from src.regulation.map import (
    BASELINE_NOTE,
    NFO_NOT_APPLIED_NOTE,
    CorridorSpec,
    build_regulation_map,
)
from src.regulation.types import (
    RegulationCitation,
    RegulationEntry,
    RegulationKey,
    RegulationMap,
)

__all__ = [
    "BASELINE_NOTE",
    "NFO_NOT_APPLIED_NOTE",
    "CorridorSpec",
    "REGULATED_SEASONS",
    "REGULATED_WALKUPS_LONDON",
    "REGULATED_WALKUPS_LONG",
    "RegulationCitation",
    "RegulationEntry",
    "RegulationKey",
    "RegulationMap",
    "build_regulation_map",
    "classify_ticket",
]
