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
    # Optional timetable (RSPS5046 .MCA CIF master). Present when an NRDP
    # timetable bundle has been unzipped into data/; absent on minimal
    # installs. Modules that can use it (splits) fall back to a hardcoded
    # whitelist when this is None or the file is missing.
    timetable_mca: Path | None = None

    @classmethod
    def default_for_data_dir(cls, data_dir: Path, *, snapshot: str = "RJFAF805") -> "FeedPaths":
        """RJFAF805 snapshot layout used by the rest of the project.
        Timetable .MCA is auto-detected from `data/RJTTF*.MCA` if present
        (the DTD timetable bundle names rotate with each snapshot)."""
        d = Path(data_dir)
        tt_candidates = sorted(d.glob("RJTTF*.MCA"))
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
            timetable_mca=tt_candidates[-1] if tt_candidates else None,
        )

    def missing(self) -> list[Path]:
        """Return any of the 9 required paths that don't exist on disk; for
        test skips. The timetable .MCA is optional and never reported here."""
        return [p for p in (self.ffl, self.loc, self.fsc, self.nfo,
                            self.rlc, self.dis, self.rcm, self.frr, self.tty)
                if not p.exists()]
