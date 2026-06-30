"""FastAPI surface over the fares-cockpit engine.

Thin HTTP layer — every endpoint dispatches to an existing function in
src/resolver, src/impact, or src/staging. No engine logic lives here."""

from src.api.main import app

__all__ = ["app"]
