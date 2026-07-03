"""Hand-verifiable parser tests for the railcard-discount feed files.

For each new parser (.RLC, .DIS, .RCM, .FRR, plus the .TTY DISCOUNT_CATEGORY
position fix), pick a specific row from the real RJFAF805 feed and assert the
parsed fields match what RSPS5045 §4.6/§4.15/§4.16/§4.17/§4.18 says they
should be. A reviewer can open the spec PDF and the feed file side-by-side
and reproduce these checks by hand — that's the bar.

Fast tests: these files are small (KB-MB), no FFL scan involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ingest.inspect import (
    load_frr_rules,
    load_railcards,
    load_rcm_min_fares,
    load_status_discounts,
    load_ticket_discount_categories,
    load_toc_meta,
    parse_dis_discount,
    parse_dis_status,
    parse_frr,
    parse_rcm,
    parse_rlc,
    parse_tty,
    raw_feed_line,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"
RLC = DATA / "RJFAF805.RLC"
DIS = DATA / "RJFAF805.DIS"
RCM = DATA / "RJFAF805.RCM"
FRR = DATA / "RJFAF805.FRR"
TTY = DATA / "RJFAF805.TTY"


def _require(p: Path) -> None:
    if not p.exists():
        pytest.skip(f"missing feed file: {p}")


# --- Parsers: offsets per RSPS5045 -----------------------------------------


def test_parse_rlc_yng_adult_status() -> None:
    """The YNG (16-25 Railcard) record yields ADULT_STATUS='003' at pos 119-121.
    That's the link the resolver follows into .DIS — if this offset is wrong,
    every YNG fare is wrong."""
    # One canonical YNG row (constant across snapshots; current-day version).
    line = (
        "YNG010920250109202515072025A16-25 RAILCARD      "
        "YNYNYNGY00100100100100000000100100000000003500000000001200        YZMA003XXXXXX"
    )
    rec = parse_rlc(line)
    assert rec["RAILCARD_CODE"] == "YNG"
    assert rec["DESCRIPTION"] == "16-25 RAILCARD      "
    assert rec["ADULT_STATUS"] == "003"
    assert rec["CHILD_STATUS"] == "XXX"
    assert rec["AAA_STATUS"] == "XXX"
    assert rec["PRICE"] == "00003500"  # £35
    assert rec["MIN_PASSENGERS"] == "001"
    assert rec["MAX_PASSENGERS"] == "001"


def test_parse_dis_discount_yng_status_003_cat_01() -> None:
    """For STATUS=003 (YNG adult) CAT=01, the DIS D-record gives
    DISCOUNT_INDICATOR='0' DISCOUNT_PERCENTAGE=334 — the famous "1/3 off"
    is implemented as 33.4% in the feed."""
    line = "D00331122999010334"
    rec = parse_dis_discount(line)
    assert rec["RECORD_TYPE"] == "D"
    assert rec["STATUS_CODE"] == "003"
    assert rec["DISCOUNT_CATEGORY"] == "01"
    assert rec["DISCOUNT_INDICATOR"] == "0"
    assert rec["DISCOUNT_PERCENTAGE"] == "334"


def test_parse_dis_status_s_record() -> None:
    """A Status (S) record's fixed positions stay readable — the resolver
    will start using these flat/min fields when the 'F'/'M'/'H'/'L' indicators
    are wired in."""
    # Canonical 'ADULT' status from sample data.
    line = "S0003112299922102014ADULT     00000000000000000000000000000000000000000000000000000000000000000YYYY"
    rec = parse_dis_status(line)
    assert rec["RECORD_TYPE"] == "S"
    assert rec["STATUS_CODE"] == "000"
    assert rec["ATB_DESC"] == "ADULT"
    assert rec["FS_MKR"] == "Y"
    assert rec["SR_MKR"] == "Y"


def test_parse_rcm_two_together_min_fare() -> None:
    """A Two Together (2TR) min-fare row parses with the correct ticket code
    and 8-digit pence amount."""
    line = "2TR0CA311229990203202500002360"
    rec = parse_rcm(line)
    assert rec["RAILCARD_CODE"] == "2TR"
    assert rec["TICKET_CODE"] == "0CA"
    assert rec["MINIMUM_FARE"] == "00002360"  # £23.60


def test_parse_frr_rule_01_5p_band() -> None:
    """Rule 01 index 09 is the rounding band that applies to ordinary
    railcard-discounted fares: any amount up to £999,999.97 rounds UP to 5p."""
    line = "013112299909310520179999999700000005"
    rec = parse_frr(line)
    assert rec["RULE_NO"] == "01"
    assert rec["RULE_INDEX"] == "09"
    assert rec["MAX_AMOUNT"] == "99999997"
    assert rec["ROUND_AMOUNT"] == "00000005"


def test_parse_tty_sor_discount_category_at_pos_112() -> None:
    """The TTY DISCOUNT_CATEGORY at pos 112-113 is the link into the .DIS
    table. Earlier versions of this parser had it at pos 99 — leaving it
    there would route every discount lookup to the wrong status row.
    This test guards the fix against silent regression."""
    # Use a known SOR row layout from the snapshot; only the offsets matter.
    # Full SOR record padded to 113 chars from the feed snapshot:
    line = (
        "RSOR311229992305201722052017ANYTIME R      2RS31122999001001001000001000NNN41"
        + " " * 20  # ATB_DESCRIPTION padding (pos 78-97)
        + "1"      # LUL_XLONDON_ISSUE (pos 98)
        + "N"      # RESERVATION_REQUIRED (pos 99)
        + "   "    # CAPRI_CODE (pos 100-102)
        + "N"      # LUL_93 (pos 103)
        + "00"     # UTS_CODE (pos 104-105)
        + "0"      # TIME_RESTRICTION (pos 106)
        + " "      # FREE_PASS_LUL (pos 107)
        + "N"      # PACKAGE_MKR (pos 108)
        + "000"    # FARE_MULTIPLIER (pos 109-111)
        + "01"     # DISCOUNT_CATEGORY (pos 112-113)  <- the field we care about
    )
    assert len(line) >= 113, f"test fixture too short: {len(line)}"
    rec = parse_tty(line)
    assert rec["TICKET_CODE"] == "SOR"
    assert rec["DISCOUNT_CATEGORY"] == "01"


# --- Loaders: real-feed end-to-end (still fast; files are small) -----------


def test_load_railcards_yng_lookup() -> None:
    """The .RLC loader gives YNG with ADULT_STATUS=003 from a real feed row."""
    _require(RLC)
    by_code = load_railcards(RLC)
    yng = by_code.get("YNG")
    assert yng is not None
    assert yng.adult_status == "003"
    assert yng.line_no > 0
    assert "16-25" in yng.description


def test_load_status_discounts_yng_path() -> None:
    """(status=003, category=01) — the cell YNG/SOR looks up — exists and
    gives the expected indicator + percentage."""
    _require(DIS)
    by_key = load_status_discounts(DIS)
    dis = by_key.get(("003", "01"))
    assert dis is not None
    assert dis.discount_indicator == "0"
    assert dis.discount_percentage == 334


def test_load_rcm_yng_sor_min_fare() -> None:
    """(YNG, SOR) → £12 minimum fare. Confirms the loader picks the
    current-day row (latest START_DATE) when multiple history rows exist."""
    _require(RCM)
    by_key = load_rcm_min_fares(RCM)
    rcm = by_key.get(("YNG", "SOR"))
    assert rcm is not None
    assert rcm.minimum_fare_pence == 1200


def test_load_frr_rule_01_has_5p_band() -> None:
    """Rule 01 (the default railcard rounding rule) contains an ascending
    series of bands, the catch-all of which rounds to 5p for ordinary
    fare values."""
    _require(FRR)
    by_rule = load_frr_rules(FRR)
    bands = by_rule.get("01")
    assert bands is not None and len(bands) > 0
    # Find the band that catches a typical fare (e.g. £100 = 10000p).
    band = next(b for b in bands if 10000 <= b.max_amount_pence)
    assert band.round_amount_pence == 5


def test_load_ticket_discount_categories_sor() -> None:
    """The TTY loader exposes (line_no, DISCOUNT_CATEGORY) per ticket and
    correctly reads SOR's category from pos 112-113."""
    _require(TTY)
    by_code = load_ticket_discount_categories(TTY)
    entry = by_code.get("SOR")
    assert entry is not None
    line_no, cat = entry
    assert line_no > 0
    assert cat == "01"


