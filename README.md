# Fares Change Cockpit

<p align="center">
  <img src="https://readme-typing-svg.demolab.com?font=JetBrains+Mono&size=19&pause=1300&color=6F9E86&center=true&vCenter=true&width=640&lines=understand+a+system+few+people+still+know;change+it+without+breaching+regulated+caps;approve+every+diff+with+a+human+in+the+loop" alt="fares cockpit header animation">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-6F9E86?style=for-the-badge&labelColor=0C0D0F" alt="python">
  <img src="https://img.shields.io/badge/fastapi-served-6F9E86?style=for-the-badge&labelColor=0C0D0F" alt="fastapi">
  <img src="https://img.shields.io/badge/frontend-react_%2B_blueprint-8A93A3?style=for-the-badge&labelColor=0C0D0F" alt="frontend">
  <img src="https://img.shields.io/badge/license-MIT-8A93A3?style=for-the-badge&labelColor=0C0D0F" alt="license">
</p>

<p align="center">
  <code>innovationlab</code> &nbsp; <code>hackathon</code>
</p>

---

## The problem

The UK rail fares system runs on roughly 55 million fares governed by undocumented rules, station clusters, and override files that only a handful of specialists still understand. A single pricing change takes weeks of manual tracing, and a wrong step can breach a regulated cap or quietly wipe out margin before anyone notices.

Right now the Railways Bill is legally mandating Great British Railways to change that system, and a 0 percent fares freeze is in force through March 2027. The work is unavoidable and the tools do not exist.

## What this is

A deterministic cockpit for that work. An analyst types a change in plain English. The engine parses the cryptic RDG DTD feed into an explicit graph, computes the full impact of the change, and stages every diff behind human approval. The baseline never mutates in-session.

Nothing consequential is ever produced by a language model. Every price on screen comes from the resolver or the impact engine. The LLM lives at two edges only. One turns English into a `ChangeRequest`. The other turns a computed result into a sentence a human can read.

## Three verbs, three components

| Understand | Change | Control |
| --- | --- | --- |
| Deterministic resolver with full provenance. Every fare carries the flow record, the override, the railcard rule, the status discount, and the rounding step that produced it. | Impact engine over the affected set through cluster fan out, with regulated cap breaches flagged, anomalies surfaced, and revenue exposure estimated. | Staging and approval. Proposals become cards. Cards need a human to approve them. A genuine contradiction blocks the queue and asks the analyst to pick a side. |

## The three demo showpieces

<details>
<summary><strong>1. Provenance and rule trace</strong></summary>

<br>Pick any fare on the corridor. The panel draws its full derivation chain from left to right. Flow record, then any NFO override, then railcard rule, then status discount, then rounding, then final price. Every node cites its source line in the feed. No existing tool tells you why a fare is the number it is. This one reconstructs it from the feed.
</details>

<details>
<summary><strong>2. Blast radius map</strong></summary>

<br>Propose a change in plain English, for example "add a Student railcard, one third off, peak valid on Manchester to London". The engine fans through station clusters and paints a GB rail map. Green means clean. Amber means cannibalising or anomalous. Red means a regulated cap breach. One change can touch hundreds of fares through clustering, and the map shows every one of them.
</details>

<details>
<summary><strong>3. Approval queue with contradiction escalation</strong></summary>

<br>Proposed changes arrive as cards with a diff, an impact summary, and compliance flags. The analyst approves them one by one into a staging layer. The baseline stays immutable. When two records disagree and no rule resolves the conflict, the queue refuses to guess. It presents both options with evidence and asks the human to decide.
</details>

## Stack

Backend runs on Python 3.11 with FastAPI. The resolver and impact engine are pure, deterministic, and side effect free. Frontend is React with Palantir Blueprint on a dark theme, echoing Foundry with a lineage graph, blast radius map, and approval queue. A thin Fetch.ai uAgent wrapper exposes the same engine as a Mailbox agent, discoverable through ASI:One.

## Running it

You need the RDG fares feed snapshot in `data/`. That folder is gitignored and the feed is never committed.

```sh
.venv/bin/uvicorn src.api.main:app --port 8000
```

Then open http://127.0.0.1:8000/live/ in a browser. The UI must be served from the API origin.

The first boot per feed snapshot parses the full `.FFL` file, which is roughly 9.6 million records and takes a few minutes. The UI polls `GET /api/health` and loads itself once the backend reports `warm: true`. Run one server instance at a time. Parallel instances contend for CPU during the warm parse.

## Tests

```sh
.venv/bin/python -m pytest -q
```

## Repo tour

```
src/ingest/       parse the fixed width feed files
src/resolver/     deterministic fare resolver with provenance
src/impact/       compute affected set, compliance, anomalies, revenue
src/regulation/   external regulation map and the classifier
src/llm/          the two LLM touchpoints, in and out
src/api/          FastAPI surface the frontend calls
src/agent/        Fetch.ai uAgent wrapper, thin and last
frontend/live/    React and Blueprint cockpit
docs/             regulation notes, hackathon brief, RSPS5045 spec
```

## Ground rules baked into the code

The resolver never silently guesses. Bad records get quarantined and flagged. The sentinel `99999999` reads as a suppression, not a fare of £999,999. Contradictions escalate to a human. Every step of the derivation stays in the provenance chain. The baseline graph is immutable within a session. Approval writes to staging only. No path from the LLM to a baseline mutation exists in the codebase, by construction.

## License

MIT. No GPL or AGPL code enters this repo. The public RSPS5045 spec informs the resolver. Everything else is ours.

Built for the Conduct track of the UK AI Agent Hackathon EP5.
