"""Pure classifier: is one (ticket, corridor-context) tuple regulated?

The classifier is a pure function — no I/O, no caches, no globals beyond the
named constants below. The caller (`src.regulation.map.build_regulation_map`)
loads the .TTY/.LOC indexes and calls in.

Rules implemented (REGULATION.md §1 / §3 / §4, in priority order):

  R0 ticket has no .TTY record                     → not regulated, §1
  R1 .TTY TKT_CLASS=='1' or TKT_GROUP=='F'         → not regulated, §1 (First Class)
  R2 .TTY DESCRIPTION contains 'ADVANCE'           → not regulated, §1 (Advance fare)
     [§1 says Advance is unregulated. We use the DESCRIPTION text because
     Advance fares in the current feed sit in TKT_GROUP='S', not 'P'.]
  R3 origin .LOC COUNTY starts with 'S'            → not regulated, §3 (Scotland devolved)
  R4 .TTY TKT_CLASS != '2' or TKT_GROUP != 'S'     → not regulated, §1 (not Standard)
  R5 ticket in REGULATED_WALKUPS_LONG              → regulated,    §1 (Off-Peak Return walk-up)
  R6 is_london_flow AND ticket in REGULATED_WALKUPS_LONDON → regulated, §1 (London commuter)
  R7 ticket in REGULATED_SEASONS                   → regulated,    §1 (Weekly+ season)
  R8 default                                       → not regulated, §1 (not on the regulated list)

The ticket-code sets are MANIFESTLY incomplete — they cover the corridor we
demo. Extending them is a separate, sourced-from-DfT-list task (REGULATION.md
§4 build priority order). The map's `notes` list MUST flag this honestly."""

from __future__ import annotations

from src.ingest.inspect import TtyRecord

from src.regulation.types import RegulationCitation


# §1 (verbatim): "On most longer-distance flows: the Off-Peak Return (the
# successor to the 2003 'Saver Return'), or the Super Off-Peak Return on some
# London flows (GWR/EMR/LNER)."
REGULATED_WALKUPS_LONG: frozenset[str] = frozenset({"SVR", "OPR"})

# §1 (verbatim): "In the London Travel-to-Work area: typically the Anytime
# Day Return." SDR ('ANYTIME DAY R' per .TTY DESCRIPTION) is the only
# canonical code we trust here; SOR (ANYTIME R) is NOT the same thing.
REGULATED_WALKUPS_LONDON: frozenset[str] = frozenset({"SDR"})

# §1 (verbatim): "Weekly (and by derivation, longer) season tickets, Standard
# class." Codes from the .TTY DESCRIPTION inspection:
#   7DS = SEVEN DAY   STD, 1MS = MONTHLY (TBC), 3MS = 3-MONTHLY (TBC),
#   AMS = ANNUAL  (TBC). Confirm against feed before adding new ones.
REGULATED_SEASONS: frozenset[str] = frozenset({"7DS", "1MS", "3MS", "AMS"})


def _devolved_nation(county: str) -> str | None:
    """Map a .LOC COUNTY code to a devolved nation, or None for England /
    unknown. Bands are feed-validated (see the Rule R3 comment below)."""
    code = county.strip()
    if code in {"NI", "IR", "CI"}:
        return "outside Great Britain"
    if code.isdigit():
        n = int(code)
        if 31 <= n <= 37:
            return "Wales"
        if 38 <= n <= 43:
            return "Scotland"
    return None


def classify_ticket(
    ticket_code: str,
    ticket_meta: TtyRecord | None,
    *,
    origin_county: str,
    is_london_flow: bool,
) -> tuple[bool, RegulationCitation]:
    """Apply §1/§3/§4 rules to one ticket. Returns (regulated?, citation).

    Pure: same inputs → same outputs. Branches are ordered so the *first*
    matching rule wins; the citation records which rule fired so a reviewer
    can hand-verify.
    """
    # Rule R0: no TTY row at all — out of scope (we never invent regulation).
    if ticket_meta is None:
        return False, RegulationCitation(
            section="§1",
            rule_text="ticket code not in .TTY — out of scope",
            evidence={"ticket_code": ticket_code},
        )

    base_evidence: dict[str, str] = {
        "ticket_code":       ticket_code,
        "tkt_class":         ticket_meta.tkt_class,
        "tkt_type":          ticket_meta.tkt_type,
        "tkt_group":         ticket_meta.tkt_group,
        "discount_category": ticket_meta.discount_category,
        "description":       ticket_meta.description,
        "origin_county":     origin_county,
    }

    # Rule R1: First class (TKT_CLASS=1 or TKT_GROUP=F) — explicitly unregulated.
    if ticket_meta.tkt_class == "1" or ticket_meta.tkt_group == "F":
        return False, RegulationCitation(
            section="§1",
            rule_text="First Class — explicitly unregulated",
            evidence=base_evidence,
        )

    # Rule R2: Advance — DESCRIPTION-based because TKT_GROUP='P' isn't used
    # for Advance in the current feed (Advance sits in TKT_GROUP='S').
    if "ADVANCE" in ticket_meta.description.upper():
        return False, RegulationCitation(
            section="§1",
            rule_text="Advance fare (.TTY DESCRIPTION mentions ADVANCE)",
            evidence=base_evidence,
        )

    # Rule R3: Devolved nation — Scotland/Wales out of scope of the freeze.
    # RSPS5045 (.LOC COUNTY, p.57): "Used to decide if a location is in
    # Scotland, England & Wales or elsewhere. County codes on the mainland
    # are all numeric values. Other values are 'NI', 'IR', 'CI'." The spec
    # does NOT publish the numeric table, so the bands below are validated
    # against the feed itself (station-name sample per code): 01-30 England,
    # 31-37 Wales (Clwyd..Powys), 38-43 Scotland (Fife/Central..Tayside).
    # Unknown/blank counties fall through as England — over-flagging is the
    # conservative failure mode for a compliance gate.
    devolved = _devolved_nation(origin_county)
    if devolved is not None:
        return False, RegulationCitation(
            section="§3",
            rule_text=f"devolved nation ({devolved}) — 0% freeze does not apply",
            evidence=base_evidence,
        )

    # Rule R4: Not Standard class.
    if ticket_meta.tkt_class != "2" or ticket_meta.tkt_group != "S":
        return False, RegulationCitation(
            section="§1",
            rule_text="not Standard class / Standard group — not on regulated list",
            evidence=base_evidence,
        )

    # Rule R5: Regulated long-distance walk-up.
    if ticket_code in REGULATED_WALKUPS_LONG:
        return True, RegulationCitation(
            section="§1",
            rule_text="Off-Peak Return on long-distance flow + Standard class",
            evidence=base_evidence,
        )

    # Rule R6: Regulated London-area walk-up.
    if is_london_flow and ticket_code in REGULATED_WALKUPS_LONDON:
        return True, RegulationCitation(
            section="§1",
            rule_text="Anytime Day Return on London-area flow + Standard class",
            evidence=base_evidence,
        )

    # Rule R7: Weekly+ Standard season ticket.
    if ticket_code in REGULATED_SEASONS:
        return True, RegulationCitation(
            section="§1",
            rule_text="Weekly+ Standard season ticket",
            evidence=base_evidence,
        )

    # Rule R8: default — Standard walk-up that isn't on the regulated list
    # (e.g. Anytime Single / Anytime Return on a long-distance flow).
    return False, RegulationCitation(
        section="§1",
        rule_text="Standard walk-up not on regulated list (Anytime / other)",
        evidence=base_evidence,
    )
