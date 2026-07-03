"""One-shot local probe for the Meridian fares agent (no Agentverse needed).

Runs the REAL chat-protocol handlers (imported from src.agent.fares_agent)
on a local shell agent plus a throwaway probe agent in one Bureau, sends one
canonical query through the chat protocol, prints the engine-backed reply and
exits. Exit 0 = round trip worked; exit 1 = watchdog timeout.

Usage:  MERIDIAN_API_URL=http://127.0.0.1:8000 \
        .venv/bin/python tools/probe_fares_agent.py ["your question"]
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uagents import Agent, Bureau, Context  # noqa: E402
from uagents_core.contrib.protocols.chat import (  # noqa: E402
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
)

from src.agent.fares_agent import chat_proto  # noqa: E402  (real handlers)

QUERY = sys.argv[1] if len(sys.argv) > 1 else "fare from manchester to london euston"

shell = Agent(name="meridian-fares-local", seed="meridian probe shell (local only)")
shell.include(chat_proto)

probe = Agent(name="probe-sender", seed="meridian probe sender (local only)")


@probe.on_event("startup")
async def ask(ctx: Context) -> None:
    print(f"PROBE → {QUERY!r}", flush=True)
    await ctx.send(
        shell.address,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[TextContent(type="text", text=QUERY)],
        ),
    )


@probe.on_message(ChatMessage)
async def reply(ctx: Context, sender: str, msg: ChatMessage) -> None:
    for c in msg.content:
        if isinstance(c, TextContent):
            print(f"REPLY ← {c.text}", flush=True)
    os._exit(0)  # one-shot tool; Bureau has no graceful stop API


@probe.on_message(ChatAcknowledgement)
async def ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    print("ACK   ← received", flush=True)


def _timeout() -> None:
    print("TIMEOUT: no reply", flush=True)
    os._exit(1)


threading.Timer(120, _timeout).start()

bureau = Bureau(port=8030)
bureau.add(shell)
bureau.add(probe)

if __name__ == "__main__":
    bureau.run()
