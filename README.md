# 单断面总磷核查 API

FastAPI + SQLite 实现的单断面总磷（TP）核查服务。化验单以整数 μg/L 声明浓度区间，
服务按水样汇总现行区间，并给出月度警戒线计数的上下界与待复测名单。

## 规则映射

| 业务规则 | 实现 |
| --- | --- |
| 化验单填浓度闭区间 | `measured: {lower, upper}`，两端均含，`lower <= upper`，均为非负整数 |
| 未检出填检出限，按 `[0, L)` 计 | `nondetect: {detection_limit: L}`，内部记为 `[0, L)`；整数域等价于 `[0, L-1]` |
| 每张单只用最高修订 | 每份 `(report_no)` 取 `MAX(revision)` 参与交集，旧修订仅保留历史 |
| 原样重报不增计 | 同单号、同修订、同内容返回 `200`，`accepted=false, idempotent=true`，不新增行、不重算 |
| 同号同版异文退回 | 同 `report_no` + `revision` 但内容不同返回 `409` |
| 旧修订不可补录 | 提交修订号不大于已存最高修订且非幂等时返回 `409` |
| 同号跨水样 | 化验单号全局唯一，重复用于其他水样返回 `409` |
| 同水样取各现行区间交集 | `sample_state` 保存交集；空交集记为 `retest` |
| 交集为空列为待复测、不计入分母 | `retest` 水样进入名单并从 `denominator` 排除 |
| 采样月固定 | `samples.sampling_month` 创建后不可变更，冲突返回 `409` |
| 警戒线计数上下界 | 下界=区间下端点已达警戒线的样本数（必然达到）；上界=区间上端点可达警戒线的样本数（可能达到） |
| 更正只重算本水样 | 写事务内先减去该水样旧贡献、再加入新贡献，不扫描其他水样 |
| 三件产物一起生效 | 化验单、`sample_state`、月度摘要/直方图在同一 `BEGIN IMMEDIATE` 事务提交 |
| 写入失败仍查原结果 | 任何异常整体回滚，返回 `500`，读路径只看到上次已提交状态 |
| 一次查询来自同一已提交状态 | `/summary` 在单个读事务（WAL 快照）内取计数与名单，并校验 `denominator + retest_count == total_samples` 且 `retest_count == len(list)` |
| 按采样月重建 | `POST /months/{month}/rebuild` 直接从每张化验单的最高修订重算该水样判定、待复测名单与月度摘要 |
| 重建期间读旧结果 | 重算在读事务里进行，不持写锁；只有最后的替换是一次短的 `BEGIN IMMEDIATE` |
| 同月新修订纳入或重试 | 每月有变更代号 `month_generation`；替换前在写事务内复核，若期间同月有新修订则重算并纳入，连续冲突返回 `409` 提示重试 |
| 其他月份报送不使重建失效 | 代号按采样月独立计数，其他月写入不改变本月代号 |
| 重建失败保留原结果 | 三件产物在同一写事务内先删后插，任一步失败整体回滚 |

### 区间端点范围

浓度端点与警戒线均为非负整数，上限为 SQLite 的 `2^63-1`；超出范围的端点直接返回 `422`，
避免 SQLite 静默转成浮点后丢失整数语义。浮点数（如 `10.5`）同样被拒绝。

### 上下界语义

设某水样现行区间为 `[lo, hi]`（未检出为 `[0, L)`，`hi = L-1`），警戒线 `T`：

- 下界（必然 ≥ T）：`lo >= T`；
- 上界（可能 ≥ T）：`hi >= T`。

因为 `[0, L)` 不含 `L`，警戒线恰好等于 `L` 时计为**未达到**；等于 `L-1` 时计为可能达到。
闭区间则包含端点，`hi == T` 计为达到。

## 启动

```bash
docker compose up --build
```

- Swagger UI：<http://localhost:8000/docs>（每个端点均可 "Try it out"）
- OpenAPI：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/healthz>
- 数据卷：`tp-data` 挂载到容器 `/data/tp.sqlite3`

### 旧备份样例与重建演示

`docker-compose.yml` 另有一个一次性服务 `legacy-sample`（仅在 `demo` profile 下启用，避免常规启动把旧摘要
重放到已重建的月份），在 API 健康后把 `samples/legacy_2026-10.json` 装载进同一个数据卷。该样例模拟
“化验单原件已补齐、派生统计仍是旧备份”的状态：2026-10 的 `S-2026-10-01` 仍按已被取代的修订 `99..100`
计数，`S-2026-10-05` 被旧名单误列为待复测并从分母漏掉。装载后：

```bash
docker compose up --build -d
docker compose --profile demo run --rm legacy-sample   # 装载旧备份样例
```

