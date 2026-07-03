"""LLM touchpoints.

Per CLAUDE.md there are exactly TWO LLM touchpoints in this project:
English -> ChangeRequest coming IN, and structured result -> English going
OUT.  The LLM never computes, prices, or resolves a fare.

The routeing module adds a THIRD, strictly build-time touchpoint:
easement-text (RSPS5047 § 6.16) is unstructured English up to 2000 chars
per record, and we translate it into structured predicates offline, at
build time, into `data/easement_predicates.json`.  This cache is checked
into the artefact used at runtime; the runtime engine never calls the
LLM.  If the cache is missing the engine degrades to "text-only" mode
(structured checks from .RGF only, English shown as-is)."""
