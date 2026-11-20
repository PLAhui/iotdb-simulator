"""IoTDB 批量写入器。

设计要点（针对整站 68 万序列）：
  1. 惰性消费设备生成器，按 batch 组装 Tablet，内存占用恒定
  2. insertTablets 一次提交多设备，减少 RPC 往返
  3. 依赖 IoTDB 服务端 auto-schema 自动建序列（避免 68 万次显式建序列）
  4. stop_event 可在批次间中断；rate_limit 按比例限速
  5. dry_run 提供模拟会话，端到端自检无需真实 IoTDB
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from .config import AppConfig, Connection
from .engine import iter_devices, scale, write_targets
from .rules import AnomalyInjector, Generator, RuleMatcher, PointValue

LogFn = Callable[[str, str], None]

# 日志里用的中文名（与界面上的分组名一致）
GROUP_LABEL = {"BMS": "BMS", "PCS": "PCS", "XB": "箱变"}


def _import_iotdb():
    """兼容 IoTDB 1.x 与 2.x 的 Tablet 路径。"""
    from iotdb.Session import Session
    from iotdb.utils.IoTDBConstants import TSDataType
    try:
        from iotdb.utils.Tablet import Tablet  # 2.x
    except ImportError:
        from iotdb.Tablet import Tablet  # 1.x
    return Session, Tablet, TSDataType


def _tablet_size(tablet) -> int:
    """取单个 Tablet 的点数（时间戳数 × 测点数），兼容 1.x/2.x 属性差异。"""
    if isinstance(tablet, tuple):  # dry-run 的占位元组: (device, measures, dtypes, values, timestamps)
        return len(tablet[4]) * len(tablet[1])
    if hasattr(tablet, "get_row_number"):  # 2.x
        return tablet.get_row_number() * len(tablet.get_measurements())
    ts = getattr(tablet, "timestamps", None)
    ms = getattr(tablet, "measurements", None)
    return len(ts or []) * len(ms or [])


class MockSession:
    """dry_run 用的模拟会话：接口与真实 Session 对齐，仅统计不落库。"""

    def __init__(self):
        self.inserted_devices = 0
        self.inserted_points = 0
        self.opened = False

    def open(self, *a, **k):
        self.opened = True

    def close(self):
        pass

    def execute_non_query_statement(self, sql: str):
        pass

    def insert_tablets(self, tablets):
        for t in tablets:
            self.inserted_devices += 1
            self.inserted_points += len(t[4]) * len(t[1])

    def insertTablets(self, tablets):  # 兼容旧版 API
        self.insert_tablets(tablets)


class Writer:
    def __init__(self, cfg: AppConfig, log: LogFn, stop_event: threading.Event,
                 progress: Optional[Callable[[dict], None]] = None):
        self.cfg = cfg
        self.log = log
        self.stop_event = stop_event
        self.progress = progress
        self.matcher = RuleMatcher(cfg.rules)
        # 写入对象：只生成并写入选中的设备大类（BMS / PCS / 箱变 的任意组合）
        self.targets = write_targets(cfg)
        self.generator = Generator(cfg, self.matcher)
        self.anomaly = AnomalyInjector(False)
        self.session = None
        self.Tablet = None
        self.TSDataType = None
        self.mock = cfg.write.dry_run
        # 进度总数：由 run() 按实际规模初始化，供每批进度事件携带"待写入"字段
        self.total_devices = 0
        self.total_points = 0

    def _connect(self, db: Connection):
        if self.mock:
            self.session = MockSession()
            self.log("WARN", "dry-run 模式：不连接真实 IoTDB，仅模拟完整写入流程")
        else:
            Session, Tablet, TSDataType = _import_iotdb()
            self.Tablet, self.TSDataType = Tablet, TSDataType
            self.session = Session(db.host, db.port, db.user, db.password)
            self.session.open(False)
        # 兼容两套命名
        if hasattr(self.session, "execute_non_query_statement"):
            exec_sql = self.session.execute_non_query_statement
        else:
            exec_sql = self.session.executeNonQueryStatement
        database = db.database or "root.cm"
        self._ensure_database(exec_sql, database)
        self.log("INFO", f"采用服务端 auto-schema 自动建序列"
                         f"（按需创建 {getattr(self, '_total_series', 0):,} 条序列，无需逐条 CREATE TIMESERIES）")

    def _ensure_database(self, exec_sql, database: str):
        """创建数据库，兼容不同 IoTDB 版本的建库语法。

        版本差异：
          - 1.x / 2.x tree 模式：CREATE DATABASE root.cm（部分版本不接受 IF NOT EXISTS）
          - 0.13 及更早：CREATE STORAGE GROUP root.cm
        已存在时服务端报错，视为创建成功；仅语法不支持时才回退到另一种写法。
        权限/网络等非语法错误原样抛出，避免掩盖真实问题。
        """
        # 优先用标准写法，去掉 IF NOT EXISTS（该子句在部分版本解析失败）
        candidates = [
            f"CREATE DATABASE {database}",
            f"CREATE STORAGE GROUP {database}",
        ]
        last_err: Optional[Exception] = None
        for sql in candidates:
            try:
                exec_sql(sql)
                self.log("SUCCESS", f"数据库 {database} 就绪（{sql}）")
                return
            except Exception as e:  # noqa: BLE001 - 需按错误消息分类处理
                last_err = e
                msg = str(e).lower()
                # 数据库已存在 → 直接视为成功
                # 覆盖多种服务端文案：
                #   "already exists" / "already created" /
                #   "has already been created as database" / "repeatedly create"
                if ("already" in msg and ("exist" in msg or "creat" in msg)) \
                        or "repeatedly" in msg:
                    self.log("INFO", f"数据库 {database} 已存在，跳过创建")
                    self.log("SUCCESS", f"数据库 {database} 就绪")
                    return
                # 明确的语法不支持 → 尝试下一种写法
                if ("mismatched input" in msg or "no viable alternative" in msg
                        or "syntax" in msg or "recognizing" in msg):
                    self.log("WARN", f"建库写法不被支持，尝试备选：{sql}")
                    continue
                # 其他错误（权限/网络/连接）→ 不吞掉，原样抛出
                raise
        raise last_err  # type: ignore[misc]

    def _dtype(self, dtype: str):
        if self.TSDataType is None:
            return dtype  # mock 模式用字符串占位
        return self.TSDataType.BOOLEAN if dtype == "BOOLEAN" else self.TSDataType.FLOAT

    def _timestamps(self) -> List[int]:
        w = self.cfg.write
        n = max(w.points_per_device, 1)
        interval = max(w.interval_ms, 1)
        if w.mode == "history" and w.history_start:
            try:
                start = int(datetime.strptime(w.history_start, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
            except ValueError:
                start = int(time.time() * 1000) - n * interval
        else:
            start = int(time.time() * 1000) - (n - 1) * interval
        return [start + i * interval for i in range(n)]

    def _make_tablet(self, device: str, measures: List[str], timestamps: List[int]):
        """构造 IoTDB Tablet。

        apache-iotdb 的 Tablet 要求 values 为【行优先】二维列表：
          values[row][col] —— 行 = 时间戳，列 = 测点，与 measurements 顺序一致。
        客户端构造时会校验 len(timestamps) == len(values)，序列化时按
        self.__values[j][i] 取值。列优先写法在 dry-run 下不报错，接真实库必现
        "len(timestamps) does not equal to len(values)"。
        """
        rows: List[list] = []
        data_types: List = []
        for ts_idx, ts in enumerate(timestamps):
            points: List[PointValue] = self.generator.values_for(measures, ts, device)
            self.anomaly.apply(points)
            if len(points) != len(measures):
                raise RuntimeError(
                    f"测点值数量不匹配：{device} 期望 {len(measures)} 个，实际 {len(points)} 个"
                )
            row: list = []
            for pt in points:
                row.append(pt.value)
                if ts_idx == 0:  # 每列的类型只需确定一次
                    data_types.append(self._dtype(pt.dtype))
            rows.append(row)
        if len(rows) != len(timestamps):
            raise RuntimeError(f"行数不匹配：期望 {len(timestamps)}，实际 {len(rows)}")
        if self.Tablet is None:
            # dry-run 占位元组同样保持行优先，_tablet_size / MockSession 用 len(rows)*len(measures)
            return (device, measures, data_types, rows, timestamps)
        return self.Tablet(device, measures, data_types, rows, timestamps)

    def run(self) -> dict:
        s = scale(self.cfg)
        # 只统计选中的写入对象：进度分母、总点数、日志口径必须与 iter_devices 产出一致
        total_series = int(s.get("target_series", s["series"]))
        self._total_series = total_series
        # 分母用"有测点的设备数"，与 iter_devices 实际产出台数一致，进度才能收敛到 100%
        total_devices = int(s.get("target_devices", s.get("writable_devices", s["devices"])))
        self.total_devices = total_devices
        if total_devices <= 0:
            raise RuntimeError(
                "写入对象下没有可写设备：请检查所选分组（"
                + " / ".join(GROUP_LABEL.get(g, g) for g in self.targets)
                + "）是否配了测点、编码范围是否可解析"
            )
        w = self.cfg.write
        # points_per_device 为 None（前端留空）= 持续写入，不限制总点数
        streaming = w.points_per_device is None
        timestamps = [] if streaming else self._timestamps()
        if not streaming:
            # 总点数 = 序列总数 × 每设备时间点数（每个设备测点数不同，不能用设备数相乘）
            self.total_points = total_series * len(timestamps)

        db = self.cfg.active_db()
        self.log("INFO", f"连接配置「{db.name}」· {db.host}:{db.port} · 目标库 {db.database}")
        self.log("INFO", f"信号类型 {self.cfg.prefix.signal} → 全部测点 "
                         f"{'BOOLEAN（0/1）' if self.cfg.prefix.signal == 'yx' else 'FLOAT'}")
        # 写入对象：写明本次写什么、排除了什么，避免"为什么数据少了"的困惑
        picked = " + ".join(GROUP_LABEL.get(g, g) for g in self.targets)
        skipped = [GROUP_LABEL.get(g, g) for g in ("BMS", "PCS", "XB") if g not in self.targets]
        if skipped:
            self.log("INFO", f"写入对象：{picked}（已排除 {' / '.join(skipped)}）"
                             f" · 整站全量 {int(s['series']):,} 序列 / "
                             f"{int(s.get('writable_devices', 0)):,} 台设备")
        else:
            self.log("INFO", f"写入对象：{picked}（全部）")
        if streaming:
            self.log("INFO", f"写入规模：{total_series:,} 序列 / {total_devices:,} 台设备 "
                             f"（每设备点数留空 → 持续写入，不限制总量）")
        else:
            per_group = s.get("target_groups") or {}
            detail = " + ".join(
                f"{GROUP_LABEL.get(g, g)} {int((per_group.get(g) or {}).get('series', 0)):,}"
                for g in self.targets
            )
            self.log("INFO", f"写入规模：{total_series:,} 序列 / {total_devices:,} 台设备 / "
                             f"预计 {self.total_points:,} 个数据点（{detail}）")
        self._connect(db)

        # 两条路径各自负责关闭 session（_run_once 沿用其原有 finally，_run_streaming 自带）
        if streaming:
            return self._run_streaming(total_series, total_devices)
        return self._run_once(total_series, total_devices, timestamps)

    def _stream_base_ms(self) -> int:
        """持续写入的起始时间戳（毫秒）：history 用指定时刻，realtime 用当前时间。"""
        w = self.cfg.write
        if w.mode == "history" and w.history_start:
            try:
                return int(datetime.strptime(w.history_start, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
            except ValueError:
                pass
        return int(time.time() * 1000)

    def _run_streaming(self, total_series: int, total_devices: int) -> dict:
        """持续写入：每过一个采样周期写一轮（每台设备 1 个点），直到手动停止。

        与一次性模式的本质区别：时间戳随轮次不断向后推进，永不结束，
        用于模拟设备实时上报，而不是回灌一段固定长度的历史。
        """
        w = self.cfg.write
        interval = max(w.interval_ms, 1)
        interval_s = interval / 1000.0
        base_ms = self._stream_base_ms()
        batch_size = max(w.batch, 1)

        # 持续模式每轮都要遍历全部设备，惰性生成器只能消费一次，先落地成列表
        devices = list(iter_devices(self.cfg, self.targets))
        total_devices = len(devices) or total_devices
        self.total_devices = total_devices

        started = time.time()
        rounds = 0
        total_points = 0
        # 每轮都打日志（采样周期 >= 400ms 时一行一轮，节奏本身就是节流）；
        # 周期更密时按"约 1 秒一行"聚合，避免 100ms 周期刷出 10 行/秒
        log_every = 1 if interval >= 400 else max(1, int(round(1000.0 / interval)))
        self.log("INFO", f"持续写入已启动：每 {interval:,} ms 写一轮，每轮 {total_devices:,} 台设备 × 1 点，"
                         f"直到点击「停止」")

        while not self.stop_event.is_set():
            rounds += 1
            ts = base_ms + (rounds - 1) * interval
            buffer: List = []
            round_points = 0
            for device, group, level, measures in devices:
                if self.stop_event.is_set():
                    break
                buffer.append(self._make_tablet(device, measures, [ts]))
                if len(buffer) >= batch_size:
                    round_points += self._flush_stream(buffer)
                    buffer = []
            if buffer:
                round_points += self._flush_stream(buffer)

            total_points += round_points
            now = time.time()
            elapsed = now - started
            rate = total_points / max(elapsed, 0.001)

            if self.progress:
                self.progress({
                    "phase": "streaming", "round": rounds,
                    "round_points": round_points, "points": total_points,
                    "devices": total_devices, "elapsed": round(elapsed, 1),
                    "rate": round(rate), "interval_ms": interval,
                })
            # 逐轮记录：一轮一行，方便核对每一轮是否真的写进去了
            if rounds % log_every == 0 or rounds == 1:
                self.log("INFO", f"持续写入 第 {rounds:,} 轮 · 本轮 {round_points:,} 点 · "
                                 f"累计 {total_points:,} 点 · {rate:,.0f} 点/秒 · 已运行 {elapsed:.0f}s")

            # 按采样周期控制节奏；若一轮耗时已超过周期则不额外等待（尽力而为不堆积）
            wait = (started + rounds * interval_s) - time.time()
            if wait > 0 and self.stop_event.wait(wait):
                break

        try:
            if self.session:
                self.session.close()
        except Exception:
            pass

        elapsed = time.time() - started
        result = {
            "series": total_series, "devices": total_devices, "points": total_points,
            "rounds": rounds, "total_devices": total_devices, "total_points": total_points,
            "elapsed": round(elapsed, 2), "stopped": self.stop_event.is_set(),
            "streaming": True, "dry_run": self.mock,
        }
        if self.progress:
            self.progress({"phase": "done", **result})
        return result

    def _flush_stream(self, buffer) -> int:
        """持续模式按批提交，返回本批点数。"""
        if hasattr(self.session, "insert_tablets"):
            self.session.insert_tablets(buffer)
        else:
            self.session.insertTablets(buffer)
        return sum(_tablet_size(t) for t in buffer)

    def _run_once(self, total_series: int, total_devices: int, timestamps: List[int]) -> dict:
        w = self.cfg.write
        batch_size = max(w.batch, 1)
        buffer: List = []
        written_devices = 0
        written_points = 0
        started = time.time()
        next_rate_ts = started
        last_report = started
        last_build_report = started
        built_devices = 0
        self.log("INFO", f"正在生成 {total_devices:,} 台设备的测试数据"
                         f"（每设备 {len(timestamps):,} 个时间点）...")
        build_cap = 50.0  # 生成阶段占用进度条前半程，写入阶段占后半程

        try:
            for device, group, level, measures in iter_devices(self.cfg, self.targets):
                if self.stop_event.is_set():
                    self.log("WARN", "收到停止信号，中断写入")
                    break
                buffer.append(self._make_tablet(device, measures, timestamps))
                built_devices += 1
                # 构造阶段也推进度：大点数场景下生成可能耗时数秒，期间必须有可见反馈
                now_build = time.time()
                if self.progress and (
                    now_build - last_build_report >= 0.12 or built_devices == total_devices
                ):
                    self.progress({
                        "phase": "building",
                        "pct": round(built_devices / total_devices * build_cap, 1)
                               if total_devices else 0,
                        "built_devices": built_devices, "total_devices": total_devices,
                        "elapsed": round(now_build - started, 1),
                    })
                    last_build_report = now_build
                if len(buffer) >= batch_size:
                    written_devices, written_points = self._flush(
                        buffer, written_devices, written_points, total_devices,
                        started, last_report, next_rate_ts,
                    )
                    buffer = []
                    last_report = time.time()
                    next_rate_ts = self._rate_limit(next_rate_ts, written_points, started)
            if buffer and not self.stop_event.is_set():
                written_devices, written_points = self._flush(
                    buffer, written_devices, written_points, total_devices,
                    started, last_report, next_rate_ts,
                )
        finally:
            if self.session:
                self.session.close()

        elapsed = time.time() - started
        result = {
            "series": total_series, "devices": written_devices, "points": written_points,
            "total_devices": total_devices, "total_points": self.total_points,
            "elapsed": round(elapsed, 2), "stopped": self.stop_event.is_set(),
            "dry_run": self.mock,
        }
        if self.progress:
            self.progress({"phase": "done", **result})
        return result

    def _flush(self, buffer, written_devices, written_points, total_devices,
               started, last_report, next_rate_ts) -> Tuple[int, int]:
        tablets = buffer
        if hasattr(self.session, "insert_tablets"):
            self.session.insert_tablets(tablets)
        else:
            self.session.insertTablets(tablets)
        batch_points = sum(_tablet_size(t) for t in tablets)
        written_devices += len(tablets)
        written_points += batch_points
        now = time.time()
        # 进度条前半程给"生成数据"，后半程给"写入"，两段连续不回退
        pct = 50 + written_devices / total_devices * 50 if total_devices else 100
        rate = written_points / max(now - started, 0.001)
        remaining_devices = max(total_devices - written_devices, 0)
        remaining_points = max(self.total_points - written_points, 0)

        # 每一批都推进度事件（前端据此刷新动态进度行），不再受 1s 节流限制
        if self.progress:
            self.progress({
                "phase": "running", "pct": round(pct, 1),
                "devices": written_devices, "total_devices": total_devices,
                "remaining_devices": remaining_devices,
                "points": written_points, "total_points": self.total_points,
                "remaining_points": remaining_points,
                "rate": round(rate),
                "elapsed": round(now - started, 1),
            })

        # 日志文本仍按 1s 节流，避免刷屏；但内容包含"已写/待写"全量信息
        if now - last_report >= 1.0:
            self.log("INFO", f"写入进度 {pct:.1f}% · 设备 {written_devices:,}/{total_devices:,}"
                             f"（待写 {remaining_devices:,}）· 点 {written_points:,}/{self.total_points:,}"
                             f"（待写 {remaining_points:,}）· {rate:,.0f} 点/秒")
        return written_devices, written_points

    def _rate_limit(self, next_ts: float, written_points: int, started: float) -> float:
        limit = self.cfg.write.rate_limit
        if not limit or limit <= 0:
            return next_ts
        expected = started + written_points / limit
        delay = expected - time.time()
        if delay > 0:
            time.sleep(delay)
        return time.time()
