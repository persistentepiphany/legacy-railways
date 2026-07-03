"""Copilot grammar + intent-validation tests (feed-free, fast).

The grammar is the copilot's primary parser — the LLM only sees text the
grammar misses. These tests pin the ten canonical queries, the honest
ambiguity/clarification behavior (never guess a station), and the
validate_intent safety net that collapses malformed LLM output to `help`.
"""

from __future__ import annotations

from src.copilot.grammar import (
    Vocabulary,
    build_vocabulary,
    find_pair,
    parse,
    resolve_place,
)
from src.copilot.intents import Intent, validate_intent

CORRIDORS = [
    {"id": "man-eus", "name": "Manchester \u2013 London Euston",
     "origin_nlc": "2968", "dest_nlc": "1444",
     "origin_crs": "MAN", "dest_crs": "EUS"},
    {"id": "lds-kgx", "name": "Leeds \u2013 London Kings Cross",
     "origin_nlc": "8487", "dest_nlc": "6121",
     "origin_crs": "LDS", "dest_crs": "KGX"},
]


def vocab() -> Vocabulary:
    return build_vocabulary(
        CORRIDORS,
        station_names={
            "Stockport": ("2969", "Stockport"),
            "Macclesfield": ("2971", "Macclesfield"),
        },
        crs_to_nlc={"SPT": "2969"},
        railcards={"Family & Friends Railcard": "FAM",
                   "Two Together Railcard": "TST"},
    )


# --- Entity resolution -------------------------------------------------------


def test_place_by_nlc_crs_alias_and_name():
    v = vocab()
    assert resolve_place(v, "2968") == ("ok", [("2968", "2968")])
    assert resolve_place(v, "kgx")[1][0][0] == "6121"
    assert resolve_place(v, "manchester")[0] == "ok"
    assert resolve_place(v, "stockport")[1][0][0] == "2969"
    assert resolve_place(v, "atlantis")[0] == "miss"


def test_london_alone_is_ambiguous_but_pair_snaps_to_corridor():
    v = vocab()
    status, hits = resolve_place(v, "london")
    assert status == "ambiguous" and len(hits) >= 2
    got = find_pair(v, "manchester", "london")
    assert got[0] == "2968" and got[2] == "1444" and got[4] == "man-eus"
    got = find_pair(v, "leeds", "london")
    assert got[0] == "8487" and got[2] == "6121" and got[4] == "lds-kgx"


def test_unknown_station_is_clarified_never_guessed():
    got = find_pair(vocab(), "atlantis", "london")
    assert isinstance(got, str) and "atlantis" in got


# --- The canonical queries ---------------------------------------------------


def test_fare_query_variants():
    v = vocab()
    for q in ("fare from manchester to london euston",
              "how much is it from manchester to london?",
              "what's the price from MAN to EUS"):
        it = parse(v, q)
        assert it is not None and it.intent == "resolve_fare", q
        assert it.params["origin_nlc"] == "2968", q
        assert it.params["dest_nlc"] == "1444", q
        assert it.clarify is None, q


def test_fare_query_with_ticket_and_railcard():
    it = parse(vocab(), "fare from manchester to london with a family "
                        "& friends railcard, SVR")
    assert it.intent == "resolve_fare"
    assert it.params["ticket_code"] == "SVR"
    assert it.params["railcard_code"] == "FAM"


def test_unknown_railcard_clarifies_with_known_list():
    it = parse(vocab(), "fare from manchester to london with a klingon railcard")
    assert it.intent == "resolve_fare" and it.clarify
    assert "family & friends railcard" in it.clarify


def test_why_that_price():
    it = parse(vocab(), "why is it that price")
    assert it.intent == "explain_provenance" and it.params == {}
    it = parse(vocab(), "why does the fare from manchester to london cost that")
    assert it.intent == "explain_provenance"
    assert it.params == {"origin_nlc": "2968", "dest_nlc": "1444"}


def test_run_impact_and_change_cost():
    v = vocab()
    assert parse(v, "run the impact").intent == "run_impact"
    assert parse(v, "what does this change cost").intent == "run_impact"
    assert parse(v, "what's the revenue exposure").intent == "run_impact"


def test_splits_report_breach():
    v = vocab()
    assert parse(v, "show the splits").intent == "show_split"
    sp = parse(v, "any splits from manchester to london?")
    assert sp.intent == "show_split" and sp.params["origin_nlc"] == "2968"
    assert parse(v, "open the report").intent == "open_report"
    assert parse(v, "which fares breach the cap").intent == "which_breach"
    assert parse(v, "which fares go over the cap").intent == "which_breach"


def test_zoom_and_show_corridor():
    v = vocab()
    it = parse(v, "zoom to the corridor")
    assert it.intent == "show_corridor" and it.params == {}
    assert parse(v, "show man-eus").params == {"corridor_id": "man-eus"}
    assert parse(v, "zoom to leeds").params == {"corridor_id": "lds-kgx"}


def test_compare_two_pairs():
    it = parse(vocab(), "compare manchester to london with leeds to kgx")
    assert it.intent == "compare_fares"
    assert it.params == {"origin_nlc": "2968", "dest_nlc": "1444",
                         "origin2_nlc": "8487", "dest2_nlc": "6121"}


def test_help_and_gibberish():
    v = vocab()
    assert parse(v, "help").intent == "help"
    assert parse(v, "what can you do").intent == "help"
    # Gibberish is a grammar MISS (None) — the caller decides on LLM fallback.
    assert parse(v, "recite a poem about trains") is None


# --- validate_intent: the LLM safety net -------------------------------------


def test_validate_rejects_malformed_to_help():
    assert validate_intent(None).intent == "help"
    assert validate_intent("resolve_fare").intent == "help"
    assert validate_intent({"intent": "price_a_fare"}).intent == "help"
    bad_nlc = validate_intent({"intent": "resolve_fare",
                               "params": {"origin_nlc": "29"}})
    assert bad_nlc.intent == "help" and bad_nlc.confidence == 0.0


def test_validate_normalizes_params():
    it = validate_intent({"intent": "run_impact",
                          "params": {"discount_pct": "34", "bogus": 1},
                          "confidence": "2.0"})
    assert it.intent == "run_impact"
    assert it.params == {"discount_pct": 0.34}
    assert it.confidence == 1.0
    tk = validate_intent({"intent": "resolve_fare",
                          "params": {"origin_nlc": "2968", "dest_nlc": "1444",
                                     "ticket_code": "svr"}})
    assert tk.params["ticket_code"] == "SVR"


def test_validate_passes_through_intent_objects():
    it = validate_intent(Intent("open_report", confidence=0.9))
    assert it.intent == "open_report" and it.confidence == 0.9
