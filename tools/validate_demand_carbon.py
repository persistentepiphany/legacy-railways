"""Gate-based validation for the demand + carbon ImpactReport modules.

Runs six pre-registered gates and writes docs/demand-carbon-validation.md:

  C1  carbon arithmetic reproduces the National Rail carbon-calculator
      substantiation worked example and the DEFRA blend identity.
  C2  carbon components: MAN->EUS distance vs the public route mileage;
      traction mix reads electric on the WCML and diesel on a known DMU
      corridor (WCML reading diesel = STOP: parser bug, not calibration).
  C3  oracle comparison vs the National Rail Carbon Calculator on 6
      corridors — SKIPPED with a TODO until data/carbon_oracle_template.json
      is filled in by hand (the script generates the template).
  D1  demand engine reproduces hand-computed published-elasticity worked
      examples exactly (Transport Scotland -0.641/-0.144 + PDFH LD London).
  D2  structural invariants (any violation = hard bug, not calibration).
  D3  plausibility of the demo change's predicted growth vs the
      elasticity-implied band.

THRESHOLDS ARE HARD-CODED BELOW AND PRE-REGISTERED — do not tune them to
make a gate pass; a FAIL is reported honestly. Verdict semantics:
PASS ships with the EST label; DEGRADE ships the specified downgrade;
SCRAP excludes the module from DEFAULT_INCLUDE; STOP means stop and fix.

Run:
    python -m tools.validate_demand_carbon
Exit codes: 0 = no FAIL/SCRAP/STOP, 1 = FAIL/DEGRADE present, 2 = SCRAP/STOP.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.impact.carbon_factors import (  # noqa: E402
    CAR_AVG_OCCUPANCY,
    CAR_KGCO2E_PER_VKM,
    RAIL_NATIONAL_KGCO2E_PER_PKM,
    rail_factor_for_mix,
)
from src.impact.change_request import ChangeRequest  # noqa: E402
from src.impact.distance import flow_distance_km  # noqa: E402
from src.impact.elasticities import (  # noqa: E402
    ELASTICITIES,
    Direction,
    FlowType,
    TicketSegment,
    lookup_elasticity,
)
from src.impact.feed_paths import FeedPaths  # noqa: E402
from src.impact.odm import load_odm_index  # noqa: E402
from src.impact.report import compute_impact  # noqa: E402
from src.ingest.inspect import load_loc_meta  # noqa: E402

DATA = REPO_ROOT / "data"
REPORT_PATH = REPO_ROOT / "docs" / "demand-carbon-validation.md"
ORACLE_PATH = DATA / "carbon_oracle_template.json"
ODM_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "odm" / "mini_odm.csv"

# --- PRE-REGISTERED THRESHOLDS (do not tune) --------------------------------

# C1: arithmetic reproduction of published worked examples, tolerance on
# the relative error (published figures are rounded to 2 d.p.).
C1_TOL_PCT = 1.0

# C2(i): MAN->EUS public route mileage ~184 mi = 296 km; ±5%.
C2_DIST_REF_KM = 296.0
C2_DIST_TOL_PCT = 5.0
# C2(ii): "overwhelmingly" = at least 90% of corridor schedules.
C2_TRACTION_MIN_SHARE = 0.90
C2_ELECTRIC_CORRIDOR = ("MAN", "EUS")   # WCML — must be electric
C2_DIESEL_CORRIDOR = ("NRW", "SHM")     # Bittern line — DMU territory

# C3: per-corridor ratio ours/oracle. PASS band ±40% (flat-loading vs the
# calculator's per-service loadings); DEGRADE up to 2x; SCRAP if >2x wrong
# on at least this many corridors.
C3_PASS_RATIO = 1.4
C3_DEGRADE_RATIO = 2.0
C3_SCRAP_MIN_CORRIDORS = 2

# D1: worked examples must match hand-computed fixtures to 0.01 pp.
D1_TOL_PP = 0.01

# D3: demo change (~33% cut) predicted gross growth must land in
# [single digits .. low tens] percent; DEGRADE up to 3x the upper bound
# (ship direction + order of magnitude, strip the number); beyond = SCRAP.
D3_MIN_PCT = 1.0
D3_MAX_PCT = 30.0
D3_DEGRADE_MAX_PCT = 90.0

# ----------------------------------------------------------------------------

# National Rail carbon-calculator substantiation worked example (37.3 km):
# rail 1.32 kgCO2e, car 4.14 kgCO2e, saving 2.82 kgCO2e.
NR_EXAMPLE_KM = 37.3
NR_EXAMPLE_RAIL_KG = 1.32
NR_EXAMPLE_CAR_KG = 4.14
NR_EXAMPLE_SAVING_KG = 2.82

# D1 hand-computed fixtures (computed once by hand, NOT by this script):
#   (1.10)^-0.641 - 1 = -5.93%   Transport Scotland commuting, +10% rise
#   (0.90)^-0.144 - 1 = +1.53%   Transport Scotland commuting, -10% cut
#   (1.05)^-0.95  - 1 = -4.53%   PDFH LD-London non-season, +5% rise
D1_CASES = (
    ("TS commuting +10% rise", FlowType.NETWORK_LONDON, TicketSegment.SEASON,
     Direction.INCREASE, -0.641, 1.10, -5.93),
    ("TS commuting -10% cut", FlowType.NETWORK_LONDON, TicketSegment.SEASON,
     Direction.REDUCTION, -0.144, 0.90, +1.53),
    ("PDFH LD-London +5% rise", FlowType.LD_LONDON, TicketSegment.NON_SEASON,
     Direction.INCREASE, -0.95, 1.05, -4.53),
)

ORACLE_CORRIDORS = (
    {"name": "Manchester Piccadilly - London Euston (demo, WCML electric)",
     "origin_crs": "MAN", "dest_crs": "EUS"},
    {"name": "Norwich - Sheringham (regional diesel)",
     "origin_crs": "NRW", "dest_crs": "SHM"},
    {"name": "Woking - London Waterloo (short commuter)",
     "origin_crs": "WOK", "dest_crs": "WAT"},
    {"name": "Birmingham New Street - Edinburgh (long cross-country)",
     "origin_crs": "BHM", "dest_crs": "EDB"},
    {"name": "Brighton - London Victoria",
     "origin_crs": "BTN", "dest_crs": "VIC"},
    {"name": "Leeds - London Kings Cross",
     "origin_crs": "LDS", "dest_crs": "KGX"},
)


@dataclasses.dataclass
class Check:
    gate: str
    name: str
    ours: str
    reference: str
    ratio: str
    verdict: str      # PASS | FAIL | DEGRADE | SCRAP | SKIP | STOP
    detail: str = ""


def _pct_err(ours: float, ref: float) -> float:
    return abs(ours - ref) / abs(ref) * 100.0


# --- C1 ---------------------------------------------------------------------


def gate_c1() -> list[Check]:
    checks: list[Check] = []
    cases = (
        ("rail 37.3 km", NR_EXAMPLE_KM * RAIL_NATIONAL_KGCO2E_PER_PKM,
         NR_EXAMPLE_RAIL_KG),
        ("car 37.3 km", NR_EXAMPLE_KM * CAR_KGCO2E_PER_VKM / CAR_AVG_OCCUPANCY,
         NR_EXAMPLE_CAR_KG),
        ("saving 37.3 km",
         NR_EXAMPLE_KM * (CAR_KGCO2E_PER_VKM / CAR_AVG_OCCUPANCY
                          - RAIL_NATIONAL_KGCO2E_PER_PKM),
         NR_EXAMPLE_SAVING_KG),
    )
    for name, ours, ref in cases:
        err = _pct_err(ours, ref)
        checks.append(Check(
            "C1", f"NR calculator worked example — {name}",
            f"{ours:.4f} kg", f"{ref:.2f} kg", f"{err:.2f}% err",
            "PASS" if err <= C1_TOL_PCT else "FAIL"))
    # DEFRA blend identity: derived electric/diesel pair must reproduce the
    # national average at the national traction split.
    blend, _ = rail_factor_for_mix(0.70, 0.30, 0.0)
    err = _pct_err(blend, RAIL_NATIONAL_KGCO2E_PER_PKM)
    checks.append(Check(
        "C1", "DEFRA blend identity 0.70E+0.30D vs national avg",
        f"{blend:.5f}", f"{RAIL_NATIONAL_KGCO2E_PER_PKM:.5f}",
        f"{err:.2f}% err", "PASS" if err <= C1_TOL_PCT else "FAIL"))
    return checks


# --- C2 ---------------------------------------------------------------------


def gate_c2(fp: FeedPaths, msn: Path | None) -> list[Check]:
    checks: list[Check] = []
    dist = flow_distance_km("MAN", "EUS", rgd_path=fp.rgd, msn_path=msn)
    if dist is None:
        checks.append(Check("C2", "MAN->EUS distance", "None",
                            f"{C2_DIST_REF_KM} km", "-", "FAIL",
                            "no distance derivable at all"))
    else:
        err = _pct_err(dist.km, C2_DIST_REF_KM)
        checks.append(Check(
            "C2", f"MAN->EUS distance ({dist.method})",
            f"{dist.km} km", f"{C2_DIST_REF_KM} km ±{C2_DIST_TOL_PCT}%",
            f"{err:.1f}% err",
            "PASS" if err <= C2_DIST_TOL_PCT else "FAIL"))

    if fp.timetable_mca is None or not fp.timetable_mca.exists():
        checks.append(Check("C2", "traction mix", "no timetable", "-", "-",
                            "SKIP", "drop an RJTTF .MCA into data/"))
        return checks

    from src.ingest.timetable import load_timetable_index, traction_mix
    idx = load_timetable_index(fp.timetable_mca)

    o, d = C2_ELECTRIC_CORRIDOR
    mix = traction_mix(idx, o, d)
    verdict = "PASS" if mix.electric_pct >= C2_TRACTION_MIN_SHARE else "FAIL"
    if mix.diesel_pct > mix.electric_pct:
        verdict = "STOP"  # WCML reading diesel = parser bug, stop and fix
    checks.append(Check(
        "C2", f"{o}->{d} traction (WCML, expect electric)",
        f"{mix.electric_pct:.0%} electric / {mix.diesel_pct:.0%} diesel "
        f"({mix.train_count} trains)",
        f">={C2_TRACTION_MIN_SHARE:.0%} electric", "-", verdict))

    o, d = C2_DIESEL_CORRIDOR
    mix = traction_mix(idx, o, d)
    checks.append(Check(
        "C2", f"{o}->{d} traction (Bittern line, expect diesel)",
        f"{mix.diesel_pct:.0%} diesel / {mix.electric_pct:.0%} electric "
        f"({mix.train_count} trains)",
        f">={C2_TRACTION_MIN_SHARE:.0%} diesel", "-",
        "PASS" if mix.diesel_pct >= C2_TRACTION_MIN_SHARE else "FAIL"))
    return checks


# --- C3 ---------------------------------------------------------------------


def gate_c3(fp: FeedPaths, msn: Path | None) -> list[Check]:
    """Compare our per-passenger rail kgCO2e against hand-filled National
    Rail Carbon Calculator values. Generates the template on first run and
    SKIPs until the null values are filled in by hand."""
    if not ORACLE_PATH.exists():
        template = {
            "_instructions": (
                "Fill nr_rail_kg (and optionally nr_car_kg) per corridor from "
                "https://www.nationalrail.co.uk journey planner's CO2e figures "
                "for a single adult one-way journey, then re-run "
                "tools/validate_demand_carbon.py. null = not yet filled."),
            "corridors": [
                {**c, "nr_rail_kg": None, "nr_car_kg": None}
                for c in ORACLE_CORRIDORS
            ],
        }
        ORACLE_PATH.write_text(json.dumps(template, indent=2) + "\n",
                               encoding="utf-8")
        return [Check("C3", "oracle comparison", "-", "-", "-", "SKIP",
                      f"TODO: generated {ORACLE_PATH.name}; fill the "
                      "nr_rail_kg values by hand and re-run.")]

    doc = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
    rows = [c for c in doc.get("corridors", [])
            if c.get("nr_rail_kg") is not None]
    if not rows:
        return [Check("C3", "oracle comparison", "-", "-", "-", "SKIP",
                      f"TODO: {ORACLE_PATH.name} exists but all nr_rail_kg "
                      "are null — fill by hand and re-run.")]

    from src.ingest.timetable import load_timetable_index, traction_mix
    idx = (load_timetable_index(fp.timetable_mca)
           if fp.timetable_mca and fp.timetable_mca.exists() else None)

    checks: list[Check] = []
    badly_wrong = 0
    for c in rows:
        dist = flow_distance_km(c["origin_crs"], c["dest_crs"],
                                rgd_path=fp.rgd, msn_path=msn)
        if dist is None:
            checks.append(Check("C3", c["name"], "no distance",
                                f"{c['nr_rail_kg']} kg", "-", "FAIL"))
            badly_wrong += 1
            continue
        if idx is not None:
            mix = traction_mix(idx, c["origin_crs"], c["dest_crs"])
            factor, _ = (rail_factor_for_mix(mix.electric_pct, mix.diesel_pct,
                                             mix.unknown_pct)
                         if mix.train_count else
                         (RAIL_NATIONAL_KGCO2E_PER_PKM, ""))
        else:
            factor = RAIL_NATIONAL_KGCO2E_PER_PKM
        ours = dist.km * factor
        ref = float(c["nr_rail_kg"])
        ratio = ours / ref if ref else float("inf")
        worst = max(ratio, 1.0 / ratio) if ratio > 0 else float("inf")
        if worst <= C3_PASS_RATIO:
            verdict = "PASS"
        elif worst <= C3_DEGRADE_RATIO:
            verdict = "DEGRADE"
        else:
            verdict = "FAIL"
            badly_wrong += 1
        checks.append(Check(
            "C3", f"{c['name']} rail kg/passenger",
            f"{ours:.2f} kg ({dist.km} km, {dist.method})",
            f"{ref:.2f} kg (NR calculator)", f"x{ratio:.2f}", verdict,
            "diagnose: constant offset -> load factor; diesel-only -> "
            "traction; grows with distance -> mileage" if verdict != "PASS"
            else ""))
    if badly_wrong >= C3_SCRAP_MIN_CORRIDORS:
        checks.append(Check(
            "C3", "overall", f"{badly_wrong} corridors >2x wrong",
            f"< {C3_SCRAP_MIN_CORRIDORS}", "-", "SCRAP",
            "quantitative carbon output must be excluded"))
    return checks


# --- D1 ---------------------------------------------------------------------


def gate_d1() -> list[Check]:
    checks: list[Check] = []
    for name, ft, seg, direction, want_eps, ratio, want_pct in D1_CASES:
        eps = lookup_elasticity(ft, seg, direction)
        if abs(eps.value - want_eps) > 1e-9:
            checks.append(Check(
                "D1", f"{name} — elasticity cell", f"{eps.value}",
                f"{want_eps} (published)", "-", "FAIL",
                f"table cell drifted from the published value ({eps.source})"))
            continue
        got_pct = (ratio ** eps.value - 1.0) * 100.0
        err = abs(got_pct - want_pct)
        checks.append(Check(
            "D1", name, f"{got_pct:+.2f}%",
            f"{want_pct:+.2f}% (hand-computed fixture)",
            f"{err:.3f} pp err",
            "PASS" if err <= D1_TOL_PP else "FAIL"))
    return checks


# --- D2 ---------------------------------------------------------------------


def _demo_change(discount_pct: float) -> ChangeRequest:
    return ChangeRequest(
        kind="add_railcard", railcard_code="STU", discount_pct=discount_pct,
        discount_categories=("01",), corridor_origin_nlc="2968",
        corridor_dest_nlc="1444", peak_valid=True,
        description="validation probe")


def gate_d2(fp: FeedPaths) -> tuple[list[Check], object]:
    """Invariants. Any violation is a hard bug. Returns the demo demand
    block too so D3 doesn't recompute the corridor."""
    checks: list[Check] = []
    fp_fixture = dataclasses.replace(fp, odm_csv=ODM_FIXTURE)

    # (a) zero change -> zero shift. A literal 0% change is rejected at the
    # boundary (discount_pct must be strictly inside (0,1)) — itself part of
    # the invariant. To exercise the engine's ratio==1 path we use a tiny
    # discount whose rounding restores the original price on some rows;
    # those rows must show direction 'none' and 0% response.
    try:
        compute_impact(_demo_change(0.0), fp_fixture, include={"demand"})
        checks.append(Check(
            "D2", "zero change rejected at the boundary", "accepted",
            "ValueError", "-", "FAIL",
            "a 0% change should be rejected by ChangeRequest validation"))
    except ValueError as exc:
        checks.append(Check(
            "D2", "zero change rejected at the boundary", "ValueError",
            "ValueError", "-", "PASS", str(exc)))
    rep0 = compute_impact(_demo_change(0.0001), fp_fixture,
                          include={"demand"})
    assert rep0.demand is not None
    rows0 = rep0.demand.estimates
    unchanged = [e for e in rows0 if e.price_ratio == 1.0]
    violated = [e for e in unchanged
                if e.gross_demand_change_pct != 0.0 or e.direction != "none"]
    checks.append(Check(
        "D2", "unchanged price -> zero shift (0.01% probe, rounded back)",
        f"{len(unchanged)} unchanged rows, {len(violated)} violations",
        ">=1 unchanged row, 0 violations", "-",
        ("PASS" if unchanged and not violated
         else ("SKIP" if not unchanged else "FAIL")),
        "" if unchanged else
        "no row's rounding restored the original price; ratio==1 path "
        "not exercisable through the public boundary"))

    # (b) sign correctness, formula level, every cell in the table.
    bad_sign = [
        (ft, seg, d) for (ft, seg, d), e in ELASTICITIES.items()
        if (d == Direction.INCREASE and 1.10 ** e.value >= 1.0)
        or (d == Direction.REDUCTION and 0.90 ** e.value <= 1.0)
    ]
    checks.append(Check(
        "D2", "sign: rise loses demand, cut gains, all 16 cells",
        f"{len(bad_sign)} violations", "0", "-",
        "PASS" if not bad_sign else "FAIL", str(bad_sign) if bad_sign else ""))

    # (c) asymmetry: a 10% cut gains less than a 10% rise loses, per segment.
    bad_asym = []
    for ft in FlowType:
        for seg in TicketSegment:
            up = lookup_elasticity(ft, seg, Direction.INCREASE).value
            down = lookup_elasticity(ft, seg, Direction.REDUCTION).value
            loss = 1.0 - 1.10 ** up
            gain = 0.90 ** down - 1.0
            if not gain < loss:
                bad_asym.append((ft.value, seg.value))
    checks.append(Check(
        "D2", "asymmetry: -10% gain < +10% loss, all 8 segments",
        f"{len(bad_asym)} violations", "0", "-",
        "PASS" if not bad_asym else "FAIL", str(bad_asym) if bad_asym else ""))

    # (d,e,f) volume invariants + validity warning on the real demo change
    # with the fixture ODM.
    rep = compute_impact(_demo_change(1.0 / 3.0), fp_fixture,
                         include={"demand"})
    assert rep.demand is not None
    d = rep.demand
    vol_rows = [e for e in d.estimates if e.net_new_journeys is not None]
    bad_vol = [
        e.flow_id for e in vol_rows
        if e.abstracted_journeys > e.odm_journeys_per_period
        or e.net_new_journeys > e.gross_product_journeys
        or e.abstracted_journeys > e.eligible_base_journeys
    ]
    checks.append(Check(
        "D2", "abstraction <= existing volume AND net <= gross",
        f"{len(vol_rows)} volume rows, {len(bad_vol)} violations",
        "0 violations", "-",
        "PASS" if vol_rows and not bad_vol
        else ("SKIP" if not vol_rows else "FAIL"),
        "" if vol_rows else "fixture ODM matched no demo flow"))

    not_flagged = [e.flow_id for e in d.estimates
                   if abs(e.price_change_pct) > 25.0 and e.within_validity]
    checks.append(Check(
        "D2", "33% demo change trips the validity warning",
        f"{d.validity_warnings} warnings / {len(d.estimates)} rows, "
        f"{len(not_flagged)} unflagged >25% rows",
        "all >25% rows flagged", "-",
        "PASS" if d.validity_warnings and not not_flagged else "FAIL"))

    # (g) implied yield == ODM yield when the ODM carries revenue.
    with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8") as tmp:
        tmp.write("origin_nlc,dest_nlc,journeys_per_year,revenue_pence\n"
                  "2968,1444,5000,45000000\n")
        tmp_path = Path(tmp.name)
    try:
        loc = load_loc_meta(fp.loc)
        odm = load_odm_index(tmp_path, loc=loc)
        got = odm.yield_pence("2968", "1444")
        checks.append(Check(
            "D2", "implied yield == revenue/journeys",
            f"{got}p", "9000p (45000000/5000)", "-",
            "PASS" if got == 9000 else "FAIL"))
        rep_y = compute_impact(_demo_change(1.0 / 3.0),
                               dataclasses.replace(fp, odm_csv=tmp_path),
                               include={"demand"})
        assert rep_y.demand is not None
        yield_rows = [e for e in rep_y.demand.estimates
                      if e.yield_basis == "odm_yield"]
        bad_yield = [e.flow_id for e in yield_rows
                     if e.price_base_pence != 9000]
        checks.append(Check(
            "D2", "demand rows use the ODM yield as price base",
            f"{len(yield_rows)} odm_yield rows, {len(bad_yield)} wrong base",
            ">=1 row, 0 wrong", "-",
            "PASS" if yield_rows and not bad_yield else "FAIL"))
    finally:
        tmp_path.unlink(missing_ok=True)

    return checks, d


