# Regulation & Compliance Reference

This document is the authoritative reference for the compliance feature — the single strongest
differentiator of the tool. Read it fully before writing anything in `src/regulation/`. The
central, load-bearing fact is established first, because it shapes the whole feature.

---

## 0. The load-bearing fact: the feed has no regulation flag

The RDG DTD fares feed (all 70 pages of RSPS5045, all 25 file types) contains **no field, on any
record, that identifies a fare as regulated or unregulated.** This is confirmed by reading the
full spec. A tool therefore **cannot** determine regulated-cap compliance from the feed alone.

Regulation is defined **externally**, in two places:
- The **Ticketing & Settlement Agreement (TSA)**, whose **Schedule 17** lists "Regulated Stations,"
  and which defines a Regulated Fare as one whose price is capped under a Franchise Agreement.
- A **DfT-held list of regulated flows + the specific regulated ticket type per flow**, historically
  disclosed via Freedom of Information (a substantial disclosure ~2012, mirrored on
  WhatDoTheyKnow and the RailUK Forums).

**Consequence for the build:** the compliance feature requires us to *source/assemble a regulation
map* and *join it to the feed* on (flow + ticket type). That join is the moat — nobody else
bothers to assemble it. We do not need all ~55M fares classified; we need the handful on the
demo corridor classified correctly. If the map cannot be assembled even for the corridor, the
pitch falls back to provenance + blast-radius without compliance (still strong, just less sharp).

---

## 1. What is regulated (the rules)

Regulation attaches to **specific fare types on specific flows**, not to whole ticket categories.
Roughly 45% of all fares are regulated. The regulated set is, broadly:

