# Fares-Change Cockpit

`innovationlab` `hackathon`

A deterministic fares-change cockpit for the UK rail fares system: propose a change in
plain English, see its full impact (affected fares, regulated-cap compliance, anomalies,
revenue), inspect the provenance of every price, and gate every change behind human
approval. Built for the Conduct track of the UK AI Agent Hackathon EP5.

## Running

Requires the RDG fares feed snapshot in `data/` (gitignored — never committed).

```sh
.venv/bin/uvicorn src.api.main:app --port 8000
```

Then open **http://127.0.0.1:8000/live/** (the UI must be served from the API origin).

The first boot per feed snapshot parses the full `.FFL` (~9.6M records, several minutes).
The UI shows the warm-up status via `GET /api/health` and loads itself once the backend
reports `warm: true` — no manual reload needed. Run **one** server instance; parallel
instances contend for CPU during the warm parse.

## Tests

```sh
.venv/bin/python -m pytest -q
```
