"""Pydantic 请求/响应模型。"""
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field, PlainSerializer, model_validator

# 响应中 UTC 时刻统一序列化为带显式偏移的 ISO 8601（...+00:00），
# 不随 pydantic 版本改用 “Z” 后缀，保证文档与响应格式稳定。
UtcDatetime = Annotated[
    datetime,
    PlainSerializer(
        lambda dt: dt.astimezone(timezone.utc).isoformat() if dt is not None else None,
        return_type=str,
        when_used="json",
    ),
]


class PortCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="港口名称")
    reference_depth: float = Field(description="航道基准水深（米）")


class PortUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    reference_depth: float | None = Field(default=None, description="航道基准水深（米）")


class PortOut(BaseModel):
    id: int
    name: str
    reference_depth: float


class TideForecastPoint(BaseModel):
    forecast_time: datetime = Field(description="预报时刻（ISO 8601；无时区信息按 UTC 处理）")
    tide_height: float = Field(description="潮高（米，相对海图基准面）")


class TideForecastUpsert(BaseModel):
    """批量录入/更正预报点：同一港口同一时刻重复提交即覆盖（更正）。"""

    points: list[TideForecastPoint] = Field(min_length=1)


class TideForecastOut(BaseModel):
    forecast_time: UtcDatetime
    tide_height: float


class WindowQuery(BaseModel):
    draft: float = Field(ge=0, description="船舶吃水（米）")
    safety_margin: float = Field(ge=0, description="安全富余（米）")
    duration_minutes: float = Field(gt=0, description="所需通航时长（分钟），必须为正数")


class DepartureWindow(BaseModel):
    start: UtcDatetime = Field(description="可出发时段起点")
    end: UtcDatetime = Field(description="可出发时段终点（达标窗口结束时刻 − 通航时长）")


class WindowQueryResult(BaseModel):
    port_id: int
    required_depth: float = Field(description="所需水深 = 吃水 + 安全富余（米）")
    duration_minutes: float
    earliest_departure: UtcDatetime | None = Field(
        description="最早可出发时刻；无可出发时段时为 null"
    )
    windows: list[DepartureWindow] = Field(description="全部可出发时段")


class ReservationCreate(BaseModel):
    ship_name: str = Field(min_length=1, max_length=100, description="船名")
    draft: float = Field(ge=0, description="船舶吃水（米）")
    safety_margin: float = Field(ge=0, description="安全富余（米）")
    duration_minutes: float = Field(gt=0, description="所需通航时长（分钟），必须为正数")
    allowed_departure_from: datetime = Field(description="允许出发范围起点（ISO 8601；无时区信息按 UTC）")
    allowed_departure_to: datetime = Field(description="允许出发范围终点（ISO 8601；无时区信息按 UTC）")

    @model_validator(mode="after")
    def _validate_range(self):
        start = self.allowed_departure_from
        end = self.allowed_departure_to
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if start > end:
            raise ValueError("allowed_departure_from 不得晚于 allowed_departure_to")
        return self


class ReservationOut(BaseModel):
    id: int
    port_id: int
    ship_name: str
    draft: float
    safety_margin: float
    duration_minutes: float
    allowed_departure_from: UtcDatetime = Field(description="请求的允许出发范围起点")
    allowed_departure_to: UtcDatetime = Field(description="请求的允许出发范围终点")
    departure_start: UtcDatetime = Field(description="排定的出发时刻")
    departure_end: UtcDatetime = Field(description="排定的通航结束时刻 = 出发时刻 + 通航时长")
    status: str = Field(description="状态：active / cancelled")
    created_at: UtcDatetime
    canceled_at: UtcDatetime | None = None


class DepthAuditDeficit(BaseModel):
    start: UtcDatetime = Field(description="欠深时段起点")
    end: UtcDatetime = Field(description="欠深时段终点")
    max_deficit: float = Field(description="该时段最大欠深（米）= 所需水深 − 时段内最低水深")


class DepthAuditUncovered(BaseModel):
    start: UtcDatetime = Field(description="未覆盖时段起点")
    end: UtcDatetime = Field(description="未覆盖时段终点")


class DepthAuditResult(BaseModel):
    reservation_id: int
    port_id: int
    ship_name: str
    status: str = Field(description="预约状态：active / cancelled")
    departure_start: UtcDatetime = Field(description="排定的出发时刻")
    departure_end: UtcDatetime = Field(description="排定的通航结束时刻")
    draft: float = Field(description="船舶吃水（米）")
    safety_margin: float = Field(description="安全富余（米）")
    required_depth: float = Field(description="所需水深 = 吃水 + 安全富余（米）")
    coverage_complete: bool = Field(description="最新预报是否覆盖整个通航区间")
    uncovered: list[DepthAuditUncovered] = Field(
        description="预报未覆盖的通航时段（水深未知，不得视为安全）"
    )
    deficits: list[DepthAuditDeficit] = Field(
        description="通航区间内水深低于所需水深的连续时段（相邻段已合并）"
    )
    sufficient: bool = Field(
        description="整个通航区间水深是否均达标；预报覆盖不足时一律为 false"
    )