- **Weekly (and by derivation, longer) season tickets**, Standard class.
- **The "commuter" walk-up fare on each flow.** Which fare this is depends on the flow:
  - In the **London Travel-to-Work area**: typically the **Anytime Day Return**.
  - On **most longer-distance flows**: the **Off-Peak Return** (the successor to the 2003 "Saver
    Return"), or the **Super Off-Peak Return** on some London flows (GWR/EMR/LNER).
  - In a few cases (long-distance flows priced by regional TOCs): an **Anytime Return**.
- In London zones, **Oyster/contactless PAYG peak fares** are regulated instead of paper single/return.
- **Child variants** of regulated fares are regulated.

**Unregulated:** Advance tickets; most long-distance Off-Peak singles outside the regulated set;
**First Class**; **Standard Premium**; promotional/special-offer fares; and **all open-access
operator fares** (Lumo, Hull Trains, Grand Central, Heathrow Express).

Important nuance: fares are regulated in **"baskets"** (averaged across a TOC's regulated fares),
not strictly flow-by-flow, with historic "flex" letting individual fares deviate from the average.
For the demo we treat the binding constraint at the individual-fare level (the freeze makes this
exact — see §3).

---

## 2. The cap formula (history) — context only, not the current rule

- **1995–2003:** RPI-based regulation introduced at privatisation.
- **2004 on:** average cap set at **RPI+1%** (using the **previous July's RPI**), with a fares-basket
  "flex" of up to 5% (later 2%) on individual fares provided the averaged increase held at RPI+1%.
- **2014–2021:** cap tracked **RPI** directly (the +1% repeatedly waived).
- **2022 on:** government set the cap **discretionarily, below inflation** (2024: capped 4.9% vs
  9% RPI; 2025: ~4.6% = July-2024 RPI 3.6% + 1%). The annual change moved from January to **March**.

You do not implement RPI maths for the demo. The current rule is simpler (next section).

---

## 3. THE CURRENT RULE TO ENFORCE: the 0% freeze (design around this)

- Announced **22 November 2025**. In effect from the **1 March 2026** fares change, running until
  **March 2027** — the first freeze in ~30 years.
- Covers **regulated Standard-class fares in England** (season tickets, peak commuter returns,
  off-peak returns between major cities).
- **The compliance rule, stated exactly:** *a regulated fare may not exceed its 1 March 2025 price.*
  The cap is binding — operators cannot exceed 0%.
- Counterfactual for the pitch: without the freeze these fares would have risen **5.8%** (July-2025
  RPI 4.8% + 1%). DfT estimates the freeze saves passengers ~£600m in 2026/27.
- **NOT covered** (these can still rise, so the tool marks them "unregulated — no cap"): First Class,
  Standard Premium, Advance and other unregulated fares; Scotland and Wales (devolved); TfL services;
  Caledonian Sleeper; open-access operators.

Why this is the right rule to demo: it is binding, current, and one sentence a judge grasps in
seconds — far cleaner than RPI-basket arithmetic. The compliance check becomes: *is this fare
regulated? If so, does the proposed new price exceed its 1 March 2025 price? If yes -> BREACH.*

---

## 4. How to build the regulation map (src/regulation/)

The map is a small table: `(origin_NLC, dest_NLC, ticket_code) -> {regulated: bool, cap_price_2025: pence}`.
For the demo you only need the rows on the Manchester–London corridor. Build it in this priority order:

1. **Sourced list (best):** find the DfT FoI disclosure of regulated fares/flows (search
   WhatDoTheyKnow and RailUK Forums for the ~2012 disclosure) and/or TSA Schedule 17 (regulated
   stations). Load the corridor's rows.
2. **Inference (fallback, and fine for the demo):** classify using the rules in §1 plus feed fields:
   - Regulated candidates are **Standard class** (`.TTY` TKT_CLASS='2', TKT_GROUP='S') and
     **published** (`.FFL` flow record PUBLICATION_IND='Y').
   - The regulated walk-up is the **Off-Peak Return** on this long-distance flow (and the **Anytime
     Day Return** for the London-area legs), plus **weekly+ season tickets**.
   - **Advance, First Class, Standard Premium -> not regulated.**
   - England check via `.LOC` COUNTY. All mainland counties are numeric (RSPS5045 p.57); the
     numeric table is not in the spec, so the bands are validated against the feed itself:
     01-30 England, 31-37 Wales, 38-43 Scotland; 'NI'/'IR'/'CI' outside GB. Wales/Scotland/non-GB
     -> devolved, out of scope for the freeze.
3. **Cap price:** for the freeze, `cap_price_2025` = the fare's 1 March 2025 price. If you only have
   the current snapshot, treat the current regulated price as the frozen baseline for the demo and
   say so explicitly in the UI ("baseline = frozen 2025 price").

The join to the feed: for each resolved fare, look up (O, D, ticket_code) in the map; if regulated,
attach `{regulated: true, cap_price: ...}` so the impact engine can flag a breach when a proposed
change pushes the fare above the cap.

---

## 5. THE VALIDATION TEST — run this before trusting the compliance feature

Before building any compliance UI, confirm the map classifies these ~5 known corridor cases
correctly. If it fails, fix the map or pivot the pitch away from compliance.

| Case | Expected |
|---|---|
| Manchester Piccadilly <-> London Euston **Off-Peak Return** | **Regulated** (frozen at 1 Mar 2025 price) |
| Stoke-on-Trent <-> Manchester **Off-Peak Return** | **Regulated** |
| Manchester <-> London **Anytime Day Return** (London-flow commuter fare) | **Regulated** |
| Manchester <-> London **Advance** | **NOT regulated** |
| Manchester <-> London **First Class** (any) | **NOT regulated** |

Pass condition: all five classified correctly. Only then is the compliance feature real.

---

## 6. One honest caveat to carry into the pitch

Regulation is now partly *academic* in practice: almost all TOCs are government-owned or on
management contracts where DfT directs both regulated and unregulated increases. The formal cap
mechanics still **bind** and still need checking (which is what the tool does), but if asked, be
honest that the "who sets fares" picture is consolidating into Great British Railways. This is a
strength, not a weakness — it is exactly the legacy-system-in-transition that the Conduct brief
and the live Railways Bill are about.