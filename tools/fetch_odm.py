"""Fetch and normalize the ORR Origin-Destination Matrix into data/odm/odm.csv.

Source: ORR ODM 2022-23, published under the Open Government Licence on the
Rail Data Marketplace; downloaded here from the OGL community mirror linked
from the RailUK Forums release thread (ODM-22-23-sorted.csv.zip, ~16 MB zip,
~1.44M station-pair rows). Newer releases (2024/25) require a free Rail Data
Marketplace account — drop their CSV over data/odm/odm.csv to upgrade; the
loader's lenient header inference (src/impact/odm.py) handles ORR naming drift.

Normalization applied (each step reported, never silent):
  - NLCs zero-padded to 4 chars (the mirror stripped leading zeros on ~4.7k rows).
  - Columns slimmed to Financial_Year, origin_nlc, dest_nlc, journeys — the
    year column is kept so the loader can label the period honestly.
  - Rows with a non-numeric journey count or missing NLC are dropped and counted.

Usage:
  python tools/fetch_odm.py                # download + normalize
  python tools/fetch_odm.py --from FILE    # normalize a local csv/zip instead
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Sequence

DRIVE_FILE_ID = "1aVtjLxRG6EtjNKbo016HLo5XxTjgPCcu"
DOWNLOAD_URL = (
    "https://drive.usercontent.google.com/download"
    f"?id={DRIVE_FILE_ID}&export=download&confirm=t"
)
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "odm" / "odm.csv"

# Demo-corridor sanity pair: Manchester Piccadilly (2968) <-> London Euston (1444).
SANITY_PAIR = ("2968", "1444")


def _download(dest: Path) -> None:
    print(f"downloading ODM 2022-23 mirror ({DOWNLOAD_URL}) ...")
    req = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "fares-cockpit/odm-fetch"})
    with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    print(f"  -> {dest} ({dest.stat().st_size:,} bytes)")


def _open_csv(path: Path) -> io.TextIOWrapper:
    """Open the source as text whether it's a bare CSV or a zip with one CSV."""
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            sys.exit(f"expected exactly one CSV inside {path}, found {names}")
        print(f"  reading {names[0]} from zip")
        return io.TextIOWrapper(zf.open(names[0]), encoding="utf-8-sig")
    return path.open("r", encoding="utf-8-sig", newline="")


def _find_col(headers: "Sequence[str]", needles: tuple[str, ...], what: str) -> str:
    for h in headers:
        if any(n in h.lower() for n in needles):
            return h
    sys.exit(f"could not find a {what} column in {headers}")


def normalize(src: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    padded = dropped = kept = 0
    sanity = {SANITY_PAIR: 0, SANITY_PAIR[::-1]: 0}

    with _open_csv(src) as fh, out.open("w", newline="") as outfh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        o_col = _find_col(headers, ("origin_nlc",), "origin NLC")
        d_col = _find_col(headers, ("destination_nlc", "dest_nlc"), "destination NLC")
        j_col = _find_col(headers, ("journey",), "journeys")
        y_col = next((h for h in headers if "year" in h.lower()), None)

        writer = csv.writer(outfh)
        writer.writerow((["financial_year"] if y_col else []) + ["origin_nlc", "dest_nlc", "journeys"])
        for row in reader:
            o, d = row[o_col].strip(), row[d_col].strip()
            try:
                j = int(float(row[j_col]))
            except (TypeError, ValueError):
                dropped += 1
                continue
            if not o or not d:
                dropped += 1
                continue
            if len(o) < 4 or len(d) < 4:
                padded += 1
            o, d = o.zfill(4), d.zfill(4)
            writer.writerow(([row[y_col].strip()] if y_col else []) + [o, d, j])
            kept += 1
            if (o, d) in sanity:
                sanity[(o, d)] += j

    print(f"wrote {out}: {kept:,} rows kept, {dropped:,} dropped, {padded:,} NLCs zero-padded")
    man_eus, eus_man = sanity[SANITY_PAIR], sanity[SANITY_PAIR[::-1]]
    print(f"sanity MAN->EUS journeys/yr: {man_eus:,}   EUS->MAN: {eus_man:,}")
    if man_eus == 0 or eus_man == 0:
        sys.exit("SANITY FAIL: demo corridor missing from ODM — refusing to install a broken file")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="src", type=Path, default=None,
                    help="normalize a local csv/zip instead of downloading")
    args = ap.parse_args()

    if args.src is not None:
        normalize(args.src, OUT_PATH)
        return
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        _download(tmp_path)
        normalize(tmp_path, OUT_PATH)
    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
