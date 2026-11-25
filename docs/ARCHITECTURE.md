# 架构设计

## 1. 设计目标

| 目标 | 具体含义 |
|---|---|
| 规模够大 | 单机跑满配储能站：约 68 万条序列 / 2.2 万设备，内存不随规模线性增长 |
| 配置够快 | 改一个编码范围，规模、路径预览、写入计划同时更新，不用重启 |
| 数据像真的 | 每个测点按名字匹配值域与变化模型，有连续性和物理意义 |
| 过程可控 | 写入前能算清规模，写入中能看到进度与速率，随时能停 |
| 部署够轻 | 一个 Python 进程 + 一个 HTML 文件，内网可用，不依赖 CDN 与前端构建 |

## 2. 模块划分

```
server/
├── app.py       HTTP 层：路由、SSE 广播、任务线程
├── config.py    配置层：dataclass 模型、YAML 读写、宽松解析
├── parser.py    语法层：编码范围与测点区间展开（前后端同构的权威实现）
├── engine.py    结构层：设备树惰性遍历、节点计数、规模统计
├── rules.py     数值层：规则匹配、六种变化模型、类型驱动、异常注入
└── writer.py    传输层：Tablet 构造、批量提交、限速、进度、中断
```

依赖方向严格单向：`app → writer → engine → parser/config`，`rules` 只依赖 `config/parser`。没有循环导入，也没有全局可变状态跨模块传递（除 `app.cfg` 这个配置单例）。

| 模块 | 关键函数 | 职责边界 |
|---|---|---|
| `config.py` | `config_from_dict` / `save_config` | 只负责"结构对不对"，不做业务校验 |
| `parser.py` | `parse_range` / `parse_measure` / `expand_measure` | 纯函数，无副作用，是前后端一致性的基准 |
| `engine.py` | `iter_devices` / `node_counts` / `scale` | 只描述"有哪些设备、各多少"，不碰数值 |
| `rules.py` | `RuleMatcher.match` / `Generator.value_for` | 只回答"这个测点该出什么值" |
| `writer.py` | `Writer.run` / `_make_tablet` / `_flush` | 只负责"把值送进库"，不决定值 |

## 3. 数据流

```
config.yaml
   │  load_config（缺失或损坏 → default_config）
   ▼
AppConfig ──┬─→ scale() ──────────────→ /api/scale ──→ 前端规模估算
            │                              ▲
            │                              └─ 前端 parseRange 同构实现（实时预览，不等后端）
            └─→ iter_devices(targets) ──→ (路径, 分组, 层级, 测点列表)
                  ↑ 只展开 write.targets 里的分组（BMS / PCS / XB）
                                       │
                                       ▼
                                 Generator.value_for（规则匹配 → 模型计算）
                                       │
                                       ▼
                                 Tablet(device, measurements, types, rows, timestamps)
                                       │
                                       ▼
                                 insertTablets（每 batch 台设备一次 RPC）
                                       │
                                       ▼
                                 Apache IoTDB（auto-schema 自动建序列）
```

写线程只做一件事：遍历 → 生成 → 打包 → 提交。任何"描述性"的工作（规模、路径样例、诊断表）都由前端用同一套语法规则即时算，避免每次敲键盘都发请求。

## 4. 关键设计决策

### 4.1 设备树惰性遍历，而不是先建树

**问题**：满配 2.2 万设备、68 万序列，如果先把设备树建成列表/字典，光是对象开销就上百 MB，而且没有任何收益。

**方案**：`iter_devices()` 是生成器，`writer` 按 `batch` 消费，缓冲区最多 `batch` 个 Tablet。

**代价**：只能遍历一次。所以持续写入模式里有 `devices = list(iter_devices(cfg))`——每轮都要重放整棵树，必须落地成列表；定量写入模式则始终保持流式。

### 4.2 Tablet 行优先

**问题**：`apache-iotdb` 的 `Tablet` 要求 `values[row][col]`，行 = 时间戳、列 = 测点，且构造时校验 `len(values) == len(timestamps)`。写成列优先在 `dry_run` 下完全正常（因为只是元组），接真库立刻报 `len(timestamps) does not equal to len(values)`。

