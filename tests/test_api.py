"""API 端到端测试（需要 fastapi/httpx，容器内或安装依赖后运行）。"""
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 每个测试用例使用独立的临时数据库文件
    monkeypatch.setattr(database, "DATABASE_PATH", str(tmp_path / "test.db"))
    database.init_db()
    with TestClient(app) as c:
        yield c


def _setup_port(client):
    r = client.post("/ports", json={"name": "示例港", "reference_depth": 4.5})
    assert r.status_code == 201
    port_id = r.json()["id"]
    r = client.put(
        f"/ports/{port_id}/tide-forecasts",
        json={
            "points": [
                {"forecast_time": "2026-09-24T00:00:00Z", "tide_height": 0.5},
                {"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 3.0},
                {"forecast_time": "2026-09-24T06:00:00Z", "tide_height": 0.5},
            ]
        },
    )
    assert r.status_code == 200
    return port_id


def test_full_flow_and_correction_recompute(client):
    port_id = _setup_port(client)

    # 吃水 5.0 + 富余 0.5 = 要求水深 5.5；基准 4.5 → 需潮高 >= 1.0
    # 水深 5.0→7.5→5.0，达标窗口 [00:36, 05:24]，通航 60 分钟
    r = client.post(
        f"/ports/{port_id}/windows",
        json={"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["required_depth"] == 5.5
    assert body["earliest_departure"] == "2026-09-24T00:36:00+00:00"
    assert body["windows"] == [
        {"start": "2026-09-24T00:36:00+00:00", "end": "2026-09-24T04:24:00+00:00"}
    ]

    # 更正高潮预报点 3.0 → 1.0：水深峰值仅 5.5，只剩零长窗口，不足 60 分钟
    r = client.put(
        f"/ports/{port_id}/tide-forecasts",
        json={"points": [{"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 1.0}]},
    )
    assert r.status_code == 200

    r = client.post(
        f"/ports/{port_id}/windows",
        json={"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60},
    )
    body = r.json()
    assert body["windows"] == []
    assert body["earliest_departure"] is None


def test_validation_rejects_non_positive_duration(client):
    port_id = _setup_port(client)
    for bad in (0, -30):
        r = client.post(
            f"/ports/{port_id}/windows",
            json={"draft": 5.0, "safety_margin": 0.5, "duration_minutes": bad},
        )
        assert r.status_code == 422


def test_unknown_port_404(client):
    assert client.post(
        "/ports/999/windows",
        json={"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60},
    ).status_code == 404


def test_no_forecasts_returns_empty(client):
    r = client.post("/ports", json={"name": "空港", "reference_depth": 10.0})
    port_id = r.json()["id"]
    r = client.post(
        f"/ports/{port_id}/windows",
        json={"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60},
    )
    assert r.status_code == 200
    assert r.json()["windows"] == []
    assert r.json()["earliest_departure"] is None


# ---------------------------------------------------------------------------
# 单向航道预约
# ---------------------------------------------------------------------------

# 示例港：基准 4.5，潮高 0.5→3.0→0.5（00:00/03:00/06:00）
# 吃水 5.0 + 富余 0.5 → 达标窗口 [00:36, 05:24]，60 分钟通航
# → 可出发时段 [00:36, 04:24]
RESERVATION_BODY = {
    "ship_name": "远洋号",
    "draft": 5.0,
    "safety_margin": 0.5,
    "duration_minutes": 60,
    "allowed_departure_from": "2026-09-24T00:00:00Z",
    "allowed_departure_to": "2026-09-24T04:24:00Z",
}


def test_create_reservation_schedules_earliest(client):
    port_id = _setup_port(client)
    r = client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "active"
    assert body["departure_start"] == "2026-09-24T00:36:00+00:00"
    assert body["departure_end"] == "2026-09-24T01:36:00+00:00"
    assert body["allowed_departure_from"] == "2026-09-24T00:00:00+00:00"
    assert body["id"] > 0


def test_reservation_within_range_starts_no_earlier_than_range(client):
    port_id = _setup_port(client)
    body = {
        **RESERVATION_BODY,
        "allowed_departure_from": "2026-09-24T02:00:00Z",
    }
    r = client.post(f"/ports/{port_id}/reservations", json=body)
    assert r.status_code == 201
    assert r.json()["departure_start"] == "2026-09-24T02:00:00+00:00"


def test_reservations_do_not_overlap_but_may_touch(client):
    port_id = _setup_port(client)
    r1 = client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY)
    assert r1.status_code == 201
    # 第二个预约最早只能紧随其后：01:36 出发（首尾相接）
    r2 = client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY)
    assert r2.status_code == 201
    assert r2.json()["departure_start"] == "2026-09-24T01:36:00+00:00"
    assert r2.json()["departure_end"] == "2026-09-24T02:36:00+00:00"


def test_reservation_conflict_returns_409_and_writes_nothing(client):
    port_id = _setup_port(client)
    # 只允许在 04:00 前出发：第一条 00:36–01:36，第二条无可行时刻
    body = {
        **RESERVATION_BODY,
        "allowed_departure_to": "2026-09-24T00:50:00Z",
    }
    assert client.post(f"/ports/{port_id}/reservations", json=body).status_code == 201
    r = client.post(f"/ports/{port_id}/reservations", json=body)
    assert r.status_code == 409
    # 409 不写入：有效预约仍只有一条
    listed = client.get(f"/ports/{port_id}/reservations").json()
    assert len(listed) == 1


def test_list_reservations_only_active_and_port_scoped(client):
    port_id = _setup_port(client)
    other = client.post("/ports", json={"name": "其他港", "reference_depth": 4.5})
    other_id = other.json()["id"]
    client.put(
        f"/ports/{other_id}/tide-forecasts",
        json={"points": [
            {"forecast_time": "2026-09-24T00:00:00Z", "tide_height": 0.5},
            {"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 3.0},
            {"forecast_time": "2026-09-24T06:00:00Z", "tide_height": 0.5},
        ]},
    )

    r1 = client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY)
    rid = r1.json()["id"]
    client.post(f"/ports/{other_id}/reservations", json=RESERVATION_BODY)

    listed = client.get(f"/ports/{port_id}/reservations").json()
    assert [r["id"] for r in listed] == [rid]

    # 取消后不再出现在有效列表，航道占用随即释放
    assert client.delete(f"/reservations/{rid}").status_code == 200
    assert client.get(f"/ports/{port_id}/reservations").json() == []

    # 已取消再取消 → 409；不存在 → 404
    assert client.delete(f"/reservations/{rid}").status_code == 409
    assert client.delete("/reservations/999").status_code == 404


def test_cancelled_reservation_frees_the_channel(client):
    port_id = _setup_port(client)
    r1 = client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY)
    rid = r1.json()["id"]
    # 第二条被挤到 01:36
    r2 = client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY)
    assert r2.json()["departure_start"] == "2026-09-24T01:36:00+00:00"
    # 取消第一条后新建预约可以回到最早的 00:36
    client.delete(f"/reservations/{rid}")
    r3 = client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY)
    assert r3.json()["departure_start"] == "2026-09-24T00:36:00+00:00"


def test_forecast_correction_does_not_touch_existing_reservation(client):
    port_id = _setup_port(client)
    r = client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY)
    rid = r.json()["id"]
    # 预报更正为全程水深不足：已有预约保持有效、时刻不变
    client.put(
        f"/ports/{port_id}/tide-forecasts",
        json={"points": [{"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 1.0}]},
    )
    listed = client.get(f"/ports/{port_id}/reservations").json()
    assert len(listed) == 1
    assert listed[0]["id"] == rid
    assert listed[0]["departure_start"] == "2026-09-24T00:36:00+00:00"
    # 但新预约按更正后的预报无窗口 → 409
    assert client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY).status_code == 409


def test_concurrent_reservations_never_double_book(client):
    port_id = _setup_port(client)
    results: list = []
    errors: list = []

    def worker():
        try:
            r = client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY)
            results.append((r.status_code, r.json() if r.status_code == 201 else None))
        except Exception as exc:  # pragma: no cover - 并发不应抛出异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    created = [body for status, body in results if status == 201]
    conflicts = [status for status, _ in results if status == 409]
    # 可出发时段 [00:36, 04:24] 内恰好容纳 4 条 60 分钟预约（首尾相接）
    assert len(created) + len(conflicts) == 8
    assert len(created) == 4

    intervals = sorted(
        (b["departure_start"], b["departure_end"]) for b in created
    )
    # 两两不得重叠（首尾相接允许）
    for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
        assert e1 <= s2


def test_reservation_validation_errors(client):
    port_id = _setup_port(client)
    # 通航时长非正 → 422
    bad_duration = {**RESERVATION_BODY, "duration_minutes": 0}
    assert client.post(f"/ports/{port_id}/reservations", json=bad_duration).status_code == 422
    # 允许出发范围起止颠倒 → 422
    bad_range = {
        **RESERVATION_BODY,
        "allowed_departure_from": "2026-09-24T04:24:00Z",
        "allowed_departure_to": "2026-09-24T00:00:00Z",
    }
    assert client.post(f"/ports/{port_id}/reservations", json=bad_range).status_code == 422
    # 船名为空 → 422
    bad_name = {**RESERVATION_BODY, "ship_name": ""}
    assert client.post(f"/ports/{port_id}/reservations", json=bad_name).status_code == 422
    # 未知港口 → 404
    assert client.post("/ports/999/reservations", json=RESERVATION_BODY).status_code == 404


def test_reservation_naive_datetime_treated_as_utc(client):
    port_id = _setup_port(client)
    body = {
        **RESERVATION_BODY,
        "allowed_departure_from": "2026-09-24T00:00:00",   # 无时区 → UTC
        "allowed_departure_to": "2026-09-24T04:24:00",
    }
    r = client.post(f"/ports/{port_id}/reservations", json=body)
    assert r.status_code == 201
    assert r.json()["departure_start"] == "2026-09-24T00:36:00+00:00"


# ---------------------------------------------------------------------------
# 预约水深复核 GET /reservations/{id}/depth-audit
# ---------------------------------------------------------------------------


def _create_reservation(client, port_id):
    r = client.post(f"/ports/{port_id}/reservations", json=RESERVATION_BODY)
    assert r.status_code == 201
    return r.json()["id"]


def test_depth_audit_sufficient_when_forecast_unchanged(client):
    port_id = _setup_port(client)
    rid = _create_reservation(client, port_id)

    r = client.get(f"/reservations/{rid}/depth-audit")
    assert r.status_code == 200
    body = r.json()
    assert body["reservation_id"] == rid
    assert body["port_id"] == port_id
    assert body["ship_name"] == "远洋号"
    assert body["status"] == "active"
    assert body["departure_start"] == "2026-09-24T00:36:00+00:00"
    assert body["departure_end"] == "2026-09-24T01:36:00+00:00"
    assert body["required_depth"] == 5.5
    assert body["coverage_complete"] is True
    assert body["uncovered"] == []
    assert body["deficits"] == []
    assert body["sufficient"] is True


def test_depth_audit_reflects_latest_forecast_and_stays_read_only(client):
    port_id = _setup_port(client)
    rid = _create_reservation(client, port_id)

    # 更正高潮预报点 3.0 → 1.0：通航区间 [00:36, 01:36] 内水深 5.1→5.27，
    # 全程低于所需 5.5，最大欠深在 00:36（5.5 − 5.1 = 0.4）
    client.put(
        f"/ports/{port_id}/tide-forecasts",
        json={"points": [{"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 1.0}]},
    )
    body = client.get(f"/reservations/{rid}/depth-audit").json()
    assert body["coverage_complete"] is True
    assert body["uncovered"] == []
    assert body["deficits"] == [
        {
            "start": "2026-09-24T00:36:00+00:00",
            "end": "2026-09-24T01:36:00+00:00",
            "max_deficit": 0.4,
        }
    ]
    assert body["sufficient"] is False

    # 复核只读：预约的排定时刻与状态不受影响
    listed = client.get(f"/ports/{port_id}/reservations").json()
    assert len(listed) == 1
    assert listed[0]["id"] == rid
    assert listed[0]["departure_start"] == "2026-09-24T00:36:00+00:00"
    assert listed[0]["departure_end"] == "2026-09-24T01:36:00+00:00"
    assert listed[0]["status"] == "active"


def test_depth_audit_reports_uncovered_when_forecast_range_shrinks(client):
    port_id = _setup_port(client)
    rid = _create_reservation(client, port_id)

    # 模拟预报覆盖收缩（如外部系统清理旧预报点）：删掉 03:00/06:00 点，
    # 再补一个 01:00 点 → 预报只覆盖 [00:00, 01:00]，通航到 01:36
    conn = sqlite3.connect(database.DATABASE_PATH)
    conn.execute(
        "DELETE FROM tide_forecasts WHERE forecast_time IN (?, ?)",
        ("2026-09-24T03:00:00+00:00", "2026-09-24T06:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    client.put(
        f"/ports/{port_id}/tide-forecasts",
        json={"points": [{"forecast_time": "2026-09-24T01:00:00Z", "tide_height": 1.0}]},
    )

    body = client.get(f"/reservations/{rid}/depth-audit").json()
    # 覆盖不足：不得判为安全
    assert body["coverage_complete"] is False
    assert body["sufficient"] is False
    assert body["uncovered"] == [
        {"start": "2026-09-24T01:00:00+00:00", "end": "2026-09-24T01:36:00+00:00"}
    ]
    # 覆盖段 [00:36, 01:00] 内水深 5.3→5.5：00:36 起欠深，01:00 恰好达标
    assert body["deficits"] == [
        {
            "start": "2026-09-24T00:36:00+00:00",
            "end": "2026-09-24T01:00:00+00:00",
            "max_deficit": 0.2,
        }
    ]


def test_depth_audit_cancelled_reservation_still_auditable(client):
    port_id = _setup_port(client)
    rid = _create_reservation(client, port_id)
    assert client.delete(f"/reservations/{rid}").status_code == 200

    body = client.get(f"/reservations/{rid}/depth-audit").json()
    assert body["status"] == "cancelled"
    assert body["sufficient"] is True  # 预报未变，排定区间水深仍达标


def test_depth_audit_unknown_reservation_404(client):
    assert client.get("/reservations/999/depth-audit").status_code == 404
