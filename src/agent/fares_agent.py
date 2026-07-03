"""Meridian fares agent — a thin Fetch.ai uAgents wrapper over the copilot API.

The agent computes NOTHING. Every message is relayed to the local Meridian
backend (`POST /api/copilot/query`), which parses intent and answers with
numbers from the deterministic resolver/impact engine. The agent replies with
`answer_text` verbatim and ignores `ui_commands` (there is no map to drive
over chat). Mailbox agent, never Hosted — the engine and feed stay local.

Run:  MERIDIAN_API_URL=http://127.0.0.1:8000 \
      .venv/bin/python -m src.agent.fares_agent
"""

from __future__ import annotations

import os

# macOS Pythons often lack system CA certs, which breaks the agent's TLS
# calls to agentverse.ai (manifest publish / mailbox). This MUST run before
# the uagents import: aiohttp caches its default SSL context at import time,
# so setting SSL_CERT_FILE any later is ignored.
if "SSL_CERT_FILE" not in os.environ:
    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass

import asyncio  # noqa: E402
import json  # noqa: E402
import urllib.request  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from uuid import uuid4  # noqa: E402

from uagents import Agent, Context, Protocol  # noqa: E402
from uagents_core.contrib.protocols.chat import (  # noqa: E402
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_dotenv() -> None:
    """Same minimal loader as src/api/main.py — never overrides real env."""
    env = REPO_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()

API_URL = os.environ.get("MERIDIAN_API_URL", "http://127.0.0.1:8000").rstrip("/")
SEED = os.environ.get("FETCH_AI_AGENT_SEED")  # unset ⇒ ephemeral address

agent = Agent(
    name="meridian-fares-agent",
    seed=SEED,
    port=8020,
    mailbox=True,
)

chat_proto = Protocol(spec=chat_protocol_spec)


def _query_engine(text: str) -> str:
    """Blocking POST to the copilot endpoint; run via asyncio.to_thread."""
    req = urllib.request.Request(
        API_URL + "/api/copilot/query",
        data=json.dumps({"text": text[:500]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode())
    return body.get("answer_text") or "The engine returned no answer."


def _chat_text(text: str, end: bool = True) -> ChatMessage:
    content: list = [TextContent(type="text", text=text)]
    if end:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(
        timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content
    )


@agent.on_event("startup")
async def announce(ctx: Context) -> None:
    ctx.logger.info(f"meridian-fares-agent address: {agent.address}")
    ctx.logger.info(f"relaying to engine at {API_URL}")
    if SEED is None:
        ctx.logger.warning(
            "FETCH_AI_AGENT_SEED not set — address is ephemeral and will "
            "change on restart. Set it in .env for a stable Agentverse identity."
        )


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id
        ),
    )
    text = " ".join(
        c.text for c in msg.content if isinstance(c, TextContent)
    ).strip()
    if not text:
        return  # start-session / non-text content needs no reply
    ctx.logger.info(f"query from {sender[:16]}…: {text[:80]}")
    try:
        answer = await asyncio.to_thread(_query_engine, text)
    except Exception as exc:  # engine down ≠ agent down — answer honestly
        ctx.logger.error(f"engine unreachable: {exc}")
        answer = (
            "The Meridian fares engine is not reachable right now, so I have "
            "no numbers to give you — I never invent one. Please try again "
            "once the backend is up."
        )
    await ctx.send(sender, _chat_text(answer))


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    ctx.logger.debug(f"ack from {sender[:16]}… for {msg.acknowledged_msg_id}")


agent.include(chat_proto, publish_manifest=True)


if __name__ == "__main__":
    agent.run()
