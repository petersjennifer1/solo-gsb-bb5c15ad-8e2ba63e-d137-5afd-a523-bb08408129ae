# 港口潮汐通航窗口与单向航道预约 API

基于 **Python + FastAPI + SQLite** 的港口潮汐通航窗口计算与单向航道预约服务。

- 录入港口的**潮高预报点**与**航道基准水深**；
- 船舶提交**吃水**、**安全富余**和**正数通航时长**；
- 相邻预报点间按**线性插值**求潮高，仅在**预报覆盖时段**内计算
  `水深 = 基准水深 + 潮高 ≥ 吃水 + 安全富余` 的连续达标窗口（**端点恰好达标也纳入**）；
- 每个达标窗口的**结束时刻减去通航时长**得到**可出发时段**，不足所需时长的窗口舍弃；
- 返回**全部可出发时段**和**最早出发时刻**；
- 预报点更正（同一时刻重复提交即覆盖）后，查询自动按最新数据实时重算；
- **单向航道预约**：在请求方允许的出发时刻范围内，按最新预报安排**最早**出发时刻，
  使整个通航过程不与该港口现有**有效预约**重叠（两船**首尾相接允许**）；
  无可用时刻返回 **409** 且不写入；同港口并发创建原子地“检查 + 落库”，杜绝双重占用；
- 已落库预约记录的是排定的确定时刻，**预报更正不追溯调整或取消已有预约**；
- **预约水深复核**：`GET /reservations/{id}/depth-audit` 按**查询时最新预报**复算
  预约排定通航区间的水深，合并返回欠深时段及各段**最大欠深**；预报首末点未覆盖
  整个通航区间时明确返回未覆盖时段且**不判为安全**；复核**只读**，不改动预约或原排程。

## 快速开始（单条命令）

```bash
docker compose up --build
```

该命令自动完成：依赖安装 → SQLite 数据库初始化（服务启动时幂等建表）→ 启动服务。

启动后访问：

- **API 地址**：http://localhost:8000
- **交互式文档（Swagger UI）**：http://localhost:8000/docs
- **健康检查**：http://localhost:8000/health

停止：`docker compose down`（加 `-v` 可同时删除数据卷）。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 服务端口 | `8000` | 修改 `docker-compose.yml` 中 `ports` 左侧的宿主机端口即可 |
| `DATABASE_PATH` | `/data/tide.db`（容器内）/ `tide.db`（本地） | SQLite 数据库文件路径，通过环境变量配置 |
| 数据持久化 | 命名卷 `tide-data` → `/data` | 容器重建后数据保留 |

时间约定：所有时刻使用 ISO 8601；不带时区信息时按 **UTC** 处理，响应统一返回 UTC 时刻。
长度单位为米，时长单位为分钟。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/ports` | 录入港口（名称 + 航道基准水深） |
| `GET` | `/ports` / `/ports/{id}` | 查询港口 |
| `PATCH` | `/ports/{id}` | 更正港口名称 / 基准水深 |
| `PUT` | `/ports/{id}/tide-forecasts` | 批量录入/更正潮高预报点（同一时刻重复提交即覆盖） |
| `GET` | `/ports/{id}/tide-forecasts` | 查看当前预报点 |
| `POST` | `/ports/{id}/windows` | 提交吃水、安全富余、通航时长，计算可出发时段 |
| `POST` | `/ports/{id}/reservations` | 创建单向航道预约（自动排最早出发时刻） |
| `GET` | `/ports/{id}/reservations` | 按港口列出**有效**预约（按出发时刻升序） |
| `DELETE` | `/reservations/{id}` | 取消预约（释放航道占用） |
| `GET` | `/reservations/{id}/depth-audit` | 预约水深复核：按最新预报复算排定通航区间水深（只读） |

### 完整示例

```bash
# 1. 录入港口：航道基准水深 4.5 米
curl -X POST http://localhost:8000/ports \
  -H "Content-Type: application/json" \
  -d '{"name": "示例港", "reference_depth": 4.5}'

# 2. 录入潮高预报点（每 3 小时一个点）
curl -X PUT http://localhost:8000/ports/1/tide-forecasts \
  -H "Content-Type: application/json" \
  -d '{"points": [
        {"forecast_time": "2026-09-24T00:00:00Z", "tide_height": 0.5},
        {"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 3.0},
        {"forecast_time": "2026-09-24T06:00:00Z", "tide_height": 0.5}
      ]}'

