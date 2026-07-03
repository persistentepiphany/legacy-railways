"""Copilot dispatch tests — answers come from the deterministic engine.

Marked slow: dispatch calls resolve_fare / compute_impact against the real
feed (FFL index build on first touch). The point under test is the CLAUDE.md
discipline: every number in an answer_text is the engine's number verbatim,
ui_commands speak the meridian:* contract, and no LLM is ever consulted for
a grammar-parseable query (asserted by unsetting the API keys).

Run with:   pytest tests/test_copilot_dispatch.py -m slow
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.copilot.dispatch import CopilotState, answer
from src.copilot.grammar import build_vocabulary
from src.impact.feed_paths import FeedPaths

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"


@pytest.fixture(scope="module")
def state() -> CopilotState:
    paths = FeedPaths.default_for_data_dir(DATA)
    missing = paths.missing()
    if missing:
        pytest.skip(f"missing feed file(s): {missing}")
    corridors_file = DATA / "corridors.json"
    if not corridors_file.exists():
        pytest.skip("missing data/corridors.json")
    corridors = json.loads(corridors_file.read_text())["corridors"]
    vocab = build_vocabulary(corridors, station_names={}, crs_to_nlc={},
                             railcards={})
    return CopilotState(fp=paths, vocab=vocab,
                        names={"2968": "Manchester Piccadilly",
                               "1444": "London Euston"})


@pytest.fixture(autouse=True)
def no_llm_keys(monkeypatch):
    """Grammar-parseable queries must never touch an LLM; gibberish must
    degrade to help without one."""
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("ASI_ONE_API_KEY", raising=False)


def _events(result: dict) -> list[str]:
    return [c["event"] for c in result["ui_commands"]]


def test_resolve_fare_answer_carries_engine_price(state):
    got = answer(state, "fare from manchester to london euston")
    assert got["intent"] == "resolve_fare"
    assert re.search(r"\u00a3\d+\.\d\d", got["answer_text"])
    assert "Manchester Piccadilly" in got["answer_text"]
    assert _events(got) == ["meridian:highlightStations", "meridian:openTab"]
    assert got["ui_commands"][0]["payload"]["nlcs"] == ["2968", "1444"]


def test_provenance_chain_is_listed_verbatim(state):
    got = answer(state, "why is it that price")
    assert got["intent"] == "explain_provenance"
    assert re.search(r"^1\. ", got["answer_text"], re.M)
    assert "flow" in got["answer_text"]


def test_run_impact_counts_and_est_label(state):
    got = answer(state, "run the impact")
    assert got["intent"] == "run_impact"
    text = got["answer_text"]
    assert re.search(r"\d+ fares repriced", text)
    assert "blast radius" in text and "EST" in text
    assert _events(got) == ["meridian:runImpact", "meridian:openTab"]


def test_which_breach_reuses_cached_report(state):
    answer(state, "run the impact")  # warms the DEFAULT_INCLUDE cache entry
    before = set(state.impact_cache)
    got = answer(state, "which fares breach the cap")
    assert got["intent"] == "which_breach"
    assert "cap" in got["answer_text"]
    assert _events(got) == ["meridian:openTab"]
    # Same change + same include set — answered from cache, no new entry.
    assert set(state.impact_cache) == before


def test_show_corridor_and_report(state):
    got = answer(state, "zoom to the corridor")
    assert got["intent"] == "show_corridor"
    assert _events(got) == ["meridian:zoomToCorridor"]
    assert got["ui_commands"][0]["payload"]["corridorId"]
    rep = answer(state, "open the report")
    assert rep["intent"] == "open_report"
    assert _events(rep) == ["meridian:openReport"]


def test_context_corridor_steers_defaults(state):
    got = answer(state, "zoom to the corridor",
                 context={"corridor_id": "lds-kgx"})
    assert got["ui_commands"][0]["payload"]["corridorId"] == "lds-kgx"


def test_gibberish_without_llm_keys_degrades_to_help(state):
    got = answer(state, "recite a poem about trains")
    assert got["intent"] == "help"
    assert got["confidence"] == 0.0
    assert "fare from Manchester" in got["answer_text"]
    assert got["ui_commands"] == []
