# Hackathon Goal & What Must Be Demonstrated

This document states what we are building toward and what the demo must show. Keep it in front
of the "how" — every technical decision serves one of the things below.

---

## 1. The event and the track

UK AI Agent Hackathon EP5 (Imperial College London; DoraHacks; Fetch.ai a sponsor).
We target the **Conduct track (£8,000)**, with **Fetch.ai** as a free secondary bounty.

**Conduct's brief, verbatim in spirit:** "When the business needs to change (a new pricing model,
a new regulation, a new market) the software has to change too. But that software is millions of
lines deep, with little documentation and few people who still understand how it works. So a
change that should take days takes months. Teams of consultants are hired, budgets run into the
millions, and a few specialists become the bottleneck for the whole company. Closing this gap is
what Conduct does. For this track, we want you to take it on, too."

**What Conduct does (the model we transpose):** it takes an opaque, undocumented system a business
runs on but no longer understands, and makes it **legible and changeable** — understand -> operate/
change -> do it safely with a human in control. Their spine: *"an agent can only act on a system
it understands."*

---

## 2. Our answer, in one paragraph (the pitch)

The UK rail fares system is exactly such a system: ~55M fares governed by undocumented, clustered,
override-laden rules that only a few specialists understand, where a single pricing change takes
weeks of manual tracing and risks breaching regulated caps. Right now the **Railways Bill** is
legally mandating Great British Railways to change that system, and a **0% fares freeze** is in
force. We built a tool that does what Conduct does, for fares: it parses the cryptic feed into an
explicit, self-explaining model (**understand**), computes the full impact and compliance risk of
a proposed change in seconds (**operate/change**), and surfaces every change for human approval
rather than acting autonomously (**control**). We took Conduct's own thesis — "an agent can only
act on a system it understands" — and proved it in a domain that's in Parliament this week.

The three verbs map to three components:
- **Understand** -> the deterministic resolver + **provenance** (show why any fare is what it is).
- **Operate/change** -> the **impact engine** (affected set via cluster fan-out, cap-breach, anomalies, revenue).
- **Control** -> the **approval/staging layer** (propose -> human approves card-by-card -> nothing
  mutates baseline; contradictions escalate, never guessed).

---

## 3. What the demo must SHOW (the three showpieces)

The demo is a sequence of concrete moments, not a feature tour. Build toward these:

1. **Provenance / rule-trace.** Pick a fare on the corridor. Show its full derivation chain as a
   left-to-right graph: flow record -> any override -> railcard rule -> status discount -> rounding
   -> final price. Each node clickable to the source record. The line: "no existing tool can tell
   you *why* a fare is what it is; ours reconstructs it from the cryptic feed."

2. **Blast-radius map.** The analyst proposes the change in plain English ("add a Student railcard,
   1/3 off, peak-valid on Manchester–London"). The tool computes every affected fare by fanning
   through the station clusters, and paints a GB rail map: **green** = clean, **amber** =
   cannibalising/anomaly, **red** = regulated-cap breach. The line: "one change touches hundreds of
   fares through clustering; weeks of specialist tracing, done in seconds."

3. **Approval queue with contradiction-escalation.** The proposed changes appear as a queue of
   cards, each with its diff, impact summary, and compliance flags. The analyst approves card-by-
   card into a staging layer; the baseline never mutates until approved. Then hit the **staged
   contradiction** — a record conflict the rules can't resolve — and show the tool **escalate**
   (present both options + evidence) instead of guessing. The line: "the AI proposes, the human
   disposes; on a genuine contradiction it refuses to guess."

---

## 4. How each piece maps to judging (build to the criteria, not around them)

- **"Real engineering, not a prompt-wrapper" (~35%).** Won by the deterministic resolver +
  provenance + robust handling of messy data. Foreground these. State explicitly, in the demo and
  README: *the LLM never computes a fare; everything consequential is deterministic Python.* This
  perception is won or lost here.
- **Robustness on messy/ambiguous inputs, recovers from errors (graded explicitly).** Won by the
  five failure-handling layers (§5). Show, don't claim: hit the `99999999` sentinel, the malformed
  record, the contradiction — live.
- **The demo (~20%).** Won by the three showpieces above with the "Palantir for fares" Blueprint
  aesthetic (dark, dense, graph + map + queue). Tight, scripted, tested against BRFares so the
  numbers are right.
- **Fit to the Conduct brief.** Won by the §2 pitch: literally "a new regulation forces a change"
  on a system "few people understand," compressing "months to minutes," live in Parliament now.
- **Fetch.ai secondary.** Won by wrapping the resolver as a Mailbox uAgent with the chat protocol
  and a keyword-rich README so it's ASI:One-discoverable. Same engine, second door ("why is my
  ticket this price?"). Thin layer, built last.

---

## 5. Failure modes turned into demo beats (the architecture's best trick)

Our worst failure modes are our strongest moments, because the "prompt-wrapper" competitor hides
or hallucinates through them and the judges (ex-Palantir) see the difference instantly.

- **Messy data -> "watch us read the `99999999` sentinel as a suppression, not a £999,999 fare."**
- **Malformed record -> "watch us quarantine it and continue" (dtd2mysql crashes here; we don't).**
- **Contradiction in the rules -> "watch us escalate to the human instead of guessing."**
- **Genuinely-unknowable rule -> "watch us flag uncertainty instead of fabricating a number."**
- **Confident-but-wrong LLM narration -> can't pass a bad change, because the card's numbers come
  from the deterministic engine, not the model; the human approves the computed consequence.**

The unifying rule, and the thing to say out loud: **the tool never silently guesses on bad data.**

---

## 6. Scope discipline (what NOT to do)

- Do not try to resolve the whole network. One corridor (Manchester–London), resolved correctly
  and validated against BRFares, beats broad-but-wrong. Depth wins the 35%.
- Do not build the Fetch.ai agent first or let it sprawl. It is a thin wrapper, built last, ~half a day.
- Do not add features that don't serve one of the three showpieces. A smaller, tighter demo lands harder.
- Do not let the LLM near a fare calculation, ever. (Repeated because it is the whole game.)

---

## 7. Submission checklist

- [ ] Working demo of the three showpieces, recorded (screen capture) as backup.
- [ ] Resolver output validated against BRFares for the corridor.
- [ ] Regulation map passes the 5-case test (docs/REGULATION.md §5).
- [ ] Fetch.ai Mailbox agent registers and answers via chat protocol; README keyword-rich.
- [ ] README explains the Conduct mapping (understand/change/control) and states "LLM never prices a fare."
- [ ] README carries the `innovationlab` and `hackathon` tags (Fetch.ai requirement).
- [ ] Submitted on DoraHacks/Devpost before the deadline.
- [ ] License: MIT; no GPL/AGPL code imported.