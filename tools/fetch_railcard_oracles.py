"""Pull BRFares JSON for 5 railcards on the MAN-EUS demo corridor.

Reads the session JWT from $BRFARES_JWT, fetches one JSON per railcard,
saves to data/brfares_man_eus_<rlc>.json. Use:

    BRFARES_JWT='eyJ...' python tools/fetch_railcard_oracles.py

The JWT lives ~10 minutes (decode at jwt.io to see exp). Grab a fresh one
from devtools just before running — see CLAUDE chat notes.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
ENDPOINT = "https://gw.brfares.com/internal_queryextra"
RAILCARDS = ["YNG", "SRN", "2TR", "FAM", "DIS"]
ORIG = "MAN"
DEST = "EUS"
DATE = "20260301"  # matches the regulation freeze baseline


def main() -> int:
    jwt = os.environ.get("BRFARES_JWT", "").strip()
    if not jwt:
        print("error: set BRFARES_JWT env var to the Bearer token from devtools", file=sys.stderr)
        return 2

    DATA_DIR.mkdir(exist_ok=True)
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Origin": "https://www.brfares.com",
        "Referer": "https://www.brfares.com/",
        "Accept": "application/json",
        "User-Agent": "fares-cockpit-oracle/0.1",
    }

    failed: list[str] = []
    for rlc in RAILCARDS:
        qs = urllib.parse.urlencode({"orig": ORIG, "dest": DEST, "rlc": rlc, "date": DATE})
        url = f"{ENDPOINT}?{qs}"
        out = DATA_DIR / f"brfares_man_eus_{rlc.lower()}.json"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:200]
            print(f"  FAIL {rlc}: HTTP {exc.code} :: {body}", file=sys.stderr)
            failed.append(rlc)
            continue
        except Exception as exc:
            print(f"  FAIL {rlc}: {exc!r}", file=sys.stderr)
            failed.append(rlc)
            continue
        with out.open("w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        n = len(payload.get("fares", [])) if isinstance(payload, dict) else 0
        print(f"  OK   {rlc}: {n} fares -> {out.relative_to(REPO_ROOT)}")

    if failed:
        print(f"\n{len(failed)} failed: {failed}", file=sys.stderr)
        print("most common cause is an expired JWT — pull a fresh one and re-run", file=sys.stderr)
        return 1
    print(f"\nAll {len(RAILCARDS)} JSONs saved. Tell Claude to run the comparison.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