**方案**：外层循环时间戳、内层循环测点，同时只在第一行收集 `data_types`；`dry_run` 的占位元组也保持同样的形状，让 `_tablet_size` 与 `MockSession` 走同一套逻辑。

**代价**：需要一次显式的行数/列数校验（两次 `RuntimeError`），但这比线上静默丢数据便宜得多。

### 4.3 依赖服务端 auto-schema

**问题**：68 万条序列逐条 `CREATE TIMESERIES` 会产生 68 万次 RPC，几十分钟起步。

**方案**：只建库，序列由 IoTDB 在首次写入时按 Tablet 的 `data_types` 自动创建。

**代价**：类型由首次写入决定，之后改 `yc/yx` 需要手动删序列，否则类型冲突。这是文档里必须写明的限制。

### 4.4 建库双语法回退

不同版本对 `CREATE DATABASE` 的支持不一致：1.x/2.x tree 模式用 `CREATE DATABASE`，0.13 用 `CREATE STORAGE GROUP`，部分版本不接受 `IF NOT EXISTS`。

**方案**：按候选语法依次尝试；错误信息含 `already exists / already created / repeatedly` 视为成功；只有 `mismatched input / no viable alternative / syntax` 这类**语法错误**才换下一种；权限、网络错误直接抛出，不吞。

**代价**：错误分类依赖字符串匹配，换版本可能要补关键词——所以分类逻辑集中在 `_ensure_database` 一个函数里。

### 4.5 每连接一条 SSE 队列

**问题**：早期是"一个全局 `queue.Queue` + 每个连接各自 `get_nowait()`"。只要同时存在两个消费者（多开标签页、EventSource 重连残留、右侧预览面板），日志就会被互相抢走——表现是轮次跳号、进度缺段，看起来像后端漏发。

**方案**：发布/订阅。`push()` 把同一事件写入所有订阅者队列；慢消费者队列满时丢旧事件，绝不阻塞写线程。

**代价**：每个连接有一份事件副本（最多 8000 条），换来日志完整与写线程不被拖慢。

### 4.6 进度两段式

**问题**：大点数场景（每设备 1000 点）下，"构造数据"可能耗时数秒，此间若进度纹丝不动，用户会以为卡死。

**方案**：构造阶段占 0–50%，写入阶段占 50–100%，两段单调不回退；构造阶段按 120ms 节流上报，写入阶段每批都上报（只对日志做 1s 节流）。

**代价**：进度百分比不是"真实完成度"，只是阶段指示；因此终态统一补推一条 `phase=done` 事件，保证短任务也能收敛到 100%。

### 4.7 进度分母用 writable_devices

`scale()['devices']` 统计全部节点（含没有测点的层），而写入只产出"挂了测点的层"的设备。用它作分母会导致进度永远到不了 100%、"待写设备"数偏高。

**方案**：`writable_device_count()` 只累加 `measure_count > 0` 的层，与 `iter_devices` 的实际产出严格一致。

### 4.8 写入对象在生成器层过滤

"只写 BMS / 只写箱变"如果放在 `Tablet` 组装之后过滤，`_make_tablet` 仍会为不写的设备生成全部测点值——68 万序列白白算一遍。

**方案**：过滤放在 `iter_devices(cfg, targets)` 的循环入口，三个分支各自判一次 `"BMS" in allow` / `"PCS" in allow` / `"XB" in allow`，被排除的分组连生成器都不会进入。`writable_device_count()` 与 `scale()['target_series' / 'target_devices']` 用同一份 `write_targets(cfg)`，保证进度分母与产出严格一致。

**取舍**：`scale()` 同时返回整站口径（`series` / `writable_devices`）与写入对象口径（`target_*`），前端才能显示"已排除箱变 52 序列 / 4 台设备"。空选择按"全部"兜底，宁可多写也不要静默什么都不写。

### 4.9 前后端解析同构

前端 `parseRange` / `parseMeasure` 与后端 `parser.py` 是两份实现，必须给出**完全相同**的结果，否则会出现"预览显示 74 台、实际写 20 台"这类静默偏差。

