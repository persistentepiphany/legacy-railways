# Handoff — visual-copilot-agent session

Branch `worktree-visual-copilot-agent` (worktree `.claude/worktrees/visual-copilot-agent`).
Ran concurrently with the IA-restructure session; **IA merges first**, then this.

## Merge protocol

```
9b6ad39  baseline: shared working-tree snapshot  ← DO NOT MERGE (pre-IA copy of main tree)
fd2b3b1  Phase 1: map narrative — callings endpoint, zoom-to-fit, dimming, command bus
7862b80  Phase 2: copilot brain — src/copilot/, /api/copilot/query
f624b1d  Phase 3: copilot drawer (frontend, flagged)
1828212  Phase 4: Fetch.ai uAgent — src/agent/, tools/probe_fares_agent.py
096c13b  Phase 5: provenance cross-highlight + corridor strip
```

After the IA session's work lands on main, **cherry-pick / replay the five phase
commits in order, skipping the baseline**. The baseline is only there so the
phases had the (then-uncommitted) app to build on; its content will have been
superseded by the IA merge.

## Merge points (files both sessions touch)

- `src/api/main.py` — one block near the end, before the static mount:
  ```python
  # --- Routers (merge point: visual-copilot-agent session) ------------------
  from src.api.corridor import router as corridor_router  # noqa: E402
  from src.api.copilot import router as copilot_router  # noqa: E402
  app.include_router(corridor_router)
  app.include_router(copilot_router)
  ```
- `frontend/live/index.html` — regions this session added/edited (all keyed off
  data presence, never IA flow-state):
  - constructor: `copilotOpen/Busy/Offline/Msgs` state + `this._copilotOn` flag read
  - `componentDidMount`: `meridian:*` command-bus listeners (thin adapters)
  - `componentDidUpdate`: `#cp-msgs` auto-scroll (before the `_impact` early-return)
  - methods: `zoomToNlcs`, `setBusHighlight`, `copilot*`, `buildCorridorStrip`,
    zoom-to-fit in `zoomToAffected`
  - `buildProvenance`: step `onEnter/onLeave` hover → `setBusHighlight`
  - templates: header COPILOT toggle (after the topbar spacer), copilot drawer
    (after the boot overlay), corridor strip (after the map legend),
    `onMouseEnter/onMouseLeave` on provenance step cards
  - `renderVals`: `V.copilot`, `V.strip`
- `frontend/live/rfe.api.js` — additive: `corridorCallings`, `copilotQuery`
- `frontend/live/rfe.adapt.js` — additive: `adaptCallings`; one-line
  `detail: detail` passthrough in `adaptResolve` steps
- `frontend/live/fare-engine.js` — additive: `RFE.corridorCallings(id)` cache,
  `RFE.stationByNlc`
- `.env.example` — `FETCH_AI_AGENT_SEED`, `MERIDIAN_API_URL` entries

## Contracts (stable, other sessions may rely on these)

- Window CustomEvents: `meridian:zoomToCorridor {corridorId}`,
  `zoomToStations {nlcs}`, `highlightStations {nlcs, pulse}`, `openTab {tab}`,
  `runImpact {changeParams?}`, `openReport {}`, `toggleModule {module, on}`,
  `setStep` (accepted, no-op — see hookups).
- `POST /api/copilot/query {text, context?:{corridor_id}}` →
  `{intent, confidence, answer_text, ui_commands:[{event, payload}]}`.
- Copilot UI exists only when `localStorage.meridianCopilot === "1"` (the
  single sanctioned localStorage use — feature flag, never app state).
- The LLM (Z.AI, else ASI:One) is consulted **only** when the grammar misses,
  emits intent JSON only, and is schema-validated; every number in
  `answer_text` is the engine's, verbatim.

## Future hookups for the IA session

- `meridian:setStep` is accepted and ignored — wire it to the stepper when
  flow-state is stable (copilot dispatch never emits it yet).
- Zoom-to-fit fires off data presence (`_impact` + `resultsCurrent`); if the IA
  flow wants zoom on a specific stage transition, move the trigger, keep
  `zoomToAffected`.
- Deferred: report-modal map exhibit (IA owns the modal).

## Environment notes

- `uagents 0.25.2` (+ deps incl. `aiohttp`, `requests`, `certifi`) installed
  into the **shared** `.venv`.
- Agent: `src/agent/README.md` has run + Agentverse mailbox steps. certifi must
  be wired **before** the uagents import (aiohttp freezes its SSL context at
  import; macOS CA gap otherwise breaks agentverse.ai TLS).
- Local agent E2E: `MERIDIAN_API_URL=http://127.0.0.1:8000 .venv/bin/python
  tools/probe_fares_agent.py` (verified: reply carries the engine's £386.00).
- Tests: `pytest tests/test_copilot_grammar.py` (fast) and
  `pytest tests/test_copilot_dispatch.py -m slow` (feed-backed).
