"""港口潮汐通航窗口与单向航道预约 API。

- 录入港口（航道基准水深）与潮高预报点；
- 相邻预报点线性插值，在预报覆盖时段内求水深 >= 吃水 + 安全富余的连续窗口；
- 窗口结束时刻减去通航时长得到可出发时段，不足时长的窗口舍弃；
- 查询窗口时实时读取最新预报数据计算，预报点更正后立即生效；
- 单向航道预约：在请求的允许出发范围内，按最新预报安排最早出发时刻，
  整个通航过程不与该港口有效预约重叠（首尾相接允许）；无可用时刻返回 409；
  已落库预约记录确定时刻，预报更正不追溯调整/取消已有预约。
- 预约水深复核：按查询时最新预报复算预约排定通航区间的水深，合并返回
  欠深时段及各段最大欠深；预报未覆盖整个通航区间时明确返回未覆盖时段，
  不判为安全；复核只读，不改动预约或原排程。
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException
from sqlite3 import Connection

from .database import get_db, init_db, locked_reservation_tx, port_lock
from .schemas import (
    DepartureWindow,
    DepthAuditDeficit,
    DepthAuditResult,
    DepthAuditUncovered,
    PortCreate,
    PortOut,
    PortUpdate,
    ReservationCreate,
    ReservationOut,
    TideForecastOut,
    TideForecastUpsert,
    WindowQuery,
    WindowQueryResult,
)
from .tides import departure_windows, depth_deficit_audit, earliest_scheduled_departure

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # 启动时初始化数据库（幂等）
    yield


app = FastAPI(
    title="港口潮汐通航窗口 API",
    version="1.1.0",
    lifespan=lifespan,
)


def _normalize(dt: datetime) -> datetime:
    """无时区信息按 UTC 处理，其余统一换算为 UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _get_port_or_404(db: Connection, port_id: int):
    row = db.execute(
        "SELECT id, name, reference_depth FROM ports WHERE id = ?", (port_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"港口 {port_id} 不存在")
    return row


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/ports", response_model=PortOut, status_code=201)
def create_port(payload: PortCreate, db: Connection = Depends(get_db)):
    """录入港口：名称 + 航道基准水深。"""
    cur = db.execute(
        "INSERT INTO ports (name, reference_depth) VALUES (?, ?)",
        (payload.name, payload.reference_depth),
    )
    return PortOut(id=cur.lastrowid, name=payload.name, reference_depth=payload.reference_depth)


@app.get("/ports", response_model=list[PortOut])
def list_ports(db: Connection = Depends(get_db)):
    rows = db.execute("SELECT id, name, reference_depth FROM ports ORDER BY id").fetchall()
    return [PortOut(**dict(r)) for r in rows]


@app.get("/ports/{port_id}", response_model=PortOut)
def get_port(port_id: int, db: Connection = Depends(get_db)):
    return PortOut(**dict(_get_port_or_404(db, port_id)))


@app.patch("/ports/{port_id}", response_model=PortOut)
def update_port(port_id: int, payload: PortUpdate, db: Connection = Depends(get_db)):
    """更正港口名称或航道基准水深。"""
    row = _get_port_or_404(db, port_id)
    name = payload.name if payload.name is not None else row["name"]
    depth = payload.reference_depth if payload.reference_depth is not None else row["reference_depth"]
    db.execute(
        "UPDATE ports SET name = ?, reference_depth = ? WHERE id = ?",
        (name, depth, port_id),
    )
    return PortOut(id=port_id, name=name, reference_depth=depth)


@app.put("/ports/{port_id}/tide-forecasts", response_model=list[TideForecastOut])
def upsert_tide_forecasts(
    port_id: int, payload: TideForecastUpsert, db: Connection = Depends(get_db)
):
    """批量录入潮高预报点；同一时刻重复提交即更正（覆盖旧值）。"""
    _get_port_or_404(db, port_id)
    for p in payload.points:
        t = _normalize(p.forecast_time).isoformat()
        db.execute(
            """
            INSERT INTO tide_forecasts (port_id, forecast_time, tide_height)
            VALUES (?, ?, ?)
            ON CONFLICT (port_id, forecast_time)
            DO UPDATE SET tide_height = excluded.tide_height
            """,
            (port_id, t, p.tide_height),
        )
    return _list_forecasts(db, port_id)


@app.get("/ports/{port_id}/tide-forecasts", response_model=list[TideForecastOut])
def get_tide_forecasts(port_id: int, db: Connection = Depends(get_db)):
    _get_port_or_404(db, port_id)
    return _list_forecasts(db, port_id)


def _list_forecasts(db: Connection, port_id: int) -> list[TideForecastOut]:
    rows = db.execute(
        "SELECT forecast_time, tide_height FROM tide_forecasts "
        "WHERE port_id = ? ORDER BY forecast_time",
        (port_id,),
    ).fetchall()
    return [
        TideForecastOut(forecast_time=datetime.fromisoformat(r["forecast_time"]), tide_height=r["tide_height"])
        for r in rows
    ]


def _forecast_points(db: Connection, port_id: int) -> list[tuple[datetime, float]]:
    """读取某港口最新预报点（(UTC 时刻, 潮高)，按时刻升序）。"""
    rows = db.execute(
        "SELECT forecast_time, tide_height FROM tide_forecasts "
        "WHERE port_id = ? ORDER BY forecast_time",
        (port_id,),
    ).fetchall()
    return [
        (datetime.fromisoformat(r["forecast_time"]), r["tide_height"])
        for r in rows
    ]


@app.post("/ports/{port_id}/windows", response_model=WindowQueryResult)
def query_windows(port_id: int, payload: WindowQuery, db: Connection = Depends(get_db)):
    """船舶提交吃水、安全富余、通航时长，返回全部可出发时段与最早出发时刻。

    每次查询都基于数据库中最新的预报点实时重算，预报点更正后立即生效。
    """
    port = _get_port_or_404(db, port_id)
    points = _forecast_points(db, port_id)

    required_depth = payload.draft + payload.safety_margin
    windows, earliest = departure_windows(
        points,
        reference_depth=port["reference_depth"],
        required_depth=required_depth,
        duration_seconds=payload.duration_minutes * 60,
    )
    return WindowQueryResult(
        port_id=port_id,
        required_depth=required_depth,
        duration_minutes=payload.duration_minutes,
        earliest_departure=earliest,
        windows=[DepartureWindow(start=s, end=e) for s, e in windows],
    )


# ---------------------------------------------------------------------------
# 单向航道预约
# ---------------------------------------------------------------------------

# 有效预约的状态值；已取消预约不再占用航道，也不在有效列表中
_STATUS_ACTIVE = "active"
_STATUS_CANCELLED = "cancelled"


def _reservation_from_row(row) -> ReservationOut:
    return ReservationOut(
        id=row["id"],
        port_id=row["port_id"],
        ship_name=row["ship_name"],
        draft=row["draft"],
        safety_margin=row["safety_margin"],
        duration_minutes=row["duration_minutes"],
        allowed_departure_from=datetime.fromisoformat(row["allowed_departure_from"]),
        allowed_departure_to=datetime.fromisoformat(row["allowed_departure_to"]),
        departure_start=datetime.fromisoformat(row["departure_start"]),
        departure_end=datetime.fromisoformat(row["departure_end"]),
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        canceled_at=(
            datetime.fromisoformat(row["canceled_at"]) if row["canceled_at"] else None
        ),
    )


def _active_blockers(db: Connection, port_id: int) -> list[tuple[datetime, datetime]]:
    """读取该港口全部有效（未取消）预约的通航占用区间。"""
    rows = db.execute(
        "SELECT departure_start, departure_end FROM reservations "
        "WHERE port_id = ? AND status = ? ORDER BY departure_start",
        (port_id, _STATUS_ACTIVE),
    ).fetchall()
    return [
        (
            datetime.fromisoformat(r["departure_start"]),
            datetime.fromisoformat(r["departure_end"]),
        )
        for r in rows
    ]


@app.post("/ports/{port_id}/reservations", response_model=ReservationOut, status_code=201)
def create_reservation(port_id: int, payload: ReservationCreate):
    """创建单向航道预约。

    基于该港口**最新预报**求可出发时段，与请求的允许出发范围取交集，
    再在其中安排**最早**、整个通航过程不与现有有效预约重叠的出发时刻
    （两船首尾相接允许）。无可用时刻返回 409 且不写入。

    同港口并发创建按港口串行化，并在 SQLite 立即写事务内原子地
    “检查 + 落库”，避免双重占用。
    """
    allowed_from = _normalize(payload.allowed_departure_from)
    allowed_to = _normalize(payload.allowed_departure_to)
    duration = timedelta(minutes=payload.duration_minutes)

    # 同港先在进程内排队，避免并发写事务互相撞锁
    with port_lock(port_id):
        with locked_reservation_tx() as db:
            port = _get_port_or_404(db, port_id)
            points = _forecast_points(db, port_id)
            required_depth = payload.draft + payload.safety_margin
            tide_windows, _ = departure_windows(
                points,
                reference_depth=port["reference_depth"],
                required_depth=required_depth,
                duration_seconds=payload.duration_minutes * 60,
            )
            blockers = _active_blockers(db, port_id)
            departure = earliest_scheduled_departure(
                tide_windows,
                duration_seconds=payload.duration_minutes * 60,
                allowed_start=allowed_from,
                allowed_end=allowed_to,
                blockers=blockers,
            )
            if departure is None:
                # 无可用时刻：事务回滚，不写入任何数据
                raise HTTPException(
                    status_code=409,
                    detail="允许出发范围内无可用时刻（潮高不足或与现有预约冲突）",
                )
            arrival = departure + duration
            now = datetime.now(timezone.utc)
            cur = db.execute(
                """
                INSERT INTO reservations (
                    port_id, ship_name, draft, safety_margin, duration_minutes,
                    allowed_departure_from, allowed_departure_to,
                    departure_start, departure_end, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    port_id,
                    payload.ship_name,
                    payload.draft,
                    payload.safety_margin,
                    payload.duration_minutes,
                    allowed_from.isoformat(),
                    allowed_to.isoformat(),
                    departure.isoformat(),
                    arrival.isoformat(),
                    _STATUS_ACTIVE,
                    now.isoformat(),
                ),
            )
            row = db.execute(
                "SELECT * FROM reservations WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _reservation_from_row(row)


@app.get("/ports/{port_id}/reservations", response_model=list[ReservationOut])
def list_reservations(port_id: int, db: Connection = Depends(get_db)):
    """按港口列出全部**有效**（未取消）预约，按出发时刻升序。"""
    _get_port_or_404(db, port_id)
    rows = db.execute(
        "SELECT * FROM reservations WHERE port_id = ? AND status = ? "
        "ORDER BY departure_start",
        (port_id, _STATUS_ACTIVE),
    ).fetchall()
    return [_reservation_from_row(r) for r in rows]


@app.delete("/reservations/{reservation_id}", response_model=ReservationOut)
def cancel_reservation(reservation_id: int, db: Connection = Depends(get_db)):
    """取消预约；不存在返回 404，已取消返回 409。

    取消后该预约立即释放航道占用；预报点更正不会追溯影响（取消）已有预约。
    """
    row = db.execute(
        "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"预约 {reservation_id} 不存在")
    if row["status"] != _STATUS_ACTIVE:
        raise HTTPException(status_code=409, detail=f"预约 {reservation_id} 已取消")
    canceled_at = datetime.now(timezone.utc)
    db.execute(
        "UPDATE reservations SET status = ?, canceled_at = ? WHERE id = ?",
        (_STATUS_CANCELLED, canceled_at.isoformat(), reservation_id),
    )
    row = db.execute(
        "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
    ).fetchone()
    return _reservation_from_row(row)


@app.get("/reservations/{reservation_id}/depth-audit", response_model=DepthAuditResult)
def audit_reservation_depth(reservation_id: int, db: Connection = Depends(get_db)):
    """预约水深复核（只读，不改动预约或原排程）。

    读取预约排定的通航起止、吃水与安全富余，按该港口**查询时最新预报**
    相邻点线性插值复算水深：合并返回通航区间内水深低于所需水深的连续
    时段及各段最大欠深；预报首末点未覆盖整个通航区间时，明确返回未覆盖
    时段且 `sufficient` 一律为 false（不判为安全）。
    """
    row = db.execute(
        "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"预约 {reservation_id} 不存在")
    port = _get_port_or_404(db, row["port_id"])
    points = _forecast_points(db, row["port_id"])

    required_depth = row["draft"] + row["safety_margin"]
    departure_start = datetime.fromisoformat(row["departure_start"])
    departure_end = datetime.fromisoformat(row["departure_end"])
    deficits, uncovered = depth_deficit_audit(
        points,
        reference_depth=port["reference_depth"],
        required_depth=required_depth,
        start=departure_start,
        end=departure_end,
    )
    return DepthAuditResult(
        reservation_id=row["id"],
        port_id=row["port_id"],
        ship_name=row["ship_name"],
        status=row["status"],
        departure_start=departure_start,
        departure_end=departure_end,
        draft=row["draft"],
        safety_margin=row["safety_margin"],
        required_depth=required_depth,
        coverage_complete=not uncovered,
        uncovered=[DepthAuditUncovered(start=s, end=e) for s, e in uncovered],
        deficits=[
            DepthAuditDeficit(start=s, end=e, max_deficit=m) for s, e, m in deficits
        ],
        sufficient=not uncovered and not deficits,
    )
