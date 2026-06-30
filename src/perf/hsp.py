"""HSP (Historic Service Performance) three-mode fetcher.

Source: NRDP `https://hsp-prod.rockshore.net/api/v1/serviceMetrics` — per-service
on-time stats for a corridor + date window + day type. HTTP Basic auth with the
caller's NRDP email/password.

The fetcher NEVER raises on transient failure: it tries live -> on-disk cache ->
committed fixture in order, recording each fall-through reason in `notes`. Only a
missing fixture (the last line of defence) raises `FileNotFoundError`, which the
API layer maps to a clean 400 (see `src/api/main.py:_file_not_found`).

Design choices:
  - stdlib `urllib.request` — matches `tools/fetch_brfares.py` convention; no
    new run-time dependency. The project pulls `httpx` transitively via FastAPI
    but the engine deliberately stays dep-light.
  - On-disk cache is best-effort JSON; cache key is sha1 of the normalized
    request body. Cache write errors are swallowed (recorded in notes) so a
    read-only filesystem never breaks a successful live call.
  - Result mode is always recorded so the UI can show a "live | cached |
    fixture" badge that never lies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping

DEFAULT_HSP_BASE_URL = "https://hsp-prod.rockshore.net"
SERVICE_METRICS_PATH = "/api/v1/serviceMetrics"
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 30.0
USER_AGENT = "fares-cockpit-perf/0.1 (+hackathon)"

DayType = Literal["WEEKDAY", "SATURDAY", "SUNDAY"]
FetchMode = Literal["live", "cached", "fixture"]


@dataclass(frozen=True)
class ServicePerformance:
    """Headline attributes of a scheduled service in the HSP response."""
    gbtt_ptd: str            # public timetable departure (HHMM)
    gbtt_pta: str            # public timetable arrival (HHMM)
    origin_crs: str
    dest_crs: str
    toc_code: str
    matched_services: int
    rids: tuple[str, ...]    # individual rail-service ids matched


@dataclass(frozen=True)
class ServiceTolerance:
    """The on-time-performance metrics for a single service.

    HSP returns one row per tolerance band (e.g. on-time, within 5, within 10).
    Each band carries: late-by-minutes threshold, % within tolerance, raw counts
    in and out of tolerance. We keep the three as parallel tuples of (mins, value)
    pairs sorted by `mins` ascending so the UI can render a step-curve cleanly.
    """
    service: ServicePerformance
    percent_tolerance: tuple[tuple[int, float], ...]
    num_tolerance: tuple[tuple[int, int], ...]
    num_not_tolerance: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class PerformanceResult:
    corridor_from_crs: str
    corridor_to_crs: str
    from_date: str           # YYYY-MM-DD
    to_date: str
    days: DayType
    services: tuple[ServiceTolerance, ...]
    mode: FetchMode
    fetched_at: str          # ISO-8601 UTC; when the *source* response was produced
    source_url: str | None
    notes: tuple[str, ...]   # honest disclosures (fall-through reasons, cache age)


# --- Public entry point --------------------------------------------------------


def fetch_performance(
    from_crs: str,
    to_crs: str,
    from_date: str,
    to_date: str,
    days: DayType = "WEEKDAY",
    *,
    cache_dir: Path,
    fixture_dir: Path,
    env: Mapping[str, str] | None = None,
    base_url: str | None = None,
    urlopen=None,
) -> PerformanceResult:
    """Three-mode fetch: live -> cached -> fixture. Never raises on net/auth
    failure; only a missing fixture raises `FileNotFoundError`.

    `urlopen` is injectable for tests (monkeypatching `urllib.request.urlopen`
    is fragile across pytest sessions). Defaults to the real one.
    """
    env = env if env is not None else os.environ
    base_url = base_url or env.get("HSP_BASE_URL", DEFAULT_HSP_BASE_URL)
    body = _build_body(from_crs, to_crs, from_date, to_date, days)
    cache_key = _cache_key(body)
    notes: list[str] = []

    # 1. live ------------------------------------------------------------------
    email = env.get("NRDP_EMAIL", "").strip()
    password = env.get("NRDP_PASSWORD", "").strip()
    if email and password:
        try:
            raw = _post_hsp(base_url, body, email, password, urlopen=urlopen)
            result = _parse_response(
                raw, from_crs, to_crs, from_date, to_date, days,
                mode="live",
                fetched_at=_now_utc_iso(),
                source_url=base_url + SERVICE_METRICS_PATH,
                notes=tuple(notes),
            )
            _write_cache(cache_dir, cache_key, raw, fetched_at=result.fetched_at, notes=notes)
            # Re-bind notes after possible cache-write warning.
            return PerformanceResult(**{**result.__dict__, "notes": tuple(notes)})
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
            notes.append(f"HSP live call failed ({type(exc).__name__}: {exc}); falling back to cache")
    else:
        notes.append("NRDP_EMAIL/NRDP_PASSWORD not set; skipping live HSP call")

    # 2. cached ----------------------------------------------------------------
    cache_path = Path(cache_dir) / f"{cache_key}.json"
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            raw = payload["raw"]
            fetched_at = payload.get("fetched_at") or _iso_from_mtime(cache_path)
            age_s = max(0, int(time.time() - cache_path.stat().st_mtime))
            notes.append(f"served from on-disk cache (age {age_s}s)")
            return _parse_response(
                raw, from_crs, to_crs, from_date, to_date, days,
                mode="cached",
                fetched_at=fetched_at,
                source_url=base_url + SERVICE_METRICS_PATH,
                notes=tuple(notes),
            )
        except (OSError, ValueError, KeyError) as exc:
            notes.append(f"cache present but unreadable ({type(exc).__name__}: {exc}); falling back to fixture")
    else:
        notes.append("no cache present; falling back to fixture")

    # 3. fixture ---------------------------------------------------------------
    fixture_path = _fixture_path_for(fixture_dir, from_crs, to_crs, days)
    if not fixture_path.exists():
        raise FileNotFoundError(
            f"HSP fixture missing for corridor {from_crs}->{to_crs} ({days}): "
            f"expected at {fixture_path}. Capture one with the curl in the plan."
        )
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    notes.append(f"served from committed fixture {fixture_path.name}")
    return _parse_response(
        raw, from_crs, to_crs, from_date, to_date, days,
        mode="fixture",
        fetched_at=_iso_from_mtime(fixture_path),
        source_url=None,
        notes=tuple(notes),
    )


# --- HTTP --------------------------------------------------------------------


def _build_body(from_crs: str, to_crs: str, from_date: str, to_date: str, days: DayType) -> dict:
    return {
        "from_loc": from_crs.upper(),
        "to_loc": to_crs.upper(),
        "from_time": "0000",
        "to_time": "2359",
        "from_date": from_date,
        "to_date": to_date,
        "days": days,
    }


def _post_hsp(base_url: str, body: dict, email: str, password: str, *, urlopen=None) -> dict:
    """POST to HSP serviceMetrics. Raises on any non-2xx / network error."""
    url = base_url.rstrip("/") + SERVICE_METRICS_PATH
    data = json.dumps(body).encode("utf-8")
    creds = base64.b64encode(f"{email}:{password}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {creds}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    opener = urlopen if urlopen is not None else urllib.request.urlopen
    with opener(req, timeout=READ_TIMEOUT_S) as resp:  # type: ignore[arg-type]
        status = getattr(resp, "status", 200)
        if status != 200:
            raise urllib.error.HTTPError(url, status, f"HSP non-200: {status}", resp.headers, None)
        raw = resp.read()
    parsed = json.loads(raw.decode("utf-8"))
    if "Services" not in parsed:
        raise ValueError("HSP response missing 'Services' field")
    return parsed


# --- Cache -------------------------------------------------------------------


def _cache_key(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(canonical).hexdigest()[:16]


def _write_cache(cache_dir: Path, cache_key: str, raw: dict, *, fetched_at: str, notes: list[str]) -> None:
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        path = Path(cache_dir) / f"{cache_key}.json"
        payload = {"fetched_at": fetched_at, "raw": raw}
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        notes.append(f"cache write skipped ({type(exc).__name__}: {exc})")


# --- Fixture lookup ----------------------------------------------------------


def _fixture_path_for(fixture_dir: Path, from_crs: str, to_crs: str, days: DayType) -> Path:
    """Deterministic per-corridor fixture filename, with a fall-back demo file.

    Prefers `<from>_<to>_<days>.json` (lowercase); if absent, returns the
    canonical demo fixture for MAN->EUS WEEKDAY so the demo runs even with no
    fixture for the exact requested window."""
    primary = Path(fixture_dir) / f"{from_crs.lower()}_{to_crs.lower()}_{days.lower()}.json"
    if primary.exists():
        return primary
    return Path(fixture_dir) / "man_eus_weekday.json"


# --- Parsing -----------------------------------------------------------------


def _parse_response(
    raw: dict,
    from_crs: str,
    to_crs: str,
    from_date: str,
    to_date: str,
    days: DayType,
    *,
    mode: FetchMode,
    fetched_at: str,
    source_url: str | None,
    notes: tuple[str, ...],
) -> PerformanceResult:
    services: list[ServiceTolerance] = []
    for svc in raw.get("Services", []):
        attrs = svc.get("serviceAttributesMetrics", {})
        metrics = svc.get("Metrics", [])
        try:
            matched = int(attrs.get("matched_services", 0))
        except (TypeError, ValueError):
            matched = 0
        perf = ServicePerformance(
            gbtt_ptd=str(attrs.get("gbtt_ptd", "")),
            gbtt_pta=str(attrs.get("gbtt_pta", "")),
            origin_crs=str(attrs.get("origin_location", from_crs)).upper(),
            dest_crs=str(attrs.get("destination_location", to_crs)).upper(),
            toc_code=str(attrs.get("toc_code", "")).upper(),
            matched_services=matched,
            rids=tuple(str(r) for r in attrs.get("rids", [])),
        )
        pct: list[tuple[int, float]] = []
        nt: list[tuple[int, int]] = []
        nnt: list[tuple[int, int]] = []
        for m in metrics:
            try:
                mins = int(m.get("tolerance_value", 0))
            except (TypeError, ValueError):
                continue
            try:
                pct.append((mins, float(m.get("percent_tolerance", 0))))
            except (TypeError, ValueError):
                pass
            try:
                nt.append((mins, int(m.get("num_tolerance", 0))))
            except (TypeError, ValueError):
                pass
            try:
                nnt.append((mins, int(m.get("num_not_tolerance", 0))))
            except (TypeError, ValueError):
                pass
        services.append(ServiceTolerance(
            service=perf,
            percent_tolerance=tuple(sorted(pct)),
            num_tolerance=tuple(sorted(nt)),
            num_not_tolerance=tuple(sorted(nnt)),
        ))

    return PerformanceResult(
        corridor_from_crs=from_crs.upper(),
        corridor_to_crs=to_crs.upper(),
        from_date=from_date,
        to_date=to_date,
        days=days,
        services=tuple(services),
        mode=mode,
        fetched_at=fetched_at,
        source_url=source_url,
        notes=notes,
    )


# --- Timestamps --------------------------------------------------------------


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iso_from_mtime(path: Path) -> str:
    return (
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )
