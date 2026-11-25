"""范围解析与测点展开。

与前端 index.html 的 parseRange / parseMeasure 逻辑严格对齐，支持四种编码写法：
  1) 数值连续：d0001-d0020、pcs0101-pcs2004、35packtran101-35packtran102
  2) 尾缀连续：c101a-c101d（末尾单字母递增）
  3) 逐项列举：c101,c102,c201,c202 或 c101 c102 c201 c202（逗号/空格分隔）
  4) 单个编码：byr、lvr、zbhv —— 不带编号也算一台设备（箱变子层常见写法）
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

_NUM_RANGE = re.compile(r"^(.*?)(\d+)-(.*?)(\d+)$")
_LETTER_RANGE = re.compile(r"^(.*?)([a-zA-Z])-(.*?)([a-zA-Z])$")
_MEASURE_RANGE = re.compile(r"^([a-zA-Z]+)(\d+)-([a-zA-Z]+)(\d+)$")
_MEASURE_SINGLE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_VALID_CODE = re.compile(r"^[A-Za-z0-9]+$")


def parse_range(text: str) -> Optional[List[str]]:
    """把范围字符串展开为完整编码列表；无法解析时返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    # 与前端一致：先把连字符两侧空白规范化，再按逗号/空白切分
    normalized = re.sub(r"\s*-\s*", "-", raw)
    tokens = [t for t in re.split(r"[,，\s]+", normalized) if t]
    out: List[str] = []
    for token in tokens:
        seg = None
        if "-" in token:
            numeric = _NUM_RANGE.match(token)
            if numeric and numeric.group(1) == numeric.group(3):
                start, end = int(numeric.group(2)), int(numeric.group(4))
                if end >= start:
                    width = len(numeric.group(2))
                    seg = [f"{numeric.group(1)}{i:0{width}d}" for i in range(start, end + 1)]
            if seg is None:
                letters = _LETTER_RANGE.match(token)
                if letters and letters.group(1) == letters.group(3):
                    a, b = ord(letters.group(2)), ord(letters.group(4))
                    if b >= a:
                        seg = [f"{letters.group(1)}{chr(i)}" for i in range(a, b + 1)]
        if seg is None:
            seg = [token]
        # 编码只要求是字母/数字组合：单台设备可以直接写一个编码（byr / zbhv / main），
        # 不强制带编号。带连字符却没解析成功的写法（c001-d074、d01-d0x）仍然非法——
        # '-' 不在允许字符集里，不会静默降级成"一台名字叫这串字符的设备"。
        if any(not _VALID_CODE.match(v) for v in seg):
            return None
        out.extend(seg)
    return out or None


def parse_measure(text: str) -> Optional[Tuple[str, str, int]]:
    """解析单个测点定义，返回 (名称, 类型, 数量)；类型为 single 或 range。"""
    raw = str(text or "").strip()
    match = _MEASURE_RANGE.match(raw)
    if match and match.group(1) == match.group(3) and int(match.group(4)) >= int(match.group(2)):
        return raw, "range", int(match.group(4)) - int(match.group(2)) + 1
    if _MEASURE_SINGLE.match(raw):
        return raw, "single", 1
    return None


def expand_measure(name: str) -> List[str]:
    """把测点定义展开为具体测点名列表。单个测点返回 [name]。"""
    parsed = parse_measure(name)
    if parsed is None:
        return []
    raw, kind, _ = parsed
    if kind == "single":
        return [raw]
    match = _MEASURE_RANGE.match(raw)
    assert match is not None
    width = len(match.group(2))
    return [f"{match.group(1)}{i:0{width}d}" for i in range(int(match.group(2)), int(match.group(4)) + 1)]


def measure_count(names: List[str]) -> int:
    """计算一组测点定义对应的实际测点数量。"""
    total = 0
    for name in names:
        parsed = parse_measure(name)
        if parsed:
            total += parsed[2]
    return total