# --- D3 ---------------------------------------------------------------------


def gate_d3(demand_block) -> list[Check]:
    rows = [e for e in demand_block.estimates if e.direction == "reduction"]
    if not rows:
        return [Check("D3", "demo growth plausibility", "no reduction rows",
                      "-", "-", "FAIL",
                      "the demo 33% cut produced no reduction rows")]
    # Elasticity-implied band for this cut, from the reduction cells the
    # rows actually used (printed alongside, per the gate spec).
    implied = [(e.price_ratio ** e.elasticity - 1.0) * 100.0 for e in rows]
    band = f"elasticity-implied {min(implied):.1f}%..{max(implied):.1f}%"
    lo = min(e.gross_demand_change_pct for e in rows)
    hi = max(e.gross_demand_change_pct for e in rows)
    if D3_MIN_PCT <= lo and hi <= D3_MAX_PCT:
        verdict = "PASS"
    elif 0.0 < lo and hi <= D3_DEGRADE_MAX_PCT:
        verdict = "DEGRADE"
    else:
        verdict = "SCRAP"
    return [Check(
        "D3", "demo ~33% cut predicted gross growth",
        f"{lo:.1f}%..{hi:.1f}% across {len(rows)} rows ({band})",
        f"[{D3_MIN_PCT:.0f}%, {D3_MAX_PCT:.0f}%] PASS; "
        f"<= {D3_DEGRADE_MAX_PCT:.0f}% DEGRADE", "-", verdict,
        "DEGRADE ships direction + order of magnitude with the number "
        "stripped; SCRAP strips quantitative output" if verdict != "PASS"
        else "")]


