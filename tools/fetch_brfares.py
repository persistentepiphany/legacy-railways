"""Fetch ground-truth fares from BRFares' JSON API.

BRFares (brfares.com) is the de-facto public mirror of the RDG fares feed.
Its `gw.brfares.com` endpoints return JSON, so we use them as a correctness
oracle for the demo corridor — Manchester Piccadilly <-> London Euston (and
Stoke-on-Trent <-> Manchester for the §5 fifth case).

The site's JS reveals two auth tiers (see brfaresJScore.*.js):

    1. AUTOCOMPLETE (`/internal_ac_loc?term=...`) — uses a "page" JWT
       (`pweb_tk`) embedded inline in the homepage HTML. Scrapeable.
    2. FARE QUERIES (`/internal_querysimple?orig=...&dest=...&rlc=...`) —
       use a "session" JWT minted by POST /fares_token in exchange for a
       Cloudflare Turnstile token. That needs a real browser; we cannot
       solve Turnstile from urllib.

So this script:
    a. Scrapes `pweb_tk` from https://www.brfares.com/ automatically and uses
       it to autocomplete-confirm MAN / EUS / SOT.
    b. For fare queries, accepts a `--session-token <JWT>` you paste from
       your browser's devtools (Network tab on any fare lookup at
       brfares.com — copy the value after "Authorization: Bearer ").
       Without it, the script prints clear instructions and exits cleanly.

Run from the repo root:

    python tools/fetch_brfares.py                              # autocomplete only
    python tools/fetch_brfares.py --session-token eyJ...       # full fare fetch
    python tools/fetch_brfares.py --session-token eyJ... --railcard YNG
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

BASE = "https://gw.brfares.com"
HOMEPAGE = "https://www.brfares.com/"
TIMEOUT_SECONDS = 30.0
USER_AGENT = "fares-cockpit-oracle/0.1 (+hackathon)"

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def _ssl_context() -> ssl.SSLContext:
    """Use certifi's CA bundle if installed (Python.org installs ship without one)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def _get_json(url: str, *, headers: dict[str, str] | None = None, attempt: int = 1) -> Any:
    """GET `url` and parse JSON. Retries once on 5xx. Logs every step."""
    print(f"  GET {url}", file=sys.stderr)
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=_SSL_CTX) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"  HTTP {exc.code} on attempt {attempt}: {body!r}", file=sys.stderr)
        if 500 <= exc.code < 600 and attempt == 1:
            print("  retrying once after 2s ...", file=sys.stderr)
            time.sleep(2.0)
            return _get_json(url, headers=headers, attempt=2)
        raise
    except urllib.error.URLError as exc:
        print(f"  network error on attempt {attempt}: {exc.reason!r}", file=sys.stderr)
        if attempt == 1:
            print("  retrying once after 2s ...", file=sys.stderr)
            time.sleep(2.0)
            return _get_json(url, headers=headers, attempt=2)
        raise

    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        snippet = raw[:500]
        print(f"  JSON parse error: {exc!r}", file=sys.stderr)
        print(f"  body (first 500 bytes): {snippet!r}", file=sys.stderr)
        raise


# --- Endpoints --------------------------------------------------------------

_PWEB_RE = re.compile(r'pweb_tk\s*=\s*"([^"]+)"')


def scrape_page_token() -> str:
    """GET brfares.com homepage and extract the inline pweb_tk JWT.

    The site's JS uses this token as Bearer for autocomplete (/internal_ac_loc).
    Fare queries (/internal_querysimple) instead need a Turnstile-minted
    session JWT — see scrape_or_use_session_token / module docstring.
    """
    print(f"  GET {HOMEPAGE}  (to scrape pweb_tk)", file=sys.stderr)
    req = urllib.request.Request(HOMEPAGE, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=_SSL_CTX) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    m = _PWEB_RE.search(html)
    if not m:
        raise RuntimeError("could not scrape pweb_tk from brfares.com homepage")
    tok = m.group(1)
    print(f"  pweb_tk scraped ({len(tok)} chars)", file=sys.stderr)
    return tok


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + token,
        "Origin": "https://www.brfares.com",
        "Referer": "https://www.brfares.com/",
    }


def lookup(term: str, page_token: str) -> list[dict[str, Any]]:
    """Autocomplete a station term via /internal_ac_loc (page-token auth)."""
    url = f"{BASE}/internal_ac_loc?{urllib.parse.urlencode({'term': term})}"
    data = _get_json(url, headers=_auth_headers(page_token))
    if isinstance(data, dict) and "data" in data:
        return list(data["data"])
    if isinstance(data, list):
        return data
    raise ValueError(f"unexpected lookup payload shape: {type(data).__name__}")


def fetch_fares(
    orig: str,
    dest: str,
    session_token: str,
    rlc: str | None = None,
    date: str | None = None,
) -> Any:
    """Query /internal_queryextra. Returns raw decoded JSON.

    `session_token` MUST be a Turnstile-minted session JWT — copy from your
    browser's devtools (Network tab on a fare lookup at brfares.com).

    Endpoint history: `/internal_querysimple` returned data through ~2024 but
    now 404s; the live site uses `/internal_queryextra` with mandatory
    `rlc=` (3 chars; 3 spaces = no railcard) and `date=YYYYMMDD`.
    Verified against devtools capture 2026-06.
    """
    if date is None:
        date = time.strftime("%Y%m%d")
    rlc_token = rlc if (rlc and len(rlc) == 3) else "   "
    params: dict[str, str] = {"orig": orig, "dest": dest, "rlc": rlc_token, "date": date}
    url = f"{BASE}/internal_queryextra?{urllib.parse.urlencode(params)}"
    return _get_json(url, headers=_auth_headers(session_token))


