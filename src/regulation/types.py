"""Public types for the regulation map.

The map answers one question per row: *is this (origin, dest, ticket) tuple a
regulated fare under the 0% freeze (REGULATION.md §3), and if so what is its
1 March 2025 cap price?* The feed has no regulated flag — the answer comes
from external inference rules cited per row (REGULATION.md §1/§4)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegulationCitation:
    """The rule that decided a row's regulated/not classification.

    `section` is the REGULATION.md section cited (e.g. "§1", "§3", "§4").
    `rule_text` is the short rule wording, suitable for the UI card.
    `evidence` carries the parsed fields the rule consumed — so a reviewer
    can hand-verify the classification against the feed without re-running
    the classifier."""
    section: str
    rule_text: str
    evidence: dict[str, str]


@dataclass(frozen=True)
class RegulationEntry:
    """One row of the regulation map: classification + cap-price baseline.

    `cap_price_2025_pence` is None when the row is not regulated, or when the
    row is an honest gap (ticket not on this corridor in the feed). For
    regulated rows present in the feed, the cap is the current resolved adult
    price — the REGULATION.md §4 fallback baseline (the true 1 Mar 2025
    reference price is not yet sourced from DfT/TSA). The map's `notes` list
    flags this assumption."""
    origin_nlc: str
    dest_nlc: str
    ticket_code: str
    regulated: bool
    cap_price_2025_pence: int | None
    citation: RegulationCitation


# Key type for the map: (origin_nlc, dest_nlc, ticket_code).
RegulationKey = tuple[str, str, str]


@dataclass(frozen=True)
class RegulationMap:
    """Built per-session by `build_regulation_map`. Lookup is by (o, d, ticket).

    The `notes` list surfaces honest assumptions and gaps the UI must echo —
    e.g. "cap_price = current snapshot, not 1 Mar 2025" or "ticket X not on
    corridor Y, classified by §1 rule on .TTY metadata alone (honest gap)"."""
    entries: dict[RegulationKey, RegulationEntry]
    notes: tuple[str, ...]

    def get(self, origin_nlc: str, dest_nlc: str, ticket_code: str) -> RegulationEntry | None:
        return self.entries.get((origin_nlc, dest_nlc, ticket_code))
