# API 参考

服务默认监听 `http://127.0.0.1:8000`，所有接口无鉴权，**请勿暴露到公网**。

## 约定

- 请求与响应均为 `application/json; charset=utf-8`（SSE 除外）；
- 时间戳字段统一为毫秒（`ts`），耗时字段统一为秒（`elapsed`）；
- 业务失败用 HTTP 状态码表达（如任务重复启动返回 `409`），响应体仍是 JSON；
- 连接测试类接口在失败时返回 `200` + `{"ok": false, "error": "..."}`，便于前端分别渲染指示灯与文案。

---

## GET /

返回单页前端 `index.html`。

```
GET /
200 text/html
```

## GET /api/config

读取当前配置（`config.yaml` 的内存副本）。

```json
{
  "connections": [{"id": "cfg1", "name": "本地", "host": "127.0.0.1", "port": 6667,
                   "user": "root", "password": "root", "database": "root.cm"}],
  "active_connection": "cfg1",
  "prefix": {"station": "cm", "zone": "z01", "signal": "yc", "order": "reverse"},
  "groups": {"BMS": [{"key": "cu", "name": "簇", "range": "c001-c074",
                      "measures": ["totv", "soc"]}]},
  "pcs_sub": false,
  "rules": [{"cat": "电压", "min": 3.0, "max": 4.2, "unit": "V",
             "model": "walk", "match": "v*, *vol", "period": 86400}],
  "write": {"interval_ms": 1000, "mode": "realtime", "batch": 100,
            "points_per_device": 10, "history_start": "", "rate_limit": 0,
            "threads": 4, "dry_run": false}
}
```

## POST /api/config

整体替换配置并原子落盘。

```json
{ "ok": true }
```

缺失字段用默认值补齐；`points_per_device` 传 `null` / `""` / `0` 均归一为"持续写入"。

```bash
curl -X POST http://127.0.0.1:8000/api/config \
  -H 'Content-Type: application/json' \
  -d @config.json
```

## POST /api/test

连接探测。三种用法：

| 用法 | 请求体 | 说明 |
|---|---|---|
| 测当前连接 | `{}` | 使用 `active_connection` 指向的配置；`dry_run` 时直接按模拟通过返回 |
| 测指定配置 | `{"host": "...", "port": 6667, "user": "...", "password": "..."}` | 连接列表逐条检测用 |
| 静默探测 | `{"quiet": true}` | 不写日志，供顶栏指示灯心跳使用 |

可选 `timeout_ms`（默认 `3000`，范围 `500–30000`）、`name`（日志里的展示名）。

```json
{ "ok": true, "latency_ms": 12, "endpoint": "127.0.0.1:6667" }
```

失败：

```json
{ "ok": false, "error": "连接超时（>3000ms）", "latency_ms": 3001, "endpoint": "10.0.0.9:6667" }
```

`dry_run` 下的模拟通过还会带 `"mock": true`。

## GET /api/scale

规模统计。前端"规模估算"与页头摘要都取自这里。

```json
{
  "counts": {"d": 20, "c": 80, "cu": 960, "p": 3840, "m": 15360,
             "pcs": 1904, "pcsd": 0, "xb": 4},
  "groups": {"BMS": 659060, "PCS": 22848, "XB": 52},
  "devices": 22168,
  "writable_devices": 18328,
  "series": 681960,
  "points_per_cycle": 681960,
  "targets": ["BMS", "PCS", "XB"],
  "target_groups": {"BMS": {"series": 659060, "devices": 16420},
                    "PCS": {"series": 22848, "devices": 1904},
                    "XB":  {"series": 52,    "devices": 4}},
  "target_series": 681960,
  "target_devices": 18328
}
```

| 字段 | 含义 |
|---|---|
| `counts` | 各层节点数；`pcsd` 在 `pcs_sub=false` 时为 0 |
| `groups` | 各组序列数 = 节点数 × 该层测点数（整站口径） |
| `devices` | 全部节点数（含没配测点的层） |
| `writable_devices` | 实际会写入的设备数（只算挂了测点的层），**整站口径** |
| `series` | 总时间序列数（整站口径） |
| `targets` | 本次写入对象，来自 `write.targets` |
| `target_groups` | 各分组的序列数与可写设备数，供前端在勾选框里显示 |
| `target_series` / `target_devices` | **只统计写入对象**的序列数与设备数，也是写入任务的进度分母 |

`series` / `writable_devices` 始终是整站数字，`target_*` 才受 `write.targets` 影响；两者同时返回，前端才能显示"已排除箱变 52 序列 / 4 台设备"这类对照。

## POST /api/run

启动写入任务（后台线程）。

```json
{ "ok": true }
```

已在运行时返回 `409`：

```json
{ "ok": false, "error": "任务已在运行" }
```

写入范围取自 `config.write.targets`（`BMS` / `PCS` / `XB` 的任意组合），
请求体不接受覆盖——避免"界面勾了 BMS、接口却写了全站"这类对不上的情况。
若所选分组下没有可写设备，任务线程会在**连接数据库之前**失败，日志里是一条
`ERROR 写入失败：RuntimeError: 写入对象下没有可写设备：请检查所选分组（PCS）…`。

## POST /api/stop

请求停止。实际在**当前批次写完**后中断。

```json
{ "ok": true }
```

无任务时返回 `{"ok": false, "error": "当前无运行任务"}`。

## GET /api/status

运行状态快照，页面刷新后据此恢复按钮状态。

```json
{
  "running": false,
  "phase": "running",
  "state": "done",
  "result": {"series": 202541, "devices": 5038, "points": 2025410, "elapsed": 1.33},
  "progress": {"pct": 100, "devices": 5038, "total_devices": 5038, "rate": 1522800}
}
```

`state` 取值：`idle` / `running` / `done` / `stopped` / `error`。

## GET /api/logs/stream

SSE 日志流。连接建立后先推一条 `status`，随后持续推送事件。

```
data: {"type":"status","data":{"running":true,"state":"running",...}}

data: {"type":"log","level":"INFO","message":"正在生成 5,038 台设备...","ts":1763558400.12}

data: {"type":"progress","phase":"running","pct":62.4,"devices":3140,"total_devices":5038,
       "remaining_devices":1898,"points":628000,"total_points":1007600,
       "remaining_points":379600,"rate":512000,"elapsed":1.2,"ts":1763558400.9}

data: {"type":"state","state":"done","ts":1763558401.4}
```

| 事件 | 说明 |
|---|---|
| `status` | 连接建立时的状态快照 |
| `log` | `level` 为 `INFO` / `WARN` / `ERROR` / `SUCCESS` |
| `progress` | `phase` 为 `building`（构造数据，0–50%）、`running`（写入，50–100%）、`streaming`（持续写入，带 `round`）、`done`（终态） |
| `state` | 运行状态变更 |

每个连接有独立队列，多标签页同时打开日志完整、不跳号。

```bash
curl -N http://127.0.0.1:8000/api/logs/stream
```

## GET /api/health

```json
{ "ok": true, "version": "1.0" }
```

## 错误一览

| 状态码 | 场景 |
|---|---|
| `409` | 任务已在运行 |
| `500` | 写入线程内未捕获异常（错误会先以 `ERROR` 日志推送，再置 `state=error`） |

写入失败不会让接口报错——`/api/run` 只负责起线程，失败通过 SSE 的 `ERROR` 日志与 `state=error` 表达。
