"""Unit tests for the corridor-callings ordering logic (feed-free, fast).

The union of intermediate calls must come back in calling order (mean
normalized position across serving trains), not alphabetical order — that
is the whole reason /api/corridor/callings exists alongside
`intermediate_calls`.
"""

from src.api.corridor import _ordered_callings
from src.ingest.timetable import CallingPoint, TrainSchedule


def _train(uid: str, *crs_codes: str) -> TrainSchedule:
    points = tuple(
        CallingPoint(tiploc=c, crs=c, is_origin=(i == 0),
                     is_terminus=(i == len(crs_codes) - 1))
        for i, c in enumerate(crs_codes)
    )
    return TrainSchedule(train_uid=uid, stp_indicator="P", train_status="P",
                         train_category="XX", power_type="E",
                         calling_points=points)


def test_orders_by_position_not_alphabet():
    # Alphabetical would give BBB, CCC; calling order is CCC then BBB.
    trains = [_train("T1", "AAA", "CCC", "BBB", "ZZZ")]
    got = _ordered_callings(trains, "AAA", "ZZZ")
    assert [crs for crs, _, _ in got] == ["CCC", "BBB"]


def test_mean_position_across_skip_stop_patterns():
    # Fast train skips MID; slow train calls everywhere. MID's position
    # comes only from the slow train; shared stops average across both.
    trains = [
        _train("FAST", "AAA", "PPP", "ZZZ"),
        _train("SLOW", "AAA", "PPP", "MID", "QQQ", "ZZZ"),
    ]
    got = _ordered_callings(trains, "AAA", "ZZZ")
    order = [crs for crs, _, _ in got]
    assert order == ["PPP", "MID", "QQQ"]
    counts = {crs: n for crs, _, n in got}
    assert counts == {"PPP": 2, "MID": 1, "QQQ": 1}


def test_endpoints_excluded_and_ties_deterministic():
    # Two stations at identical mean position tie-break by CRS.
    trains = [
        _train("T1", "AAA", "XXX", "ZZZ"),
        _train("T2", "AAA", "GGG", "ZZZ"),
    ]
    got = _ordered_callings(trains, "AAA", "ZZZ")
    assert [crs for crs, _, _ in got] == ["GGG", "XXX"]
    assert all(crs not in ("AAA", "ZZZ") for crs, _, _ in got)


def test_train_not_covering_slice_is_ignored():
    # T2 calls at AAA but never reaches ZZZ — contributes nothing.
    trains = [
        _train("T1", "AAA", "BBB", "ZZZ"),
        _train("T2", "AAA", "BBB"),
    ]
    got = _ordered_callings(trains, "AAA", "ZZZ")
    assert [(crs, n) for crs, _, n in got] == [("BBB", 1)]
