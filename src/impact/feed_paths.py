"""Bundle the 9 RDG feed paths the impact engine needs.

Most callers want all 9. `default_for_data_dir` constructs the canonical
RJFAF805.* layout, so test code and the demo CLI just say
`FeedPaths.default_for_data_dir(REPO_ROOT / 'data')` and don't repeat the
file names."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FeedPaths:
    ffl: Path
    loc: Path
    fsc: Path
    nfo: Path
    rlc: Path
    dis: Path
    rcm: Path
    frr: Path
    tty: Path
    # Optional .TOC (fare-TOC code -> operator name). Missing on minimal
    # installs; TOC-scoped features then show bare codes instead of names.
    toc: Path | None = None
    # Optional timetable (RSPS5046 .MCA CIF master). Present when an NRDP
    # timetable bundle has been unzipped into data/; absent on minimal
    # installs. Modules that can use it (splits) fall back to a hardcoded
    # whitelist when this is None or the file is missing.
    timetable_mca: Path | None = None
    # Optional ODM CSV (ORR-style origin-destination matrix). Present when
    # an OGL-licensed release has been dropped at data/odm/odm.csv. The
    # revenue_odm block degrades to None + a note when this is missing.
    odm_csv: Path | None = None
    # Optional RSPS5047 routeing-guide feed. Auto-detected from
    # `data/RJRG*.RG?`. Files are all-or-nothing per snapshot: any RJRG
    # match triggers population of all extensions we care about; missing
    # extensions inside a bundle become None. Callers (routeing engine)
    # degrade with a note when required files are absent.
    rgs: Path | None = None   # § 6.2  station -> routeing points
    rgg: Path | None = None   # § 6.3  station group -> main CRS
    rgp: Path | None = None   # § 6.4  routeing-point list
    rgn: Path | None = None   # § 6.5  nodes (routeing points + interchanges)
    rgm: Path | None = None   # § 6.6  map codes
    rgl: Path | None = None   # § 6.7  per-map links between nodes
    rgr: Path | None = None   # § 6.8  permitted routes
    rgd: Path | None = None   # § 6.9  station-link distances
    rgf: Path | None = None   # § 6.10 easement definition (E/L/D/X)
    rgh: Path | None = None   # § 6.11 easement TOC
    rgc: Path | None = None   # § 6.13 London stations (Cross-London)
    rgy: Path | None = None   # § 6.15 CRS<->NLC cross-reference
    rge: Path | None = None   # § 6.16 easement text (free-form English)

    @classmethod
    def default_for_data_dir(cls, data_dir: Path, *, snapshot: str = "RJFAF805") -> "FeedPaths":
        """RJFAF805 snapshot layout used by the rest of the project.
        Timetable .MCA is auto-detected from `data/RJTTF*.MCA` if present
        (the DTD timetable bundle names rotate with each snapshot).
        ODM CSV is auto-detected from `data/odm/odm.csv` if present.
        Routeing-guide files auto-detected from `data/RJRG*.RG?` — the
        RJRG sequence number rotates per snapshot so we pick the highest."""
        d = Path(data_dir)
        tt_candidates = sorted(d.glob("RJTTF*.MCA"))
        odm_candidate = d / "odm" / "odm.csv"
        rjrg_stem = _latest_rjrg_stem(d)
        return cls(
            ffl=d / f"{snapshot}.FFL",
            loc=d / f"{snapshot}.LOC",
            fsc=d / f"{snapshot}.FSC",
            nfo=d / f"{snapshot}.NFO",
            rlc=d / f"{snapshot}.RLC",
            dis=d / f"{snapshot}.DIS",
            rcm=d / f"{snapshot}.RCM",
            frr=d / f"{snapshot}.FRR",
            tty=d / f"{snapshot}.TTY",
            toc=(d / f"{snapshot}.TOC") if (d / f"{snapshot}.TOC").exists() else None,
            timetable_mca=tt_candidates[-1] if tt_candidates else None,
            odm_csv=odm_candidate if odm_candidate.exists() else None,
            rgs=_rjrg(d, rjrg_stem, "RGS"),
            rgg=_rjrg(d, rjrg_stem, "RGG"),
            rgp=_rjrg(d, rjrg_stem, "RGP"),
            rgn=_rjrg(d, rjrg_stem, "RGN"),
            rgm=_rjrg(d, rjrg_stem, "RGM"),
            rgl=_rjrg(d, rjrg_stem, "RGL"),
            rgr=_rjrg(d, rjrg_stem, "RGR"),
            rgd=_rjrg(d, rjrg_stem, "RGD"),
            rgf=_rjrg(d, rjrg_stem, "RGF"),
            rgh=_rjrg(d, rjrg_stem, "RGH"),
            rgc=_rjrg(d, rjrg_stem, "RGC"),
            rgy=_rjrg(d, rjrg_stem, "RGY"),
            rge=_rjrg(d, rjrg_stem, "RGE"),
        )

    def missing(self) -> list[Path]:
        """Return any of the 9 required paths that don't exist on disk; for
        test skips. The timetable .MCA is optional and never reported here."""
        return [p for p in (self.ffl, self.loc, self.fsc, self.nfo,
                            self.rlc, self.dis, self.rcm, self.frr, self.tty)
                if not p.exists()]


def _latest_rjrg_stem(data_dir: Path) -> str | None:
    """Highest-numbered RJRG bundle stem in `data_dir`, or None if absent.

    Bundle file names are `RJRGnnnn.EXT` (spec § 4.2); nnnn increases with
    each snapshot.  We pick the stem shared by the newest bundle so all
    12 extensions come from the same generation."""
    candidates = sorted(data_dir.glob("RJRG*.RGP"))  # RGP is small & always present
    if not candidates:
        return None
    return candidates[-1].stem  # e.g. 'RJRG0117'


def _rjrg(data_dir: Path, stem: str | None, ext: str) -> Path | None:
    if stem is None:
        return None
    p = data_dir / f"{stem}.{ext}"
    return p if p.exists() else None
