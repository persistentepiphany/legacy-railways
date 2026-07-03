# Meridian Fares Agent

A Fetch.ai **uAgents Mailbox agent** (never Hosted) that answers UK rail fares
questions over the Agentverse chat protocol. The agent computes nothing: every
message is relayed to the local Meridian backend (`POST /api/copilot/query`),
where a deterministic resolver and impact engine produce every number. The
agent replies with the engine's `answer_text` verbatim.

`innovationlab` `hackathon`

## What it answers

- **rail fares** — "fare from Manchester to London Euston"
- **fare provenance** — "why is it that price"
- **fare change impact** / **blast radius** — "run the impact", "what does this change cost"
- **compliance** with the regulated-fares cap — "which fares breach the cap"
- **split ticketing** — "show the splits"
- corridor comparison — "compare Manchester to London with Leeds to KGX"

Keywords: rail fares, fare change impact, compliance, blast radius,
split ticketing, innovationlab, hackathon.

## Run it

```bash
# 1. Backend must be up (feed warm takes a few minutes):
FARES_STAGING_JOURNAL=off .venv/bin/uvicorn src.api.main:app --port 8000

# 2. Start the agent (own process, port 8020):
MERIDIAN_API_URL=http://127.0.0.1:8000 .venv/bin/python -m src.agent.fares_agent
```

Optional `.env` entries:

- `FETCH_AI_AGENT_SEED=<any long random string>` — stable agent address across
  restarts. Without it the address is ephemeral (fine for a first look).
- `MERIDIAN_API_URL` — where the Meridian API lives (default `http://127.0.0.1:8000`).

## Connect it to Agentverse (manual, one time)

1. Sign in / sign up at <https://agentverse.ai>.
2. Start the agent (above). The log prints an **Agent Inspector** link
   (`https://agentverse.ai/inspect/?uri=…`).
3. Open the Inspector link → click **Connect** → choose **Mailbox** → confirm.
   The agent now receives chat messages through the Agentverse mailbox while
   running locally next to the fares engine.
4. On the agent's Agentverse profile, make sure the README/description carries
   the discovery keywords above (including `innovationlab` and `hackathon`) so
   it is findable, e.g. from ASI:One.
5. Test from Agentverse: open the agent → **Chat** → ask
   "fare from Manchester to London Euston". The reply is the engine's number,
   e.g. `£386.00`, never an LLM guess.

## Local end-to-end probe (no Agentverse needed)

```bash
MERIDIAN_API_URL=http://127.0.0.1:8000 .venv/bin/python tools/probe_fares_agent.py
```

Runs the fares agent and a throwaway probe agent in one Bureau, sends one
canonical query through the real chat protocol, and prints the engine-backed
reply.

## Guarantees

- Mailbox/local only — the RDG feed and the engine never leave the machine.
- The agent holds no fares logic; if the backend is down it says so instead of
  inventing an answer.
- `ui_commands` in the copilot response are ignored — chat has no map to drive.