# --- .TOC operator names (operator-scope picker) ----------------------------


def test_load_toc_meta_northern_from_real_feed() -> None:
    """The NTH F-row (line 39 of RJFAF805.TOC: 'FNTHNTNORTHERN...') yields
    fare-TOC 'NTH', timetable id 'NT', name 'NORTHERN'. This is the join the
    /api/tocs picker uses; a T-row ('TGNTHAMESLINK...') must NOT be parsed."""
    toc_path = DATA / "RJFAF805.TOC"
    _require(toc_path)
    by_code = load_toc_meta(toc_path)
    rec = by_code.get("NTH")
    assert rec is not None
    assert rec.fare_toc == "NTH"
    assert rec.toc_2char == "NT"
    assert rec.name == "NORTHERN"
    assert rec.line_no == 39
    # 'GNT' appears only inside a T-row ("TGNT...") — never as an F-row code.
    assert "GNT" not in by_code


def test_load_toc_meta_inline(tmp_path: Path) -> None:
    """Offsets per the .TOC layout: F at pos 1, code pos 2-4, 2-char pos 5-6,
    name pos 7-36. Comment and T-rows are skipped."""
    p = tmp_path / "X.TOC"
    p.write_text(
        "/!! Start of file\n"
        "FNTHNTNORTHERN                      \n"
        "TGNTHAMESLINK AND GT NORTHERN GN         Y\n",
        encoding="latin-1",
    )
    by_code = load_toc_meta(p)
    assert set(by_code) == {"NTH"}
    assert by_code["NTH"].toc_2char == "NT"
    assert by_code["NTH"].name == "NORTHERN"


# --- raw_feed_line: sparse-offset random access ------------------------------


def test_raw_feed_line_checkpoint_math(tmp_path: Path) -> None:
    """Lines straddling the 10,000-line checkpoint stride must resolve
    exactly: the reader seeks to offsets[k] (line k*stride+1) then scans
    forward. 25,001 lines exercises three checkpoints plus a tail."""
    p = tmp_path / "big.FFL"
    n = 25_001
    p.write_text("".join(f"L{i:07d}\n" for i in range(1, n + 1)), encoding="latin-1")
    for line_no in (1, 2, 9_999, 10_000, 10_001, 20_000, 20_001, n):
        assert raw_feed_line(p, line_no) == f"L{line_no:07d}", line_no
    assert raw_feed_line(p, 0) is None
    assert raw_feed_line(p, n + 1) is None
    assert raw_feed_line(p, 10 * n) is None
