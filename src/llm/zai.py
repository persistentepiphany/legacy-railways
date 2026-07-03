"""Minimal Z.AI (GLM) client — build-time only.

Z.AI exposes an OpenAI-compatible endpoint at
`https://api.z.ai/api/paas/v4/chat/completions` with `Authorization: Bearer
<ZAI_API_KEY>`.  We use `glm-4.6` — the current-generation GLM model
(equivalent tier to Claude Sonnet for structured-extraction workloads).

Intentionally dependency-free (stdlib `urllib.request` + `json`).  This
runs in one-off build scripts, not on a hot path — no need for httpx or
the OpenAI SDK.

Contract: `chat_json(system, user)` returns whatever JSON object the model
produced.  The caller is responsible for validating the shape.  A model
that returns non-JSON raises `ZaiError`; the translator surfaces those as
per-easement failures rather than crashing the whole build.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any


def _ssl_context() -> ssl.SSLContext:
    """SSL context that finds the system/certifi CA bundle.

    Homebrew's Python 3.11 doesn't ship with a linked cert store; without
    this the connection to api.z.ai fails with
    CERTIFICATE_VERIFY_FAILED.  Prefer certifi when installed (it always
    is via the pip dep chain), else fall back to the default context."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"
DEFAULT_MODEL = "glm-4.6"


class ZaiError(RuntimeError):
    """Raised for auth failures, network errors, or non-JSON responses."""


def _api_key() -> str:
    key = os.environ.get("ZAI_API_KEY", "").strip()
    if not key:
        raise ZaiError(
            "ZAI_API_KEY not set. Add it to .env (this project's LLM key "
            "for the build-time easement-text translation). Runtime never "
            "reads this."
        )
    return key


def chat_json(
    system: str,
    user: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    max_tokens: int = 2000,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Round-trip a chat completion, returning the parsed JSON object.

    Uses `response_format={"type": "json_object"}` (OpenAI-compatible flag
    honoured by Z.AI) to force strict JSON output.  A non-JSON response is
    raised as `ZaiError` — the caller decides whether to retry or skip.

    `temperature=0` is deliberate: we want deterministic translation of
    the same easement text on repeated builds so the cached predicates
    JSON is stable in git."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    req = urllib.request.Request(
        ZAI_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=_ssl_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ZaiError(f"Z.AI HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}") from e
    except urllib.error.URLError as e:
        raise ZaiError(f"Z.AI network error: {e}") from e
    except TimeoutError as e:
        # Python 3.10+ raises TimeoutError directly for socket-level timeouts
        # rather than wrapping in URLError; catch explicitly so the batch
        # translator can skip this ref and continue.
        raise ZaiError(f"Z.AI timeout after {timeout_s}s: {e}") from e
    except OSError as e:
        # Catches transient connection resets / DNS blips.
        raise ZaiError(f"Z.AI socket error: {e}") from e
    except json.JSONDecodeError as e:
        raise ZaiError(f"Z.AI returned non-JSON envelope: {e}") from e

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ZaiError(f"Z.AI response missing choices[0].message.content: {payload}") from e

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ZaiError(
            f"Z.AI model returned non-JSON despite response_format: {content[:400]}"
        ) from e


__all__ = ["ZAI_URL", "DEFAULT_MODEL", "ZaiError", "chat_json"]
