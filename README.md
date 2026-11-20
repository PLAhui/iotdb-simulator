# IoTDB 数据模拟器

**面向储能电站的可视化时序数据模拟工作台**

用网页配置设备层级与测点，按可调规则批量生成数据并写入 Apache IoTDB；带实时规模核算、写入进度与服务端日志。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python\&logoColor=white)

![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi\&logoColor=white)

![IoTDB](https://img.shields.io/badge/Apache%20IoTDB-1.x-1f6feb)

![License](https://img.shields.io/badge/License-MIT-3da639)

![Version](https://img.shields.io/badge/version-1.2.0-4f46e5)

---

## 简介

IoTDB 模拟工具通常是一段脚本：改代码、跑命令、看日志。本项目把它做成一个本地网页工作台，解决三件事：

1. **路径不用手写**——按「堆 / 舱 / 簇 / 包 / 模组」五层结构配编码范围，路径自动拼接，改一个数字就能把 20 台设备变成 2,000 台；
2. **数据像真的**——每个测点按通配规则匹配值域与变化模型（随机游走、正弦、单调累加、状态位、常量、翻转），而不是一堆随机数；
3. **写入过程可见**——规模先算清楚，写入有进度、有速率、能停止，日志走 SSE 实时推到页面。

默认配置（20 堆 × 4 舱 × 12 簇 × 4 包 × 4 模组 + PCS + 箱变）对应 **681,960 条时间序列 / 22,168 台设备**。实测真库单轮写入 202,541 点（5,038 台设备各 1 个时间点）耗时 **1.33 秒**。

## 功能特性

| 能力      | 说明                                                          |
| ------- | ----------------------------------------------------------- |
| 多连接管理   | 多套 IoTDB 连接可增删切换，逐条连通指示灯、延迟统计、连接诊断四格                        |
| 路径自由拼装  | `root.{场站}.{站区}.{信号}` 前缀 + 五层编码；`yc` 全 FLOAT、`yx` 全 BOOLEAN |
| 范围三种写法  | 数值连续 `d0001-d0020`、尾缀连续 `c101a-c101d`、逐项列举 `c101,c201`      |
| 表格式测点配置 | 通栏表单，一层一行；编码范围输入框是该行最宽元素，点行展开编辑测点，支持整列粘贴（逗号 / 空格 / 换行）与一键清空 |
| 规则引擎    | 通配匹配 + 特异性排序，六种变化模型，避免 `t*` 抢走 `totp`                       |
| 规模实时核算  | 改任何范围，序列数 / 设备数 / 待写设备数即时更新，前后端算法严格一致                       |
| 高性能写入   | Tablet 批量提交 + 服务端 auto-schema，惰性遍历设备，68 万序列不 OOM            |
| 两种写入模式  | 定量写入（每设备 N 点）与持续写入（留空即每周期一轮，直到手动停止）                         |
| 写入对象可选  | BMS / PCS / 箱变 可只写其一或任意组合，序列数、设备数、进度分母同步收窄                 |
| 安全自检    | `dry_run` 走完整流程但不连真库，无 IoTDB 环境也能验证                         |
| 可观测     | 底部控制台：进度条 + 分级日志（SSE），多标签页同时打开日志不跳号                         |

## 界面预览

| 数据库管理 | 路径与结构 | 生成与预览 |
| ----- | ----- | ----- |
| 数据库管理 | 路径与结构 | 生成与预览 |

## 架构

```
┌──────────────────────────── 浏览器（单页 index.html） ─────────────────────────────┐
│  数据库管理 │ 路径与结构 │ 模拟规则 │ 生成与预览                                   │
│  底部控制台：进度条 + 分级日志（SSE）                                              │
└───────────────┬────────────────────────────────────────────┬───────────────────────┘
                │ REST /api/*                                 │ SSE /api/logs/stream
┌───────────────▼────────────────────────────────────────────▼───────────────────────┐
│                              FastAPI（server/app.py）                              │
│  配置读写 · 连接探测 · 规模统计 · 任务控制（线程）                                 │
├─────────────┬────────────────┬─────────────────┬────────────────┬──────────────────┤
│  config.py  │   parser.py    │    engine.py    │    rules.py    │    writer.py     │
│  YAML 模型  │ 范围/测点展开  │ 设备树惰性遍历  │ 规则匹配+生成  │ Tablet 批量写入  │
└─────────────┴────────────────┴─────────────────┴────────────────┴──────────────────┘
                                                          │
                                                ┌─────────▼────────┐
                                                │ Apache IoTDB 1.x │
                                                └──────────────────┘
```

数据流只有一条主线：`config.yaml` → 设备树展开 → 每条序列按规则生成值 → 打包成 Tablet → 批量写入。前端与后端共用同一套范围解析与规模核算算法，所以页面上看到的数量就是真正会被写入的数量。

详细设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 快速开始

### 环境要求

- Python 3.10+
- Apache IoTDB 1.x（默认 `127.0.0.1:6667`，用户/密码 `root/root`）
- 不需要 Node.js、不需要前端构建

> 没有可用 IoTDB 也可以先跑：把「写入设置 → dry-run」打开，全流程照样走通。

### 安装与启动

```bash
git clone https://github.com/PLAhui/iotdb-simulator.git
cd iotdb-simulator

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

./start.sh          # 默认 8000 端口
./start.sh 8791     # 或指定端口
```

浏览器打开 `http://127.0.0.1:8000`。首次启动若没有 `config.yaml`，会自动使用内置默认配置。

### 使用流程

1. **数据库管理**——新增连接、填 Host/Port/User/Password/目标库，点「测试连接」确认绿灯；
2. **路径与结构**——填场站/站区、选信号类型与堆舱顺序，逐层填编码范围与测点；
3. **模拟规则**——按需调整值域、单位、变化模型与匹配式；
4. **生成与预览**——先勾选写入对象（BMS / PCS / 箱变，默认全写），核对规模估算，设置采样周期与每设备点数，点「生成并写入」，在底部控制台看进度与日志。

## 配置说明

配置落在项目根目录的 `config.yaml`（首次运行自动生成，**不建议提交到仓库**）。示例见 [examples/config.example.yaml](examples/config.example.yaml)，字段详解见 [docs/CONFIG.md](docs/CONFIG.md)。

```yaml
prefix: {station: cm, zone: z01, signal: yc, order: reverse}
groups:
  BMS:
    - {key: d,  name: 堆,   range: d0001-d0002, measures: []}
    - {key: c,  name: 舱,   range: c101-c102,   measures: [soh, rsv01-rsv07]}
    - {key: cu, name: 簇,   range: c001-c074,   measures: [totv, soc]}
write:
  interval_ms: 1000
  points_per_device: 10     # 留空 = 持续写入
  batch: 100
  dry_run: false
```

### 路径与编码规则速查

| 规则     | 说明                                             |
| ------ | ---------------------------------------------- |
| BMS 路径 | `root.cm.z01.yc.d0001.c101.c001.p01.m01`（五层嵌套） |
| 堆舱顺序   | `forward` = `d.c`，`reverse` = `c.d`，只影响这两层     |
| PCS 路径 | `root.cm.z01.yc.pcs0101`，开启子设备层后追加 `.d01`      |
| 箱变路径   | `root.cm.z01.yc.35packtran101`                 |
| 节点数    | `d × c × cu × p × m` 相乘；PCS、箱变各自独立             |
| 测点区间   | `v001-v099`、`rsv01-rsv07`，前缀与位数必须一致            |

## 项目结构

```
iotdb_simulator/
├── server/
│   ├── app.py            FastAPI 入口：配置 API、任务控制、SSE、页面托管
│   ├── config.py         配置数据类与 YAML 读写（宽松解析 + 默认值补齐）
│   ├── parser.py         编码范围解析与测点展开
│   ├── engine.py         设备树惰性遍历与规模统计
│   ├── rules.py          规则匹配、六种变化模型、异常注入
│   └── writer.py         Tablet 批量写入、限速、进度
├── index.html            单页前端（样式与交互全在一个文件，无需构建）
├── docs/                 架构 / 配置 / API / 界面 / 开发 / 提示词
├── examples/             配置示例
├── tests/                单元测试（unittest，无额外依赖）
├── start.sh              启动脚本
├── requirements.txt      运行依赖
├── CHANGELOG.md          版本记录
├── CONTRIBUTING.md       贡献指南
└── LICENSE
```

## 开发与测试

```bash
python3 -m unittest discover -s tests -t . -v
```

测试覆盖范围解析、测点展开、规模统计、规则匹配优先级与 `yc/yx` 类型分支，均不依赖真实 IoTDB。

本地调试技巧、常见坑与日志排查见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

## 文档

| 文档                                           | 内容                             |
| -------------------------------------------- | ------------------------------ |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 模块职责、数据流、性能与并发设计               |
| [docs/CONFIG.md](docs/CONFIG.md)             | `config.yaml` 全字段、路径与编码规则、常见错误 |
| [docs/API.md](docs/API.md)                   | HTTP / SSE 接口契约与请求示例           |
| [docs/UI.md](docs/UI.md)                     | 页面结构、交互约定、视觉与响应式规范             |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)   | 本地开发、测试、调试、已知坑                 |
| [docs/PROMPT.md](docs/PROMPT.md)             | 完整的开发提示词（可用于复刻本项目）             |

## 已知限制

- 写入线程数 `threads` 目前未并发使用，写入按批串行提交；
- 异常注入（`AnomalyInjector`）默认关闭，尚未接入界面；
- 仅适配物联网 tree 模型，未支持 IoTDB 2.x 的 table 模型；
- 界面为桌面端设计，窄屏（<820px）退化为整页滚动，移动端未做专门适配。

## Roadmap

- [ ] 写入并发化（按设备分片多线程提交）
- [ ] 异常注入与数据质量开关接入界面
- [ ] 配置导入 / 导出与多套方案快照
- [ ] 写入前预检（序列冲突、类型不一致提示）
- [ ] 单元测试覆盖到 writer 的 Tablet 构造

## 贡献

欢迎提 Issue 与 PR。提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，并确保 `python3 -m unittest discover -s tests -t .` 全绿。

## License

[MIT](LICENSE)
