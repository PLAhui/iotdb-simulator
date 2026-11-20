# 开发指南

## 1. 环境准备

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`apache-iotdb` 客户端只在真实写入时需要；只做界面与解析逻辑开发时，`dry_run` 足以覆盖全流程。

## 2. 启动

```bash
./start.sh              # 8000 端口
./start.sh 8791         # 指定端口
python3 -m uvicorn server.app:app --host 127.0.0.1 --port 8791 --reload   # 后端热重载
```

`--reload` 只监听 Python 文件；`index.html` 本来就是每次请求现读，浏览器刷新即可看到改动，不需要重启。

## 3. 开发循环

| 改动 | 生效方式 |
|---|---|
| `index.html`（样式 / 逻辑） | 浏览器刷新 |
| `server/*.py` | 启了 `--reload` 自动重载；否则手动重启 |
| `config.yaml` | 网页里保存会立即生效；手改文件需重启或重新保存一次 |

改完界面建议按这条路径自检：改一个编码范围 → 看规模四格是否变 → 看路径预览是否同步 → 展开一层加减测点 → 切到生成页确认规模 → 跑一次 `dry_run`。

## 4. 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_parser.py` | 三种范围写法、补零宽度、字母区间、非法输入、测点区间展开与计数 |
| `tests/test_engine.py` | 节点数相乘、PCS 子层开关、`writable_devices` 只算有测点的层、路径顺序 `forward/reverse` |
| `tests/test_rules.py` | 匹配优先级（`t*` 不抢 `totp`、`*de` 不抢 `workmode`）、`yc/yx` 类型分支、六种模型值域 |

测试全部是纯函数级，不需要 IoTDB，也不需要起服务。

## 5. dry-run 自检

把「写入设置 → dry-run」打开（或 `write.dry_run: true`），点「生成并写入」。日志应依次出现：

```
连接配置「本地」· 127.0.0.1:6667 · 目标库 root.cm
信号类型 yc → 全部测点 FLOAT
整站规模：202,541 序列 / 5,038 待写设备 / 预计 2,025,410 个数据点
dry-run 模式：不连接真实 IoTDB，仅模拟完整写入流程
数据库 root.cm 就绪（CREATE DATABASE root.cm）
写入进度 100.0% · 设备 5,038/5,038 · 点 2,025,410/2,025,410
写入完成 · 2,025,410 点 / 5,038 设备 · 耗时 x.xx s（dry-run）
```

若"待写设备"与页面估算不一致，先查该层 `measures` 是否为空、范围是否合法。

## 6. 调试

**看 SSE 原始事件**

```bash
curl -N http://127.0.0.1:8000/api/logs/stream
```

**确认规模算法**

```bash
python3 - <<'EOF'
from pathlib import Path
from server.config import load_config
from server.engine import scale
print(scale(load_config(Path('config.yaml'))))
EOF
```

**核对前端与后端解析是否一致**

在浏览器控制台输入一段范围，与后端结果对比：

```js
console.log(parseRange('c101a-c101d'))
```

```bash
python3 -c "from server.parser import parse_range; print(parse_range('c101a-c101d'))"
```

**只跑一台设备验证 Tablet 形状**

把 `groups.BMS` 的 `range` 全改成单值（如 `d0001` / `c101` / `c001` / `p01` / `m01`），开 `dry_run` 跑一次，确认日志无 `行数不匹配` / `测点值数量不匹配`。

## 7. 已知坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 空态和详情同时出现 | `[hidden]` 被 `display:flex` 覆盖 | 补 `[hidden]{display:none!important}` |
| 面板偶发闪出滚动条 | 亚像素溢出（1px） | `fitPanel()` 里差值 ≤2px 直接 `overflow-y:hidden` |
| 预览卡高度多出 25px | 媒体查询 `.card{padding}` 盖掉 `.pv-card{padding:0}` | 用 `.card.pv-card` 提高优先级 |
| 单选按钮竖排 | flex 收缩 | `.radio{white-space:nowrap;flex:0 0 auto}` |
| 吸顶标签栏透出内容 | 半透明背景 | 实底色 + `.stuck` 细阴影 |
| 日志跳号 / 进度缺段 | 多消费者抢同一个全局队列 | 每连接独立队列广播（已修，勿回退） |
| 真库报 `len(timestamps) does not equal to len(values)` | Tablet 写成了列优先 | 保持 `values[行][列]` |
| 进度永远到不了 100% | 分母用了全部节点数 | 用 `writable_devices` |
| 改 `yc→yx` 后类型冲突 | auto-schema 不改已存在序列的类型 | 删序列或换目标库 |
| 测试污染用户配置 | 功能测试会 `POST /api/config` | 测完回滚，或先备份 `config.yaml` |

## 8. 性能验证

粗测写入吞吐（真库）：

```bash
time curl -s -X POST http://127.0.0.1:8000/api/run
# 观察底部控制台的速率与耗时
```

关注三个指标：点/秒、每批耗时是否稳定、`--reload` 模式下是否因日志过多拖慢。判据：5,000 台设备、约 20 万点应在 2 秒量级完成；若明显变慢，先看 `batch` 是否过小（RPC 往返占比升高）。

前端布局验证建议在 1440×900、1440×800、1280×720、1024×768 四个视口各看一遍，确认：整页无滚动、无横向溢出、层级行不换行错位、控制台展开不遮挡内容。

## 9. 发布检查

- [ ] `python3 -m unittest discover -s tests -t .` 全绿
- [ ] `dry_run` 跑通，日志无异常
- [ ] 版本号同步三处：`index.html` 的 `.ver`、`server/app.py` 的 `FastAPI(title=...)` 旁注释、`CHANGELOG.md`
- [ ] 更新 `CHANGELOG.md` 的 `Unreleased` 段
- [ ] 确认没有提交 `config.yaml`、真实地址、口令