**约定**：先规范化连字符两侧空格，再按逗号/中文逗号/空白切分；数值区间要求前缀相同、按起始宽度补零；字母区间要求前缀相同、末尾单字母递增；展开后每个编码必须"含数字且只含字母数字"，否则整体判非法。改动任何一侧，都要同步另一侧与 `tests/test_parser.py`。

### 4.10 子设备层是「分组里的额外层级」，不建独立树

**问题**：PCS 与箱变都可能需要再挂子设备（`pcs0101.d01` / `35packtran101.byr01`），箱变的子设备还不止一种（变压器室、低压室，甚至高压室）。如果在 `groups` 里为每种结构塞一套并行定义，`iter_devices`、`node_counts`、前端预览都要分叉。

**约定**：`groups[g]` 的**第一层是设备本体，之后每一层都是挂在它下面的子设备层**。子层互相是兄弟，不参与 BMS 那套相乘关系：每个子层的节点数 = 本体数 × 该层范围。层数与命名完全由配置决定，代码里不出现 `byr` / `lvr` 这类字面量。

**开关**：`pcs_sub` 是布尔（PCS 只有 `pcsd` 一层）；箱变用 `xb_subs` 列表，列出即启用，可全不选、全选或只选其中几层。未启用的层 `parsed()` 返回空列表、`counts` 记 0、生成器不进入该层，序列数与设备数跟没有这层时完全相同。旧字段 `xb_sub: true` 在解析时展开为「启用配置里定义的全部子层」，保证老配置语义不变。

**兼容**：新增层级在旧 `config.yaml` 里不存在，`config_from_dict()` 按 `key` 从 `default_config()` 补齐；前端 `loadConfig()` 同样用启动时的 `GROUP_DEFAULTS` 快照补齐，避免"配置文件旧 → 界面看不到新层 → 无法开启"的死角。上一版未发布的通用子层 `xbd` 已由具名层取代，配置里只存在 `xbd` 时会在解析阶段被迁移掉，避免与新层重复产出设备。

## 5. 并发模型

- FastAPI 单进程 + 一个写入线程（`threading.Thread(daemon=True)`），同一时刻只允许一个任务（重复启动返回 409）；
- `stop_event` 是唯一的跨线程信号，只在**批次之间**检查，保证不产生半批数据；
- `cfg` 是模块级单例，`POST /api/config` 整体替换引用（不是原地修改），写线程持有启动时的快照，因此"运行中改配置"不会污染当前任务；
- SSE 生成器异步读取线程安全的 `queue.Queue`，`/api/test` 的连接探测用 `asyncio.to_thread` 包住阻塞调用，并额外加 `timeout_ms + 1.5s` 的兜底超时（客户端库自身的超时不可靠）。

## 6. 性能基线

| 场景 | 规模 | 结果 |
|---|---|---|
| 默认配置（仅估算） | 681,960 序列 / 22,168 设备 | `scale()` 毫秒级返回 |
| 真库写入 | 202,541 点 / 5,038 设备 / 每设备 1 个时间点 | 1.33 s |
| 持续写入 | 5,038 设备 × 1 点/轮，1000ms 周期 | 每轮稳定完成，无堆积 |
| 界面渲染 | 5 个层级行 + 折叠诊断表 | 1440×900 面板 659px，整页零滚动 |

瓶颈在 RPC 往返而非生成：生成一条 FLOAT 只是一次 `random` 或 `sin`，成本可忽略。

## 7. 扩展点

- **并发写入**：`WriteConfig.threads` 已预留。按设备路径哈希分片，每片一个 Session + 独立缓冲，注意 Tablet 顺序与停止信号的协同。
- **异常注入**：`AnomalyInjector` 已实现（按比例乘系数），只需在 `Writer.__init__` 读取配置并接到界面开关。
- **table 模型**：`config.py` 增加 `model: tree|table`，`writer` 换成 `insertRelationalTablet`，`engine` 的设备路径改为 `(table, tags, fields)` 三元组。
- **配置快照**：`config.yaml` 已有原子写入（`.tmp` + `replace`），加一个 `configs/` 目录按时间戳存快照即可支持回滚。
