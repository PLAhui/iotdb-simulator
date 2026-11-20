"""造数规则引擎：测点匹配 + 五类变化模型 + 类型驱动。

类型规则：signal=yc → 全部 FLOAT；signal=yx → 全部 BOOLEAN（0/1）。
每个序列维护轻量状态（上次值 / 单调累加量 / 正弦相位），65 万序列的状态表按需增长。
"""
from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import AppConfig, Rule
from .parser import parse_measure, parse_range


@dataclass
class PointValue:
    name: str
    value: object
    dtype: str  # FLOAT | BOOLEAN


def _glob_match(name: str, pattern: str) -> bool:
    """把 * 通配转为正则，全匹配。"""
    regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
    return re.match(regex, name) is not None


class RuleMatcher:
    """测点名 → 规则。预编译匹配表，避免每点重复解析。"""

    def __init__(self, rules: List[Rule]):
        self.rules = rules
        self._cache: Dict[str, Optional[Rule]] = {}
        # 预编译为 (规则, 匹配器, 特异性)；特异性越高越优先，避免 t* 抢走 totp、
        # *de 抢走 workmode 这类误匹配。匹配顺序：精确名 > 范围展开 > 长前缀 > 短前缀。
        self._compiled: List[tuple] = []
        for rule in rules:
            for raw in re.split(r"[,，\s]+", rule.match):
                if not raw:
                    continue
                rng = parse_range(raw)
                if rng and len(rng) > 1:
                    for value in rng:
                        self._compiled.append((rule, ("exact", value), 100))
                elif "*" not in raw:
                    self._compiled.append((rule, ("exact", raw), 100))
                else:
                    # 编号序列通配（v* / t*）：仅匹配"字母+数字"型测点，排除 totp/workmode 等
                    m = re.fullmatch(r"([a-zA-Z]+)\*", raw)
                    if m and len(m.group(1)) == 1:
                        self._compiled.append((rule, ("numseq", m.group(1)), 40))
                    else:
                        prefix = raw.split("*", 1)[0]
                        suffix = raw.rsplit("*", 1)[1]
                        self._compiled.append((rule, ("glob", raw, prefix, suffix), 10 + len(prefix) + len(suffix)))
        self._compiled.sort(key=lambda x: -x[2])

    def match(self, name: str) -> Optional[Rule]:
        if name in self._cache:
            return self._cache[name]
        found: Optional[Rule] = None
        for rule, matcher, _spec in self._compiled:
            kind = matcher[0]
            if kind == "exact" and name == matcher[1]:
                found = rule
                break
            if kind == "numseq" and re.fullmatch(re.escape(matcher[1]) + r"\d+", name):
                found = rule
                break
            if kind == "glob" and name.startswith(matcher[2]) and name.endswith(matcher[3]) \
                    and len(name) >= len(matcher[2]) + len(matcher[3]):
                found = rule
                break
        self._cache[name] = found
        return found


class Generator:
    """流式值生成器。状态按序列名惰性创建，单次运行内保持一致。"""

    MODELS = {"walk", "sine", "state", "mono", "const", "flip"}

    def __init__(self, cfg: AppConfig, matcher: RuleMatcher):
        self.cfg = cfg
        self.matcher = matcher
        self.boolean = cfg.prefix.signal == "yx"
        self.state: Dict[str, float] = {}
        self.phase: Dict[str, float] = {}
        self.rng = random.Random(42)
        self.start_ts = time.time()

    def _clamp(self, v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    def _float_value(self, name: str, rule: Optional[Rule], ts_ms: int, key: str) -> float:
        lo, hi = (rule.min, rule.max) if rule else (0.0, 1.0)
        model = rule.model if rule and rule.model in self.MODELS else "const"
        if hi < lo:
            lo, hi = hi, lo
        span = hi - lo
        mid = (lo + hi) / 2
        elapsed = (ts_ms / 1000.0) - self.start_ts

        if model == "const":
            return round(lo, 4)
        if model == "walk":
            prev = self.state.get(key, mid)
            step = span * 0.02 if span > 0 else 0.01
            nxt = prev + self.rng.uniform(-step, step)
            if nxt < lo:
                nxt = lo + (lo - nxt) * 0.5
            if nxt > hi:
                nxt = hi - (nxt - hi) * 0.5
            self.state[key] = nxt
            return round(self._clamp(nxt, lo, hi), 4)
        if model == "sine":
            if key not in self.phase:
                self.phase[key] = self.rng.uniform(0, 2 * math.pi)
            period = max(rule.period, 1) if rule else 86400
            wave = math.sin(2 * math.pi * elapsed / period + self.phase[key])
            return round(mid + wave * span / 2, 4)
        if model == "mono":
            prev = self.state.get(key, lo)
            step = max(span * 0.001, 0.001)
            nxt = prev + step
            if nxt > hi:
                nxt = lo  # 到上限后回绕
            self.state[key] = nxt
            return round(nxt, 4)
        if model == "state":
            choices = sorted({lo, mid, hi})
            return round(self.rng.choice(choices), 4)
        if model == "flip":
            prev = self.state.get(key, lo)
            nxt = hi if prev <= mid else lo
            self.state[key] = nxt
            return round(nxt, 4)
        return round(lo, 4)

    def value_for(self, name: str, ts_ms: int, key: str | None = None) -> PointValue:
        """生成单个测点的值。key 为序列唯一标识（设备路径.测点），缺省用测点名。"""
        key = key or name
        rule = self.matcher.match(name)
        if self.boolean:
            # 遥信：全部 BOOLEAN。规则模型决定 0/1 的变化方式
            model = rule.model if rule else "flip"
            if model == "const":
                value = False
            elif model == "state":
                value = self.rng.random() < 0.5
            elif model == "flip":
                prev = self.state.get(key, 0.0)
                value = prev < 0.5
                self.state[key] = 1.0 if value else 0.0
            else:
                value = self.rng.random() < 0.5
            return PointValue(name, bool(value), "BOOLEAN")
        return PointValue(name, self._float_value(name, rule, ts_ms, key), "FLOAT")

    def values_for(self, names: List[str], ts_ms: int, device: str = "") -> List[PointValue]:
        prefix = f"{device}." if device else ""
        return [self.value_for(n, ts_ms, f"{prefix}{n}") for n in names]


class AnomalyInjector:
    """可选异常注入：按比例对值做越限 / 尖峰，用于测试告警链路。"""

    def __init__(self, enabled: bool = False, rate: float = 0.001, factor: float = 3.0):
        self.enabled = enabled
        self.rate = rate
        self.factor = factor
        self.rng = random.Random(7)

    def apply(self, points: List[PointValue]) -> List[PointValue]:
        if not self.enabled:
            return points
        for pt in points:
            if pt.dtype == "FLOAT" and self.rng.random() < self.rate:
                pt.value = round(float(pt.value) * self.factor, 4)
        return points
