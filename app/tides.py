"""潮汐通航窗口核心计算（纯标准库，便于单测）。

规则：
- 实际水深(t) = 航道基准水深 + 潮高(t)；
- 相邻预报点之间潮高按线性插值；
- 只在预报覆盖时段 [首个预报点, 末个预报点] 内计算；
- 水深 >= 吃水 + 安全富余 的连续时段为达标窗口，端点恰好达标也纳入；
- 可出发时段 = [窗口起点, 窗口终点 − 通航时长]；窗口长度不足通航时长的舍弃；
- 预约水深复核：在排定通航区间内求水深低于所需水深的连续时段（相邻段合并，
  附各段最大欠深）；预报首末点未覆盖的通航时段单独返回，不得视为安全；
- 最大可通航吃水：在允许出发范围内求全程水深达标的最大非负吃水
  （可行性随吃水单调，二分求解；结果按 0.01 米向下取整）。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

_EPS = 1e-9  # 浮点容差，保证“恰好达标”的端点被纳入

ForecastPoint = tuple[datetime, float]  # (预报时刻, 潮高)


def _cross_time(t0: float, d0: float, t1: float, d1: float, required: float) -> float:
    """线性插值下水深恰好等于 required 的时刻（Unix 秒）。"""
    frac = (required - d0) / (d1 - d0)
    return t0 + frac * (t1 - t0)


def sufficient_depth_windows(
    points: list[ForecastPoint],
    reference_depth: float,
    required_depth: float,
) -> list[tuple[float, float]]:
    """返回水深达标的连续窗口列表 [(start_ts, end_ts)]（Unix 秒，按时间升序）。

    points 需按时刻升序且时刻唯一；不足 2 个预报点时无插值区间，返回空。
    """
    if len(points) < 2:
        return []

    ts = [p[0].timestamp() for p in points]
    depths = [reference_depth + p[1] for p in points]

    windows: list[tuple[float, float]] = []
    cur_start: float | None = None
    cur_end: float | None = None

    def flush() -> None:
        nonlocal cur_start, cur_end
        if cur_start is not None:
            windows.append((cur_start, cur_end))  # type: ignore[arg-type]
            cur_start = cur_end = None

    for i in range(len(points) - 1):
        t0, t1 = ts[i], ts[i + 1]
        d0, d1 = depths[i], depths[i + 1]
        above0 = d0 >= required_depth - _EPS
        above1 = d1 >= required_depth - _EPS

        if above0 and above1:
            seg = (t0, t1)
        elif not above0 and not above1:
            # 线性段两端都不达标，整段不达标；关闭当前窗口
            flush()
            continue
        elif above0:
            seg = (t0, _cross_time(t0, d0, t1, d1, required_depth))
        else:
            seg = (_cross_time(t0, d0, t1, d1, required_depth), t1)

        if cur_start is None:
            cur_start, cur_end = seg
        elif seg[0] <= cur_end + 1e-6:  # type: ignore[operator]
            # 相邻段首尾相接，合并为同一连续窗口
            cur_end = max(cur_end, seg[1])  # type: ignore[type-var]
        else:
            flush()
            cur_start, cur_end = seg

    flush()
    return windows


def departure_windows(
    points: list[ForecastPoint],
    reference_depth: float,
    required_depth: float,
    duration_seconds: float,
) -> tuple[list[tuple[datetime, datetime]], datetime | None]:
    """计算全部可出发时段与最早出发时刻。

    返回 ([(start, end), ...], earliest)，时间均为 UTC datetime。
    """
    result: list[tuple[datetime, datetime]] = []
    for start_ts, end_ts in sufficient_depth_windows(points, reference_depth, required_depth):
        if end_ts - start_ts >= duration_seconds - _EPS:
            result.append((_to_utc(start_ts), _to_utc(end_ts - duration_seconds)))
    earliest = min((w[0] for w in result), default=None)
    return result, earliest


def earliest_scheduled_departure(
    departable_windows: list[tuple[datetime, datetime]],
    duration_seconds: float,
    allowed_start: datetime,
    allowed_end: datetime,
    blockers: list[tuple[datetime, datetime]],
) -> datetime | None:
    """在潮汐可出发时段内、请求允许范围内，安排不与已有预约重叠的最早出发时刻。

    - departable_windows：departure_windows 求出的可出发时段 [s, e]
      （出发时刻 d ∈ [s, e] 即可保证整个通航过程 [d, d+时长] 水深达标）；
    - allowed_start/end：请求方允许的出发时刻范围（含端点）；
    - blockers：该港口已有有效预约的通航占用区间 [占用起点, 占用终点]；
      首尾相接允许（新区间端点恰好贴住已有区间不算重叠）。

    返回最早可出发时刻（UTC datetime）；无可用时刻返回 None。
    """
    range_lo, range_hi = allowed_start.timestamp(), allowed_end.timestamp()

    # 候选出发区间 = 潮汐可出发时段 ∩ 请求允许范围
    candidates: list[tuple[float, float]] = []
    for start, end in departable_windows:
        lo = max(start.timestamp(), range_lo)
        hi = min(end.timestamp(), range_hi)
        if lo <= hi:
            candidates.append((lo, hi))

    # 已有占用 [a, b] 会禁止 d ∈ (a-时长, b)：d == a-时长（首尾相接）
    # 与 d == b（紧随其后）均允许，因此从候选区间中挖掉开区间、保留两个端点。
    for occ_start, occ_end in sorted(blockers, key=lambda x: x[0].timestamp()):
        forbidden_lo = occ_start.timestamp() - duration_seconds
        forbidden_hi = occ_end.timestamp()
        remaining: list[tuple[float, float]] = []
        for lo, hi in candidates:
            if lo <= forbidden_lo:  # 左侧 [lo, forbidden_lo] 可出发（含相接端点）
                remaining.append((lo, min(hi, forbidden_lo)))
            if hi >= forbidden_hi:  # 右侧 [forbidden_hi, hi] 可出发（含相接端点）
                remaining.append((max(lo, forbidden_hi), hi))
        candidates = remaining
        if not candidates:
            return None

    if not candidates:
        return None
    return _to_utc(min(lo for lo, _ in candidates))


def max_navigable_draft(
    points: list[ForecastPoint],
    reference_depth: float,
    safety_margin: float,
    duration_seconds: float,
    allowed_start: datetime,
    allowed_end: datetime,
) -> tuple[float, datetime, datetime] | None:
    """求允许出发范围内、全程水深达标的最大非负吃水。

    吃水 d 可行 ⟺ 存在出发时刻 t ∈ [allowed_start, allowed_end]，使整个通航
    区间 [t, t+时长] 水深 >= d + safety_margin（沿用 departure_windows 的
    线性插值与“只在预报覆盖时段内求解”规则）。可行性随吃水单调递减，
    二分逼近真实最大值后按 0.01 米向下取整。

    返回 (max_draft, departure_start, departure_end)：
    - max_draft：最大非负吃水（米），按 0.01 米向下取整；
    - departure_start / departure_end：该吃水下最早的出发时刻与通航结束时刻
      （同一吃水有多个可行时刻时取最早）。
    允许范围内无可行的 0 米吃水（含预报未覆盖）时返回 None。
    """
    lo_ts = allowed_start.timestamp()
    hi_ts = allowed_end.timestamp()

    def feasible(draft: float) -> bool:
        windows, _ = departure_windows(
            points, reference_depth, draft + safety_margin, duration_seconds
        )
        # 可出发时段 [s, e] 与允许出发范围 [lo_ts, hi_ts] 有交集即可行
        return any(
            start.timestamp() <= hi_ts and end.timestamp() >= lo_ts
            for start, end in windows
        )

    if not feasible(0.0):
        return None

    # 上界：吃水不可能超过“覆盖时段内最高水深 − 安全富余”
    lo, hi = 0.0, max(reference_depth + h for _, h in points) - safety_margin
    for _ in range(60):  # 60 次二分后误差远小于 0.01 米
        mid = (lo + hi) / 2
        if feasible(mid):
            lo = mid
        else:
            hi = mid

    # 向下取整到 0.01 米：lo 可行但可能略低于相邻上一格，先试探上一格再定
    cents = max(0, math.floor(lo * 100 + _EPS))
    if feasible((cents + 1) / 100):
        cents += 1
    max_draft = cents / 100

    windows, _ = departure_windows(
        points, reference_depth, max_draft + safety_margin, duration_seconds
    )
    departure = earliest_scheduled_departure(
        windows, duration_seconds, allowed_start, allowed_end, blockers=[]
    )
    if departure is None:  # 理论上不可达（max_draft 已验证可行），防御性处理
        return None
    return max_draft, departure, departure + timedelta(seconds=duration_seconds)


def depth_deficit_audit(
    points: list[ForecastPoint],
    reference_depth: float,
    required_depth: float,
    start: datetime,
    end: datetime,
) -> tuple[list[tuple[datetime, datetime, float]], list[tuple[datetime, datetime]]]:
    """复核通航区间 [start, end] 的水深（按最新预报相邻点线性插值）。

    返回 (deficits, uncovered)：
    - deficits：通航区间内水深低于 required_depth 的连续时段
      [(起点, 终点, 最大欠深)]，跨预报点的连续欠深已合并，
      最大欠深 = required_depth − 时段内最低水深；
    - uncovered：预报首末点未覆盖的通航时段 [(起点, 终点)]，
      这些时段水深未知，调用方不得据此判为安全。
    """
    start_ts = start.timestamp()
    end_ts = end.timestamp()

    if len(points) < 2:
        # 无插值区间：整个通航区间都未被预报覆盖
        uncovered = [(start, end)] if start_ts < end_ts else []
        return [], uncovered

    ts = [p[0].timestamp() for p in points]
    depths = [reference_depth + p[1] for p in points]
    first_ts, last_ts = ts[0], ts[-1]

    uncovered_ts: list[tuple[float, float]] = []
    if start_ts < min(end_ts, first_ts):  # 通航起点早于首个预报点
        uncovered_ts.append((start_ts, min(end_ts, first_ts)))
    if max(start_ts, last_ts) < end_ts:  # 通航终点晚于末个预报点
        uncovered_ts.append((max(start_ts, last_ts), end_ts))

    lo = max(start_ts, first_ts)
    hi = min(end_ts, last_ts)

    deficits_ts: list[tuple[float, float, float]] = []
    cur_start: float | None = None
    cur_end: float | None = None
    cur_max = 0.0

    def flush() -> None:
        nonlocal cur_start, cur_end, cur_max
        if cur_start is not None:
            deficits_ts.append((cur_start, cur_end, round(cur_max, 6)))  # type: ignore[arg-type]
            cur_start = cur_end = None
            cur_max = 0.0

    for i in range(len(points) - 1):
        t0, t1 = ts[i], ts[i + 1]
        a, b = max(t0, lo), min(t1, hi)  # 插值段与通航区间（覆盖范围内）的交集
        if a >= b:
            continue
        # 线性插值出交集端点处的水深
        d0 = depths[i] + (depths[i + 1] - depths[i]) * (a - t0) / (t1 - t0)
        d1 = depths[i] + (depths[i + 1] - depths[i]) * (b - t0) / (t1 - t0)
        below0 = d0 < required_depth - _EPS
        below1 = d1 < required_depth - _EPS

        if not below0 and not below1:
            # 线性段两端都不欠深，整段达标；关闭当前欠深时段
            flush()
            continue
        if below0 and below1:
            seg = (a, b)
            seg_max = required_depth - min(d0, d1)
        elif below0:
            seg = (a, _cross_time(a, d0, b, d1, required_depth))
            seg_max = required_depth - d0
        else:
            seg = (_cross_time(a, d0, b, d1, required_depth), b)
            seg_max = required_depth - d1

        if cur_start is None:
            cur_start, cur_end, cur_max = seg[0], seg[1], seg_max
        elif seg[0] <= cur_end + 1e-6:  # type: ignore[operator]
            # 相邻段首尾相接（如共享的预报点同样欠深），合并为同一连续欠深时段
            cur_end = max(cur_end, seg[1])  # type: ignore[type-var]
            cur_max = max(cur_max, seg_max)
        else:
            flush()
            cur_start, cur_end, cur_max = seg[0], seg[1], seg_max

    flush()

    deficits = [(_to_utc(s), _to_utc(e), m) for s, e, m in deficits_ts]
    uncovered = [(_to_utc(s), _to_utc(e)) for s, e in uncovered_ts]
    return deficits, uncovered


def _to_utc(ts: float) -> datetime:
    """Unix 秒转 UTC datetime，四舍五入到整秒避免浮点噪声。"""
    return datetime.fromtimestamp(round(ts), tz=timezone.utc)
