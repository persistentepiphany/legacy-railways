"""Copilot brain: English → typed intent → deterministic dispatch.

Pipeline (see answer() in dispatch.py):
  1. grammar.py  — deterministic patterns for the canonical queries (no LLM).
  2. llm.py      — fallback ONLY on grammar miss; the LLM's sole job is
                   emitting intent JSON (never numbers, never prose answers).
  3. dispatch.py — read-only calls into the SAME engine functions the API
                   endpoints use; answers are fixed templates filled with
                   resolver/impact numbers verbatim.

CLAUDE.md discipline: the LLM never computes, prices, or resolves a fare.
Every number in an answer comes from the deterministic engine.
"""

from src.copilot.dispatch import answer
from src.copilot.intents import Intent, INTENTS

__all__ = ["answer", "Intent", "INTENTS"]
