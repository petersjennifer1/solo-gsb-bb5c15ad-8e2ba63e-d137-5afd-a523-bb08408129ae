"""核心窗口计算算法的单元测试（仅标准库）。"""
from datetime import datetime, timedelta, timezone

from app.tides import (
    departure_windows,
    depth_deficit_audit,
    earliest_scheduled_departure,
    sufficient_depth_windows,
)

BASE = datetime(2026, 9, 24, tzinfo=timezone.utc)


def pts(*heights: float, step_minutes: int = 60):
    """从 BASE 起按等间隔生成 (时刻, 潮高) 序列。"""
    return [
        (BASE + timedelta(minutes=step_minutes * i), h) for i, h in enumerate(heights)
    ]


def test_single_tide_cycle_window():
    # 基准水深 3，潮高 1→3→1：水深 4→6→4，要求 5
    # 上升段 00:30 达标，下降段 01:30 退出
    windows = sufficient_depth_windows(pts(1, 3, 1), reference_depth=3.0, required_depth=5.0)
    assert len(windows) == 1
    start, end = windows[0]
    assert start == (BASE + timedelta(minutes=30)).timestamp()
    assert end == (BASE + timedelta(hours=1, minutes=30)).timestamp()


def test_exact_threshold_endpoints_included():
    # 水深恰好全程等于要求值，端点恰好达标也纳入
    windows = sufficient_depth_windows(pts(2, 2), reference_depth=3.0, required_depth=5.0)
    assert windows == [(BASE.timestamp(), (BASE + timedelta(hours=1)).timestamp())]


def test_departure_window_subtracts_duration():
    # 达标窗口 [00:30, 01:30]，通航 30 分钟 → 可出发 [00:30, 01:00]
    deps, earliest = departure_windows(
        pts(1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=1800
    )
    assert len(deps) == 1
    assert deps[0][0] == BASE + timedelta(minutes=30)
    assert deps[0][1] == BASE + timedelta(hours=1)
    assert earliest == BASE + timedelta(minutes=30)


def test_window_shorter_than_duration_discarded():
    # 达标窗口 60 分钟 < 所需 90 分钟 → 舍弃
    deps, earliest = departure_windows(
        pts(1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=5400
    )
    assert deps == []
    assert earliest is None


def test_duration_exactly_equals_window_kept():
    # 窗口 60 分钟 == 所需 60 分钟 → 保留，可出发时段退化为同一时刻
    deps, earliest = departure_windows(
        pts(1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=3600
    )
    assert len(deps) == 1
    assert deps[0][0] == deps[0][1] == BASE + timedelta(minutes=30)
    assert earliest == BASE + timedelta(minutes=30)


def test_multiple_windows():
    # 潮高 1,3,1,3,1（基准 3，要求 5）：两个独立高潮窗口 [00:30,01:30]、[02:30,03:30]
    deps, earliest = departure_windows(
        pts(1, 3, 1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=60
    )
    assert len(deps) == 2
    # 第一个窗口 [00:30, 01:30]，可出发 [00:30, 01:29]
    assert deps[0][0] == BASE + timedelta(minutes=30)
    assert deps[0][1] == BASE + timedelta(hours=1, minutes=29)
    # 第二个窗口 [02:30, 03:30]
    assert deps[1][0] == BASE + timedelta(hours=2, minutes=30)
    assert deps[1][1] == BASE + timedelta(hours=3, minutes=29)
    assert earliest == deps[0][0]


def test_all_below_required():
    deps, earliest = departure_windows(
        pts(0.5, 1.0, 0.5), reference_depth=3.0, required_depth=5.0, duration_seconds=60
    )
    assert deps == []
    assert earliest is None


def test_fewer_than_two_points_no_coverage():
    deps, earliest = departure_windows(
        pts(5.0), reference_depth=3.0, required_depth=5.0, duration_seconds=60
    )
    assert deps == []
    assert earliest is None


def test_window_clipped_at_forecast_coverage():
    # 全程达标时窗口就是预报覆盖时段本身，不外推
    windows = sufficient_depth_windows(pts(3, 4, 3), reference_depth=3.0, required_depth=5.0)
    assert windows == [(BASE.timestamp(), (BASE + timedelta(hours=2)).timestamp())]


# ---------------------------------------------------------------------------
# earliest_scheduled_departure：潮汐窗口 + 请求范围 + 已有预约排程
# ---------------------------------------------------------------------------

# 一个 [00:00, 04:00] 的可出发时段（出发时刻落在此区间内即可）
DEP_WINDOWS = [(BASE, BASE + timedelta(hours=4))]
DURATION = 3600  # 60 分钟


def test_schedule_earliest_within_range():
    # 无占用、范围覆盖整个窗口 → 取范围起点（最早）
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=3), [],
    )
    assert dep == BASE


def test_schedule_intersects_range_start():
    # 允许范围从 01:00 开始 → 01:00 出发
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE + timedelta(hours=1), BASE + timedelta(hours=3), [],
    )
    assert dep == BASE + timedelta(hours=1)


def test_schedule_blocker_pushes_after_end():
    # 已有预约占用 [00:30, 01:30] → d 必须 >= 01:30（首尾相接允许）
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=4),
        [(BASE + timedelta(minutes=30), BASE + timedelta(hours=1, minutes=30))],
    )
    assert dep == BASE + timedelta(hours=1, minutes=30)