# 3. 船舶查询：吃水 5.0 米，安全富余 0.5 米，需要 60 分钟连续通航
curl -X POST http://localhost:8000/ports/1/windows \
  -H "Content-Type: application/json" \
  -d '{"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60}'
```

响应（所需水深 = 5.0 + 0.5 = 5.5 米；水深 5.0→7.5→5.0，线性插值得达标窗口
`[00:36, 05:24]`，减去 60 分钟通航时长）：

```json
{
  "port_id": 1,
  "required_depth": 5.5,
  "duration_minutes": 60.0,
  "earliest_departure": "2026-09-24T00:36:00+00:00",
  "windows": [
    {"start": "2026-09-24T00:36:00+00:00", "end": "2026-09-24T04:24:00+00:00"}
  ]
}
```

```bash
# 4. 更正 03:00 的预报点（3.0 → 1.0），随后查询自动按最新数据重算
curl -X PUT http://localhost:8000/ports/1/tide-forecasts \
  -H "Content-Type: application/json" \
  -d '{"points": [{"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 1.0}]}'

curl -X POST http://localhost:8000/ports/1/windows \
  -H "Content-Type: application/json" \
  -d '{"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60}'
# → {"windows": [], "earliest_departure": null, ...}（峰值水深仅 5.5，窗口长度不足 60 分钟）
```

### 参数校验

- `duration_minutes` 必须为**正数**（`> 0`），否则返回 422；
- `draft`、`safety_margin` 必须为非负数；
- 预报点不足 2 个（无插值区间）或全程不达标时，返回空窗口列表与 `earliest_departure: null`。

## 单向航道预约

创建预约时提交：船名、吃水、安全富余、通航分钟数，以及**允许出发的起止时刻**
（`allowed_departure_from` ≤ `allowed_departure_to`，含端点；均按上面的 UTC 规则解释）。

排程规则：

1. 基于该港口**最新预报**计算潮汐可出发时段（同 `/windows`）；
2. 与请求的允许出发范围取交集；
3. 在其中选择**最早**的出发时刻，使整个通航区间 `[出发, 出发 + 通航时长]`
   不与该港口任何**有效预约**重叠——两船**首尾相接允许**
   （上一条 01:00 结束，下一条 01:00 出发合法）；
4. 无可用时刻返回 **409 Conflict** 且**不写入**任何数据。

同港口并发创建：服务先按港口在进程内串行化，再于 SQLite `BEGIN IMMEDIATE`
写事务中原子地完成“检查最新预报与现有预约 → 插入”，并设置 `busy_timeout`
让并发请求排队等待，因此不会产生双重占用。

```bash
# 沿用上面的示例港：可出发时段 [00:36, 04:24]，甲船申请 00:00–04:24 出发
curl -X POST http://localhost:8000/ports/1/reservations \
  -H "Content-Type: application/json" \
  -d '{"ship_name": "甲船",
       "draft": 5.0,
       "safety_margin": 0.5,
       "duration_minutes": 60,
       "allowed_departure_from": "2026-09-24T00:00:00Z",
       "allowed_departure_to":   "2026-09-24T04:24:00Z"}'
```

响应（`201 Created`；潮汐最早 00:36 可出发，排定 00:36–01:36）：

```json
{
  "id": 1,
  "port_id": 1,
  "ship_name": "甲船",
  "draft": 5.0,
  "safety_margin": 0.5,
  "duration_minutes": 60.0,
  "allowed_departure_from": "2026-09-24T00:00:00+00:00",
  "allowed_departure_to": "2026-09-24T04:24:00+00:00",
  "departure_start": "2026-09-24T00:36:00+00:00",
  "departure_end": "2026-09-24T01:36:00+00:00",
  "status": "active",
  "created_at": "2026-09-24T10:00:00.000000+00:00",
  "canceled_at": null
}
```

```bash
# 乙船只允许在 01:30 前出发：01:30 出发会在 [01:30,02:30] 与甲船重叠 → 409
curl -i -X POST http://localhost:8000/ports/1/reservations \
  -H "Content-Type: application/json" \
  -d '{"ship_name": "乙船",
       "draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60,
       "allowed_departure_from": "2026-09-24T00:00:00Z",
       "allowed_departure_to":   "2026-09-24T01:30:00Z"}'
# HTTP/1.1 409 Conflict
# {"detail":"允许出发范围内无可用时刻（潮高不足或与现有预约冲突）"}