# --- Formatting -------------------------------------------------------------

def _pounds_to_pence(value: Any) -> int | None:
    """Convert "94.50" / 94.5 / 9450 -> 9450 pence via Decimal (no float drift)."""
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    # BRFares returns pounds as a string most of the time; integer pence rarely.
    # Heuristic: if value has a decimal point in its string form, it's pounds.
    if "." in str(value):
        return int((d * 100).to_integral_value())
    # Plain integer — assume pounds (BRFares convention). Multiply by 100.
    return int((d * 100).to_integral_value())


def _get_path(d: Any, *path: str) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def print_lookup_table(term: str, candidates: list[dict[str, Any]]) -> None:
    print(f"\n== lookup('{term}') — {len(candidates)} candidates ==")
    print(f"  {'CRS':<5} {'NLC':<6} NAME")
    for c in candidates[:25]:
        crs = c.get("crs") or c.get("code") or _get_path(c, "loc", "crs") or "?"
        nlc = c.get("nlc") or _get_path(c, "loc", "nlc") or "?"
        name = c.get("name") or c.get("label") or c.get("value") or "?"
        print(f"  {str(crs):<5} {str(nlc):<6} {name}")
    if len(candidates) > 25:
        print(f"  ... and {len(candidates) - 25} more")


def print_fares_table(label: str, payload: Any) -> None:
    fares = payload.get("fares") if isinstance(payload, dict) else payload
    if not isinstance(fares, list):
        # Some responses bury fares deeper.
        fares = _get_path(payload, "data", "fares") or []
    print(f"\n== {label} — {len(fares)} fares ==")
    print(f"  {'CODE':<5} {'CLS':<3} {'CAT':<3} {'PENCE':>7}  NAME")
    for f in fares:
        code = _get_path(f, "ticket", "code") or f.get("ticket_code") or "?"
        name = _get_path(f, "ticket", "name") or f.get("ticket_name") or "?"
        cls = _get_path(f, "ticket", "class") or f.get("class") or "?"
        category = f.get("category", "?")
        price_raw = f.get("fare") if "fare" in f else f.get("adult_fare")
        pence = _pounds_to_pence(price_raw)
        pence_s = f"{pence}" if pence is not None else "-"
        print(f"  {str(code):<5} {str(cls):<3} {str(category):<3} {pence_s:>7}  {name}")


# --- Orchestration ----------------------------------------------------------

def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    print(f"  wrote {path.relative_to(REPO_ROOT)}", file=sys.stderr)


SESSION_TOKEN_INSTRUCTIONS = """
To fetch fares you need a Turnstile-minted session JWT (the page JWT we just
scraped only covers autocomplete). Steps:

  1. Open https://www.brfares.com/ in your browser.
  2. Open devtools -> Network tab; type "fares_token" in the filter.
  3. Run any fare query on the site (e.g. MAN -> EUS).
  4. Click the most recent /internal_querysimple or /internal_querydetail request.
  5. Under "Request Headers" copy the value after "Authorization: Bearer ".
  6. Re-run:  python tools/fetch_brfares.py --session-token <paste>
""".rstrip()


def run(railcard: str | None, session_token: str | None) -> int:
    print("BRFares oracle fetch — gw.brfares.com (internal_* endpoints)")
    print(f"data dir: {DATA_DIR}")

    # Step 0 — page token for autocomplete.
    try:
        page_token = scrape_page_token()
    except Exception as exc:
        print(f"  failed to scrape page token: {exc!r}", file=sys.stderr)
        return 1

    # Step 1 — confirm station codes.
    for term in ("manchester piccadilly", "london euston", "stoke"):
        try:
            cands = lookup(term, page_token)
        except Exception as exc:
            print(f"  lookup({term!r}) failed: {exc!r}", file=sys.stderr)
            cands = []
        print_lookup_table(term, cands)

    if session_token is None:
        print("\nno --session-token provided; skipping fare fetch.")
        print(SESSION_TOKEN_INSTRUCTIONS)
        return 0

    # Step 2 — pull the two corridors. When railcard is set, save to a
    # railcard-suffixed file so adult and railcard oracles can coexist.
    rlc_suffix = f"_railcard_{railcard}" if railcard else ""
    corridors = [
        ("MAN", "EUS", DATA_DIR / f"brfares_man_eus{rlc_suffix}.json"),
        ("SOT", "MAN", DATA_DIR / f"brfares_sot_man{rlc_suffix}.json"),
    ]
    exit_code = 0
    for orig, dest, out in corridors:
        label = f"{orig} -> {dest}" + (f" (railcard {railcard})" if railcard else "")
        try:
            payload = fetch_fares(orig, dest, session_token, rlc=railcard)
        except Exception as exc:
            print(f"  fetch_fares({orig},{dest}) failed: {exc!r}", file=sys.stderr)
            exit_code = 1
            continue
        save_json(out, payload)
        print_fares_table(label, payload)

    return exit_code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tools/fetch_brfares.py",
        description="Pull ground-truth fares from gw.brfares.com for the demo corridor.",
    )
    p.add_argument(
        "--railcard",
        default=None,
        help="Optional railcard code (e.g. YNG for 16-25). Appended as &rlc=.",
    )
    p.add_argument(
        "--session-token",
        dest="session_token",
        default=None,
        help="Turnstile-minted JWT for fare endpoints. Paste from devtools; see --help epilogue.",
    )
    args = p.parse_args(argv)
    return run(args.railcard, args.session_token)


if __name__ == "__main__":
    raise SystemExit(main())