def test_schedule_departure_exactly_touching_blocker_start_allowed():
    # 预约占用 [01:00, 02:00]：00:00 出发、01:00 结束，首尾相接 → 允许
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=4),
        [(BASE + timedelta(hours=1), BASE + timedelta(hours=2))],
    )
    assert dep == BASE


def test_schedule_blocker_covering_range_start_takes_right_edge():
    # 占用 [00:00, 01:00]：00:00 出发会在 [00:00,01:00] 内重叠 → 最早 01:00
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=4),
        [(BASE, BASE + timedelta(hours=1))],
    )
    assert dep == BASE + timedelta(hours=1)


def test_schedule_no_room_between_blockers():
    # 两个相邻占用 [00:00,01:00]、[01:30,02:30]：
    # 间隙 01:00 出发需通航到 02:00，与第二个预约重叠 → 只能 02:30
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=4),
        [
            (BASE, BASE + timedelta(hours=1)),
            (BASE + timedelta(hours=1, minutes=30), BASE + timedelta(hours=2, minutes=30)),
        ],
    )
    assert dep == BASE + timedelta(hours=2, minutes=30)


def test_schedule_exact_fit_between_blockers():
    # 占用 [00:00,01:00]、[02:00,03:00]：间隙恰好 1 小时，01:00 出发 02:00 结束
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=4),
        [
            (BASE, BASE + timedelta(hours=1)),
            (BASE + timedelta(hours=2), BASE + timedelta(hours=3)),
        ],
    )
    assert dep == BASE + timedelta(hours=1)


def test_schedule_range_fully_blocked_returns_none():
    # 占用 [00:00, 03:30]，允许范围 00:00–02:30 内无可行出发
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=2, minutes=30),
        [(BASE, BASE + timedelta(hours=3, minutes=30))],
    )
    assert dep is None


def test_schedule_range_outside_tide_windows_returns_none():
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE + timedelta(hours=5), BASE + timedelta(hours=6), [],
    )
    assert dep is None


def test_schedule_range_touching_tide_window_endpoint_allowed():
    # 可出发时段终点 04:00，允许范围末端恰好 04:00 → 04:00 出发（端点纳入）
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE + timedelta(hours=4), BASE + timedelta(hours=4),
        [],
    )
    assert dep == BASE + timedelta(hours=4)


# ---------------------------------------------------------------------------
# depth_deficit_audit：预约通航区间水深复核
# ---------------------------------------------------------------------------


def test_audit_single_deficit_segment_with_max_deficit():
    # 基准 3，潮高 3→1→3：水深 6→4→6，要求 5；00:30 跌破，01:30 回升
    deficits, uncovered = depth_deficit_audit(
        pts(3, 1, 3), reference_depth=3.0, required_depth=5.0,
        start=BASE, end=BASE + timedelta(hours=2),
    )
    assert uncovered == []
    assert len(deficits) == 1
    start, end, max_deficit = deficits[0]
    assert start == BASE + timedelta(minutes=30)
    assert end == BASE + timedelta(hours=1, minutes=30)
    assert max_deficit == 1.0  # 最低水深 4.0（01:00），欠深 5 − 4 = 1


