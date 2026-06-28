# Fares-Change Cockpit

A deterministic "fares-change cockpit" for the UK rail fares system. An analyst proposes a
fares change in plain English; the tool parses the cryptic RDG fares feed into an explicit
graph, computes the change's full impact (affected fares, regulated-cap compliance,
anomalies, revenue exposure), shows the **provenance** of every fare, and gates every change
behind human approval. Built for the **Conduct** track of the UK AI Agent Hackathon EP5.

The thesis (Conduct's, transposed to rail): *an agent can only act on a system it understands.*
We make an opaque legacy system legible, then make change to it safe.

## Stack
- **Python 3.11+** backend. Resolver + impact engine are pure, deterministic, side-effect-free.
- **FastAPI** for the local API the frontend calls.
- **React + Blueprint** (`@blueprintjs/core`) frontend — "Palantir for fares" aesthetic, dark theme.
- **Fetch.ai uAgents** wrapper (added last, thin layer) — **Mailbox/Local agent, never Hosted**.
- Data: the RDG DTD fares feed (fixed-width flat files in a zip). See `docs/REGULATION.md`,
  `RSPS5045` spec at `docs/RSPS5045.pdf`, and the resolution-logic notes below.

## Project map
- `src/ingest/` — parse the fixed-width feed files into the graph. Arm's-length, runs once.
- `src/resolver/` — the deterministic fare resolver WITH provenance capture. The moat.
- `src/impact/` — given a ChangeRequest, compute affected set + compliance + anomalies + revenue.
- `src/regulation/` — the external regulation map and the join that classifies fares as regulated.
- `src/llm/` — the two LLM touchpoints ONLY (English->ChangeRequest in, result->English out).
- `src/api/` — FastAPI surface the frontend calls.
- `src/agent/` — Fetch.ai uAgent wrapper (built last).
- `frontend/` — React + Blueprint cockpit.
- `data/` — feed snapshot + regulation map (gitignored; never commit the feed).
- `docs/` — REGULATION.md, HACKATHON.md, RSPS5045.pdf, resolution notes.

## How to work here
- IMPORTANT: The resolver and impact engine MUST be pure deterministic Python. The LLM NEVER
  computes, prices, or resolves a fare. If you are tempted to ask the model for a number, stop —
  that number comes from the resolver.
- IMPORTANT: Nothing mutates the baseline fare graph. Proposed changes are diffs into a separate
  staging layer; the baseline is immutable within a session. Approval appends to staging only.
  There must be NO code path from the LLM (or an unapproved proposal) to a baseline mutation.
- IMPORTANT: On bad/ambiguous data the resolver NEVER silently guesses. It quarantines bad
  records and continues, interprets sentinels correctly, escalates contradictions to the human,
  and flags genuinely-unknowable cases rather than fabricating. This is the graded criterion.
- IMPORTANT: Every resolved fare carries its full provenance chain (which flow record / override /
  railcard rule / status discount / rounding produced it). Provenance is not optional or
  bolted-on; the resolver's return type includes it from the first line of code.
- License is MIT. Do NOT import GPL/AGPL code (dtd2mysql, fares-service, librailfare, etc.).
  Reference the public RSPS5045 spec and write our own resolver. If dtd2mysql is used at all,
  run it as a separate external process feeding a DB — never import its source.
- Write code that is reviewable by a non-expert: clear names, short functions, comments only
  where the rail domain is non-obvious (cite the RSPS5045 section, e.g. "§4.17 status discount").
- Validate resolver output against BRFares (brfares.com) for the demo corridor before trusting it.
- Prefer building a correct slice (one corridor) over broad coverage. Depth wins the 35% criterion.
- Pip: use `pip install --break-system-packages`. Never use browser localStorage in the frontend.

## The feed (current PMS-era reality — read docs/RSPS5045.pdf for field offsets)
- Files are fixed-width. Every line is a comment (leading `/`) or a fixed-position record.
  Files start with `/!! Start of file` header block, end with `/!! End of file`.
- `.FFL` Flow file: two record types. RECORD_TYPE `F` = flow record (origin/dest NLC, route,
  status, FLOW_ID at pos 43-49, USAGE_CODE 'A'=actual/'G'=generated, DIRECTION 'R'=reversible);
  RECORD_TYPE `T` = fare record (FLOW_ID, TICKET_CODE, FARE in pence, RESTRICTION_CODE). Linked by FLOW_ID.
- `.FSC` Station Clusters: CLUSTER_ID + member NLC. A fare set on a cluster NLC applies to ALL
  members — this is the blast-radius fan-out. One cluster fare governs many station pairs.
- `.NFO` Non-derivable fare OVERRIDES: the primary source of non-derivable fares now. NDO records
  take precedence over flow fares. COMPOSITE_INDICATOR 'Y'=use this record, 'N'=ignore (already
  in flow file). ADULT_FARE/CHILD_FARE = 99999999 means NO fare available (a suppression, NOT £999,999).
- `.NDF` Non-derivable fares: obsolete, single legacy record. Effectively ignore.
- `.TTY` Ticket types: TKT_CLASS (1/2/9), TKT_TYPE (S/R/N = single/return/season),
  TKT_GROUP (F/S/P/E = first/standard/promo/euro), DISCOUNT_CATEGORY (links to status discount).
- `.RLC` Railcards: ADULT_STATUS/CHILD_STATUS codes, min/max passengers/holders.
- `.DIS` Status discounts: STATUS record + DISCOUNT record. DISCOUNT_INDICATOR (0=pct, F=flat,
  M/H/L=pct with floor/cap, X/N=no discount), DISCOUNT_PERCENTAGE where 300 = 30.0%.
- `.RCM` Railcard minimum fares. `.FRR` Rounding rules (round UP to round-amount per band).
- `.RST` Restrictions (many record types, 2-char codes). `.LOC` Locations (NLC, CRS, FARE_GROUP,
  COUNTY — county decides England/Scotland for regulation). `.RTE` Routes. `.TOC` TOC codes.
- High date `31122999` = no end date. All-zero or absent dates = null. `****`/`*****`/`***` = wildcard "any".

## Resolution order (the algorithm — implement in src/resolver/)
1. Resolve origin/dest to their NLCs AND the cluster/group NLCs they belong to (via .FSC, .LOC).
2. Find flow fares: flow records matching those NLCs; if DIRECTION='R', swap O/D for reverse.
3. Apply .NFO overrides: an override matching O/D/route/ticket/railcard replaces/adds; a
   99999999 fare removes the fare; COMPOSITE_INDICATOR='N' records are ignored.
4. Apply railcard discount (only if requested): validate railcard min/max passengers; look up
   ticket's DISCOUNT_CATEGORY in the railcard's status's discount record; apply per DISCOUNT_INDICATOR.
5. Apply minimum-fare floor (.RCM and status mins). 6. Round per .FRR (round up to band amount).
6. Record the full chain at every step into the fare's provenance. NEVER drop a step.
- Where a rule is genuinely undocumented (the feed is silent), apply the public convention
  (round down to 5p) VISIBLY and flag uncertainty. Do not fabricate.

## Regulation & compliance (the differentiator — read docs/REGULATION.md FULLY before building this)
- IMPORTANT: The feed contains NO regulated/unregulated flag. Regulation lives in an EXTERNAL
  map (src/regulation/) we build from the DfT FoI list / TSA Schedule 17 + ticket-type inference.
- Current rule to enforce: a **0% freeze** (1 Mar 2026 -> Mar 2027) on regulated Standard-class
  England fares. A regulated fare may not exceed its 1 March 2025 price. This is the cap to check.
- Validate the map on ~5 known cases before trusting the compliance feature (see docs/REGULATION.md).

## Demo (read docs/HACKATHON.md for what must be shown and how it's judged)
- Corridor: Manchester Piccadilly <-> London Euston (Avanti). Change: add a Student railcard,
  1/3 off, peak-valid. Contradiction to stage: a .NFO override vs flow-fare restriction-code conflict.
- Three showpieces: provenance/rule-trace, GB-map blast-radius (green/amber/red), approval queue
  with contradiction-escalation card. README must carry the `innovationlab` and `hackathon` tags.