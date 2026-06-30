"""Smoke tests for the HSP performance fetcher.

Fixture-mode only — no network, no real NRDP credentials, no `data/` feed.
Verifies:

  1. `fetch_performance` falls through live -> cache -> fixture deterministically,
     surfaces every fall-through reason in `notes`, and parses a representative
     Services + Metrics payload into the typed `PerformanceResult`.
  2. Parsing keeps per-band tolerance rows sorted and types coerced correctly,
     so the UI's step-curve view receives clean data.

The heavy live-call battery (auth, multi-corridor sweep, NRE cross-check) is a
separate session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.perf import PerformanceResult, fetch_performance

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "hsp"


@pytest.fixture(scope="module")
def empty_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throw-away cache dir so the fetcher cleanly misses the cache layer."""
    return tmp_path_factory.mktemp("perf_cache_empty")


# --- 1. Fixture mode end-to-end -----------------------------------------------


def test_fetch_perf_falls_through_to_fixture(empty_cache: Path) -> None:
    """No env creds + empty cache -> fixture. Mode and notes must say so."""
    result = fetch_performance(
        "MAN", "EUS", "2026-05-01", "2026-05-31", "WEEKDAY",
        cache_dir=empty_cache,
        fixture_dir=FIXTURE_DIR,
        env={},  # no NRDP creds
    )
    assert isinstance(result, PerformanceResult)
    assert result.mode == "fixture"
    assert result.corridor_from_crs == "MAN"
    assert result.corridor_to_crs == "EUS"
    assert result.from_date == "2026-05-01"
    assert result.to_date == "2026-05-31"
    assert result.days == "WEEKDAY"
    assert result.source_url is None
    # Both fall-throughs must be recorded — opaque silence would be a lie.
    joined = "\n".join(result.notes)
    assert "NRDP_EMAIL" in joined or "skipping live" in joined
    assert "fixture" in joined.lower()
    # Parsing produced at least one service with at least one tolerance band.
    assert len(result.services) >= 1
    svc = result.services[0]
    assert svc.service.origin_crs == "MAN"
    assert svc.service.dest_crs == "EUS"
    assert svc.service.matched_services > 0
    assert len(svc.percent_tolerance) >= 1
    # Tuples sorted by minutes-late ascending.
    mins_seq = [m for m, _ in svc.percent_tolerance]
    assert mins_seq == sorted(mins_seq)


def test_fetch_perf_live_failure_falls_to_cache_then_fixture(empty_cache: Path) -> None:
    """When live POST raises, fetcher silently degrades and records the reason."""

    def _boom(_req, timeout=None):  # noqa: ARG001
        raise TimeoutError("simulated HSP timeout")

    result = fetch_performance(
        "MAN", "EUS", "2026-05-01", "2026-05-31", "WEEKDAY",
        cache_dir=empty_cache,
        fixture_dir=FIXTURE_DIR,
        env={"NRDP_EMAIL": "a@b.c", "NRDP_PASSWORD": "x"},
        urlopen=_boom,
    )
    assert result.mode == "fixture"
    joined = "\n".join(result.notes)
    assert "live call failed" in joined.lower()
    assert "timeout" in joined.lower()
    assert "fixture" in joined.lower()


def test_fetch_perf_serves_cached_on_live_failure(tmp_path: Path) -> None:
    """A pre-seeded cache file short-circuits the fixture fall-through."""
    cache_dir = tmp_path / "perf_cache"
    cache_dir.mkdir()
    body = {
        "from_loc": "MAN", "to_loc": "EUS", "from_time": "0000", "to_time": "2359",
        "from_date": "2026-05-01", "to_date": "2026-05-31", "days": "WEEKDAY",
    }
    # Re-derive the cache key the way the module does so the seeded file is found.
    from src.perf.hsp import _cache_key
    raw = json.loads((FIXTURE_DIR / "man_eus_weekday.json").read_text(encoding="utf-8"))
    (cache_dir / f"{_cache_key(body)}.json").write_text(
        json.dumps({"fetched_at": "2026-05-31T12:00:00+00:00", "raw": raw}),
        encoding="utf-8",
    )

    def _boom(_req, timeout=None):  # noqa: ARG001
        raise TimeoutError("simulated HSP timeout")

    result = fetch_performance(
        "MAN", "EUS", "2026-05-01", "2026-05-31", "WEEKDAY",
        cache_dir=cache_dir,
        fixture_dir=FIXTURE_DIR,
        env={"NRDP_EMAIL": "a@b.c", "NRDP_PASSWORD": "x"},
        urlopen=_boom,
    )
    assert result.mode == "cached"
    assert "on-disk cache" in "\n".join(result.notes).lower()
    assert result.fetched_at == "2026-05-31T12:00:00+00:00"


# --- 2. Missing fixture is the only hard error --------------------------------


def test_fetch_perf_missing_fixture_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="HSP fixture missing"):
        fetch_performance(
            "ZZZ", "YYY", "2026-05-01", "2026-05-31", "WEEKDAY",
            cache_dir=tmp_path / "cache",
            fixture_dir=tmp_path / "fix",
            env={},
        )