def test_audit_merges_deficit_across_forecast_points():
    # 水深 6→4.5→4→6：欠深段在欠深的预报点处相连，合并为一段，最大欠深取全段最低
    deficits, uncovered = depth_deficit_audit(
        pts(3, 1.5, 1, 3), reference_depth=3.0, required_depth=5.0,
        start=BASE, end=BASE + timedelta(hours=3),
    )
    assert uncovered == []
    assert len(deficits) == 1
    start, end, max_deficit = deficits[0]
    assert start == BASE + timedelta(minutes=40)   # 6→4.5 段在 00:40 跌破 5
    assert end == BASE + timedelta(hours=2, minutes=30)  # 4→6 段在 02:30 回升到 5
    assert max_deficit == 1.0


def test_audit_two_separate_deficit_segments():
    # 潮高 3,1,3,1,3（基准 3，要求 5）：两个独立低谷，各自成段
    deficits, _ = depth_deficit_audit(
        pts(3, 1, 3, 1, 3), reference_depth=3.0, required_depth=5.0,
        start=BASE, end=BASE + timedelta(hours=4),
    )
    assert len(deficits) == 2
    assert deficits[0][0] == BASE + timedelta(minutes=30)
    assert deficits[0][1] == BASE + timedelta(hours=1, minutes=30)
    assert deficits[1][0] == BASE + timedelta(hours=2, minutes=30)
    assert deficits[1][1] == BASE + timedelta(hours=3, minutes=30)
    assert [m for _, _, m in deficits] == [1.0, 1.0]


def test_audit_exact_threshold_is_not_deficit():
    # 水深在 01:00 恰好触及所需水深 5.0，不算欠深
    deficits, uncovered = depth_deficit_audit(
        pts(3, 2, 3), reference_depth=3.0, required_depth=5.0,
        start=BASE, end=BASE + timedelta(hours=2),
    )
    assert deficits == []
    assert uncovered == []


def test_audit_clips_to_transit_interval():
    # 只在通航区间内复核：通航 00:15–01:00，水深 4.5→6，00:30 起达标
    deficits, uncovered = depth_deficit_audit(
        pts(1, 3), reference_depth=3.0, required_depth=5.0,
        start=BASE + timedelta(minutes=15), end=BASE + timedelta(hours=1),
    )
    assert uncovered == []
    assert len(deficits) == 1
    start, end, max_deficit = deficits[0]
    assert start == BASE + timedelta(minutes=15)
    assert end == BASE + timedelta(minutes=30)
    assert max_deficit == 0.5


def test_audit_forecast_not_covering_transit_head_and_tail():
    # 预报只覆盖 [01:00, 02:00]，通航 [00:00, 03:00]：首尾各一段未覆盖
    points = [
        (BASE + timedelta(hours=1), 3.0),
        (BASE + timedelta(hours=2), 3.0),
    ]
    deficits, uncovered = depth_deficit_audit(
        points, reference_depth=3.0, required_depth=5.0,
        start=BASE, end=BASE + timedelta(hours=3),
    )
    assert deficits == []  # 覆盖段内水深 6.0 达标
    assert uncovered == [
        (BASE, BASE + timedelta(hours=1)),
        (BASE + timedelta(hours=2), BASE + timedelta(hours=3)),
    ]


def test_audit_without_forecast_points_all_uncovered():
    start, end = BASE, BASE + timedelta(hours=1)
    for points in ([], [(BASE, 5.0)]):  # 无预报点 / 单点无插值区间
        deficits, uncovered = depth_deficit_audit(
            points, reference_depth=3.0, required_depth=5.0, start=start, end=end,
        )
        assert deficits == []
        assert uncovered == [(start, end)]


def test_audit_deficit_stops_at_forecast_coverage_edge():
    # 通航 [00:00, 02:00]，预报只覆盖 [00:00, 01:00] 且全程水深不足：
    # 欠深段到预报末点为止，其余时段按未覆盖返回（不得视为安全）
    points = [(BASE, 1.0), (BASE + timedelta(hours=1), 1.0)]
    deficits, uncovered = depth_deficit_audit(
        points, reference_depth=3.0, required_depth=5.0,
        start=BASE, end=BASE + timedelta(hours=2),
    )
    assert deficits == [(BASE, BASE + timedelta(hours=1), 1.0)]
    assert uncovered == [(BASE + timedelta(hours=1), BASE + timedelta(hours=2))]