然后在 Swagger：

1. `POST /months/2026-10/rebuild` 重建该月；
2. `GET /summary?month=2026-10&threshold=20` 查看新月报；
3. `GET /retest?month=2026-10` 查看新的待复测名单。

| | 重建前（旧摘要） | 重建后 |
| --- | --- | --- |
| 总样本 / 分母 / 待复测 | 5 / 3 / 2 | 5 / 4 / 1 |
| 待复测名单 | `S-2026-10-02`, `S-2026-10-05` | `S-2026-10-02` |
| 警戒线 20 的上下界 | 2 / 3 | 3 / 4 |

对应 SQLite 已就绪时也可直接装载：`python3 scripts/load_legacy_sample.py samples/legacy_2026-10.json --db /data/tp.sqlite3`。

本地直接运行：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
TP_DB_PATH=/tmp/tp.sqlite3 uvicorn app.api:app --reload
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/samples` | 登记水样与固定采样月（幂等） |
| `GET` | `/samples/{sample_no}` | 查看水样、化验单历史、现行区间与状态 |
| `POST` | `/samples/{sample_no}/reports` | 提交/更正一张化验单的一个修订 |
| `POST` | `/months/{month}/rebuild` | 按采样月从最高修订重建判定、名单与摘要 |
| `GET` | `/summary?month=YYYY-MM&threshold=T` | 月度上下界计数、分母与待复测名单 |
| `GET` | `/retest?month=YYYY-MM` | 该月待复测水样号 |
| `GET` | `/healthz` | 健康检查 |

### 示例

```bash
# 登记水样
curl -X POST localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{"sample_no":"S-001","sampling_month":"2026-11"}'

# 实测闭区间
curl -X POST localhost:8000/samples/S-001/reports \
  -H 'Content-Type: application/json' \
  -d '{"report_no":"LAB-A-1","revision":1,"result":{"kind":"measured","lower":30,"upper":45}}'

# 未检出，按 [0,10) 计
curl -X POST localhost:8000/samples/S-001/reports \
  -H 'Content-Type: application/json' \
  -d '{"report_no":"LAB-A-2","revision":1,"result":{"kind":"nondetect","detection_limit":10}}'

# 查询月度摘要（该水样交集为空 -> 待复测，分母不计数）
curl 'localhost:8000/summary?month=2026-11&threshold=20'
```

## 数据结构

- `samples`：水样号、固定采样月；
- `reports`：`(report_no, revision)` 主键，原始区间端点与内容指纹；
- `sample_state`：每水样一行，`ok` 保存现行交集，`retest` 表示空交集；
- `monthly_summary`：每月总样本数、分母、待复测数；
- `monthly_lower_hist` / `monthly_max_hist`：按区间下端点 / 最大可达整数分桶的计数，
  使任意警戒线的上下界计数为一次索引范围求和，无需扫描水样。

摘要采用增量维护：每个水样对月度聚合最多贡献两条直方图记录，更正时先撤销旧贡献再加入
新贡献，因此只触及目标水样。

`month_generation` 记录每个采样月新入库的修订次数。提交新修订时与报告、派生结果同事务
自增；重建重算完成后，在替换写事务内复核该月代号，若期间同月有新修订就把新修订纳入重算，
连续多次冲突则返回 `409` 让调用方重试。其他月份的代号互不影响，因此其他月份的报送不会让
本次重建失效。

## 测试

```bash
pip install -r requirements-dev.txt
TP_DB_PATH=/tmp/unused.sqlite3 pytest tests -q
```

覆盖区间代数（含 `[0,L)` 端点）、幂等与冲突、最高修订、交集与待复测、分母与上下界、
更正局部性（仅重算目标水样）、跨月隔离、写入回滚与恢复、读快照一致性、多线程并发聚合一致性，
以及增量聚合与暴力重算的随机对拍；另含按采样月重建（旧备份纠偏、最高修订、跨月隔离、
重建期间读旧结果、同月新修订纳入/重试、其他月报送不失效、失败保留原结果、并发交错）和
旧备份样例的装载与重建校验。

当前共 49 个测试，全部通过。

## 已知限制

- 单进程/单机 SQLite（WAL）。未做跨机复制；如需水平扩展应换 PostgreSQL。
- 重建在单个进程内完成；同月持续高频报送时可能多次重算，超过 8 次尝试返回 `409`，需调用方重试。
- `threshold` 为查询参数，不做持久化配置。
- 不做化验单文件上传或 OCR，仅接收结构化 JSON。
- 本次交付环境无 Docker 守护进程，Compose / Dockerfile 仅做静态校验与本地 `uvicorn` 实跑；
  未实际执行 `docker compose up`。