# --- Report -----------------------------------------------------------------


def _module_verdict(checks: list[Check], gates: tuple[str, ...]) -> str:
    mine = [c for c in checks if c.gate in gates]
    verdicts = {c.verdict for c in mine}
    if "STOP" in verdicts:
        return "STOP — fix the flagged bug before shipping"
    if "SCRAP" in verdicts:
        return "SCRAP — exclude from DEFAULT_INCLUDE, note in PLAN.md"
    if "FAIL" in verdicts or "DEGRADE" in verdicts:
        return "DEGRADE — ship with the downgrade specified in the row(s) above"
    if all(c.verdict == "SKIP" for c in mine):
        return "SKIP — no gate could run"
    if "SKIP" in verdicts:
        return "PASS (with SKIPs) — ships with the EST label; skipped gates listed above"
    return "PASS — ships with the EST label"


def write_report(checks: list[Check]) -> None:
    lines = [
        "# Demand + carbon gate validation",
        "",
        "Generated by `python -m tools.validate_demand_carbon`. Thresholds are",
        "pre-registered at the top of that script and are not tuned to results.",
        "",
        "| Gate | Check | Ours | Reference | Ratio/err | Verdict |",
        "|---|---|---|---|---|---|",
    ]
    for c in checks:
        lines.append(
            f"| {c.gate} | {c.name} | {c.ours} | {c.reference} "
            f"| {c.ratio} | **{c.verdict}** |")
    lines.append("")
    details = [c for c in checks if c.detail]
    if details:
        lines.append("## Notes per check")
        lines.append("")
        for c in details:
            lines.append(f"- **{c.gate} / {c.name}**: {c.detail}")
        lines.append("")
    lines.append("## Module verdicts")
    lines.append("")
    lines.append(f"- **carbon** (C1, C2, C3): "
                 f"{_module_verdict(checks, ('C1', 'C2', 'C3'))}")
    lines.append(f"- **demand** (D1, D2, D3): "
                 f"{_module_verdict(checks, ('D1', 'D2', 'D3'))}")
    lines.append("")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    fp = FeedPaths.default_for_data_dir(DATA)
    missing = fp.missing()
    if missing:
        print(f"FEED MISSING — cannot validate: {[p.name for p in missing]}")
        return 2

    from src.api.geo import default_msn_path
    msn = default_msn_path(DATA)

    checks: list[Check] = []
    checks += gate_c1()
    checks += gate_c2(fp, msn)
    checks += gate_c3(fp, msn)
    checks += gate_d1()
    d2_checks, demo_demand = gate_d2(fp)
    checks += d2_checks
    checks += gate_d3(demo_demand)

    write_report(checks)
    for c in checks:
        print(f"[{c.verdict:7}] {c.gate}  {c.name}: {c.ours}  vs  {c.reference}")
    print(f"\nreport written to {REPORT_PATH}")

    verdicts = {c.verdict for c in checks}
    if verdicts & {"SCRAP", "STOP"}:
        return 2
    if verdicts & {"FAIL", "DEGRADE"}:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