# 丙船允许到 04:24：最早可紧随甲船 01:36 出发（首尾相接）
curl -X POST http://localhost:8000/ports/1/reservations \
  -H "Content-Type: application/json" \
  -d '{"ship_name": "丙船",
       "draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60,
       "allowed_departure_from": "2026-09-24T00:00:00Z",
       "allowed_departure_to":   "2026-09-24T04:24:00Z"}'
# → departure_start 01:36，departure_end 02:36

# 列出该港口全部有效预约（已取消的不在其中）
curl http://localhost:8000/ports/1/reservations

# 取消预约 1：立即释放占用；取消未知预约 404，重复取消 409
curl -X DELETE http://localhost:8000/reservations/1
```

说明：

- **预报更正不追溯**：预约创建后即固定 `departure_start/end`；之后更正预报点，
  已有预约既不会被自动取消，时刻也不会变化（仅影响此后新建预约的排程）；
- 取消后的预约状态变为 `cancelled`，不再占用航道、不出现在有效列表中；
- 创建预约的校验与 `/windows` 一致（通航时长为正、吃水/富余非负），
  此外允许出发范围起止颠倒返回 422，港口不存在返回 404。

## 预约水深复核

`GET /reservations/{id}/depth-audit`（服务默认监听 **8000** 端口）对已排定的预约
做**只读**水深复核：

- 读取预约排定的通航起止（`departure_start` / `departure_end`）、吃水与安全富余，
  按该港口**查询时刻的最新预报**相邻点线性插值复算水深；
- `deficits`：通航区间内水深低于所需水深（吃水 + 安全富余）的连续时段，
  相邻段已合并，并给出各段**最大欠深**（所需水深 − 时段内最低水深）；
- 预报首末点未覆盖整个通航区间时，`coverage_complete` 为 `false`，
  `uncovered` 列出未覆盖时段——这些时段水深未知，**不判为安全**，
  此时 `sufficient` 一律为 `false`；
- 结果只反映查询时的预报，**不改动预约或原排程**；预约不存在返回 404，
  已取消的预约也可复核（`status` 为 `cancelled`）。

```bash
# 复核预约 1（沿用上文示例：甲船排定 00:36–01:36，所需水深 5.5 米）
curl http://localhost:8000/reservations/1/depth-audit
```

预报未变时，排定区间内水深全程达标：

```json
{
  "reservation_id": 1,
  "port_id": 1,
  "ship_name": "甲船",
  "status": "active",
  "departure_start": "2026-09-24T00:36:00+00:00",
  "departure_end": "2026-09-24T01:36:00+00:00",
  "draft": 5.0,
  "safety_margin": 0.5,
  "required_depth": 5.5,
  "coverage_complete": true,
  "uncovered": [],
  "deficits": [],
  "sufficient": true
}
```

若 03:00 的预报点被更正为 1.0（见上文"完整示例"第 4 步），同一预约复核即
发现全程欠深（00:36 处欠 0.4 米），但预约本身的排定时刻不受影响：

```json
{
  "reservation_id": 1,
  "port_id": 1,
  "ship_name": "甲船",
  "status": "active",
  "departure_start": "2026-09-24T00:36:00+00:00",
  "departure_end": "2026-09-24T01:36:00+00:00",
  "draft": 5.0,
  "safety_margin": 0.5,
  "required_depth": 5.5,
  "coverage_complete": true,
  "uncovered": [],
  "deficits": [
    {"start": "2026-09-24T00:36:00+00:00", "end": "2026-09-24T01:36:00+00:00", "max_deficit": 0.4}
  ],
  "sufficient": false
}
```

若最新预报只覆盖到 01:00（首末点未覆盖整个通航区间），则返回
`coverage_complete: false`、`uncovered: [{"start": "…01:00…", "end": "…01:36…"}]`
且 `sufficient: false`——覆盖不足**不会**被当作安全。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # 运行依赖 + pytest/httpx
uvicorn app.main:app --reload         # 默认使用 ./tide.db，可用 DATABASE_PATH 覆盖
pytest                                # 运行全部测试（核心算法 + API 端到端）
```

## 项目结构

```
app/
  main.py       # FastAPI 路由与生命周期（启动时初始化数据库）
  tides.py      # 核心算法：线性插值、达标窗口、可出发时段、水深复核
  database.py   # SQLite 连接与幂等建表
  schemas.py    # Pydantic 请求/响应模型与校验
tests/
  test_tides.py # 核心算法单元测试（仅标准库）
  test_api.py   # API 端到端测试
Dockerfile / docker-compose.yml / requirements*.txt
```
