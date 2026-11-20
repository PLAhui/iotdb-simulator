"""后端配置模型与持久化（config.yaml）。

配置结构与前端 index.html 的数据模型一一对应：
  connections / activeConnection / prefix / groups / rules / write
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class Connection:
    id: str
    name: str
    host: str = "127.0.0.1"
    port: int = 6667
    user: str = "root"
    password: str = "root"
    database: str = "root.cm"


@dataclass
class Level:
    key: str
    name: str
    range: str
    measures: List[str] = field(default_factory=list)


@dataclass
class Rule:
    cat: str
    min: float
    max: float
    unit: str
    model: str
    match: str
    period: int = 86400  # 正弦周期（秒），仅 sine 使用


@dataclass
class WriteConfig:
    interval_ms: int = 1000
    mode: str = "realtime"      # realtime | history
    batch: int = 1000           # 每批 insertTablet 的设备数
    points_per_device: Optional[int] = 10  # 每个设备一次写入的时间点数；None = 持续写入直到手动停止
    history_start: str = ""     # 历史回灌起始时间，空表示当前时间回退
    rate_limit: int = 0         # 每秒最多写入点数，0 表示不限速
    threads: int = 4            # 写入线程数
    dry_run: bool = False       # true 时只走完整流程但不真实连接 IoTDB（用于自检）


@dataclass
class Prefix:
    station: str = "cm"
    zone: str = "z01"
    signal: str = "yc"          # yc | yx
    order: str = "forward"      # forward=d.c | reverse=c.d


@dataclass
class AppConfig:
    connections: List[Connection] = field(default_factory=list)
    active_connection: str = "cfg1"
    prefix: Prefix = field(default_factory=Prefix)
    groups: Dict[str, List[Level]] = field(default_factory=dict)
    pcs_sub: bool = False
    rules: List[Rule] = field(default_factory=list)
    write: WriteConfig = field(default_factory=WriteConfig)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def active_db(self) -> Connection:
        for c in self.connections:
            if c.id == self.active_connection:
                return c
        return self.connections[0] if self.connections else Connection("cfg1", "本地")

    def level(self, key: str) -> Level | None:
        for levels in self.groups.values():
            for lv in levels:
                if lv.key == key:
                    return lv
        return None


def default_config() -> AppConfig:
    """与前端 v0.8 默认值完全一致。"""
    return AppConfig(
        connections=[
            Connection("cfg1", "本地", "127.0.0.1", 6667, "root", "root", "root.cm"),
            Connection("cfg2", "测试", "192.168.1.10", 6667, "root", "root", "root.cm"),
        ],
        active_connection="cfg1",
        prefix=Prefix("cm", "z01", "yc", "forward"),
        groups={
            "BMS": [
                Level("d", "堆", "d0001-d0020",
                      ["need", "workmode", "runmode", "dayce", "dayde", "totce", "totde",
                       "totp", "totq", "soc", "soe", "soh", "maxcep", "maxdep", "rsv01-rsv07"]),
                Level("c", "舱", "c101,c102,c201,c202",
                      ["need", "workmode", "worksta", "runmode", "dayce", "dayde", "totce", "totde",
                       "totp", "totq", "soc", "soe", "soh", "singemaxvol", "singeminvol",
                       "singemaxtem", "singemintem", "avetem", "rsv01-rsv07"]),
                Level("cu", "簇", "cu01-cu12",
                      ["totv", "toti", "totp", "maxcep", "maxdep", "soc", "soe", "soh",
                       "singemaxvol", "singeminvol", "singemaxtem", "singemintem", "relaysta",
                       "soctop", "socdown", "totv2", "insulval", "avetem",
                       "relaytem01-relaytem04", "rsv01-rsv06"]),
                Level("p", "包", "p01-p04", []),
                Level("m", "模组", "m01-m04",
                      ["v001-v026", "t001-t012", "act01", "pt01", "pt02"]),
            ],
            "PCS": [
                Level("pcs", "PCS", "pcs0101-pcs2004",
                      ["totv", "toti", "totp", "totq", "soc", "soh", "workmode", "worksta", "rsv01-rsv04"]),
                Level("pcsd", "子设备 d", "d01-d02",
                      ["totv", "toti", "totp", "totq", "soc", "workmode", "rsv01-rsv04"]),
            ],
            "XB": [
                Level("xb", "箱变", "35packtran101-35packtran102,35packtran201-35packtran202",
                      ["hvvol", "lvvol", "hvcur", "lvcur", "totp", "totq",
                       "oiltemp", "windingtemp", "worksta", "rsv01-rsv04"]),
            ],
        },
        pcs_sub=False,
        rules=[
            Rule("电压", 3.0, 4.2, "V", "walk", "v*, *vol"),
            Rule("温度", 15, 45, "℃", "sine", "t*, *tem"),
            Rule("荷电状态", 0, 100, "", "walk", "soc, soe, soh"),
            Rule("功率电流", -500, 500, "", "sine", "totp, toti, totq"),
            Rule("电量", 0, 1000000, "", "mono", "*ce, *de"),
            Rule("状态位", 0, 2, "", "state", "workmode, relaysta"),
            Rule("预留", 0, 0, "", "const", "rsv01-rsv07"),
        ],
        write=WriteConfig(),
    )


def config_from_dict(data: Dict[str, Any]) -> AppConfig:
    """从前端提交的 JSON 构造配置（宽松解析，缺字段用默认值补齐）。"""
    base = default_config().to_dict()
    merged = copy.deepcopy(base)
    for key in ("connections", "activeConnection", "active_connection", "prefix",
                "groups", "pcsSub", "pcs_sub", "rules", "write"):
        if key in data:
            merged[key] = data[key]
    if "activeConnection" in merged and "active_connection" not in data:
        merged["active_connection"] = merged.pop("activeConnection")
    else:
        merged.pop("activeConnection", None)

    connections = [Connection(**{k: c.get(k, v) for k, v in
                                 {"id": "", "name": "", "host": "127.0.0.1", "port": 6667,
                                  "user": "root", "password": "root", "database": "root.cm"}.items()})
                   for c in merged.get("connections", [])]
    prefix_d = merged.get("prefix", {})
    prefix = Prefix(**{k: prefix_d.get(k, v) for k, v in
                       {"station": "cm", "zone": "z01", "signal": "yc", "order": "forward"}.items()})
    def _norm_measures(raw) -> list:
        """测点统一规范化为字符串列表。

        前端 GROUPS 里测点是 {name,type,count} 对象，而本地 YAML / 引擎用
        \"need\" / \"v001-v026\" 字符串。统一取 name，避免 scale 算成 0。
        """
        out = []
        for m in (raw or []):
            if isinstance(m, dict):
                n = str(m.get("name", "")).strip()
                if n:
                    out.append(n)
            else:
                s = str(m).strip()
                if s:
                    out.append(s)
        return out

    groups = {}
    for gname, levels in merged.get("groups", {}).items():
        groups[gname] = []
        for lv in levels:
            groups[gname].append(Level(
                key=lv.get("key", ""), name=lv.get("name", ""),
                range=lv.get("range", ""), measures=_norm_measures(lv.get("measures", [])),
            ))
    rules = []
    for r in merged.get("rules", []):
        rules.append(Rule(
            cat=r.get("cat", ""), min=float(r.get("min", 0)), max=float(r.get("max", 0)),
            unit=r.get("unit", ""), model=r.get("model", "const"),
            match=r.get("match", ""), period=int(r.get("period", 86400)),
        ))
    wd = merged.get("write", {}) or {}
    # points_per_device 留空（null/""/0）表示持续写入，不是 0 点
    ppd_raw = wd.get("points_per_device", 10)
    if ppd_raw in (None, "", "null", "None") or (
        isinstance(ppd_raw, (int, float)) and ppd_raw <= 0
    ):
        ppd: Optional[int] = None
    else:
        try:
            ppd = max(int(ppd_raw), 1)
        except (TypeError, ValueError):
            ppd = 10

    def _as_int(v, default: int) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    write = WriteConfig(
        interval_ms=max(_as_int(wd.get("interval_ms"), 1000), 1),
        mode=wd.get("mode", "realtime") or "realtime",
        batch=max(_as_int(wd.get("batch"), 1000), 1),
        points_per_device=ppd,
        history_start=wd.get("history_start", "") or "",
        rate_limit=_as_int(wd.get("rate_limit"), 0),
        threads=max(_as_int(wd.get("threads"), 4), 1),
        dry_run=bool(wd.get("dry_run", False)),
    )
    return AppConfig(
        connections=connections, active_connection=merged.get("active_connection", "cfg1"),
        prefix=prefix, groups=groups,
        pcs_sub=bool(merged.get("pcs_sub", merged.get("pcsSub", False))),
        rules=rules, write=write,
    )


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        return default_config()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return config_from_dict(data)
    except Exception:
        return default_config()


def save_config(path: Path, cfg: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg.to_dict(), fh, allow_unicode=True, sort_keys=False)
    tmp.replace(path)
