"""设备树展开与规模统计。

关键设计：iter_devices 是惰性生成器，逐个设备产出 (路径, 测点列表)。
整站 68 万序列 / 1.7 万设备不会一次性驻留内存，写入端按批消费。
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Tuple

from .config import AppConfig, Level
from .parser import expand_measure, measure_count, parse_range


def prefix_str(cfg: AppConfig) -> str:
    p = cfg.prefix
    return f"root.{p.station or 'station'}.{p.zone or 'zone'}.{p.signal}"


def parsed(cfg: AppConfig, key: str) -> List[str]:
    lv = cfg.level(key)
    return (parse_range(lv.range) or []) if lv else []


def node_counts(cfg: AppConfig) -> Dict[str, int]:
    """各层设备节点数（与前端 nodeCount 一致）。"""
    d, c, cu, p, m = (parsed(cfg, k) for k in ("d", "c", "cu", "p", "m"))
    if not all([d, c, cu, p, m]):
        counts = {k: 0 for k in ("d", "c", "cu", "p", "m")}
    else:
        dz = len(d)
        cz = dz * len(c)
        cuz = cz * len(cu)
        pz = cuz * len(p)
        mz = pz * len(m)
        counts = {"d": dz, "c": cz, "cu": cuz, "p": pz, "m": mz}
    pcs = parsed(cfg, "pcs")
    counts["pcs"] = len(pcs)
    pcsd = parsed(cfg, "pcsd")
    counts["pcsd"] = len(pcs) * len(pcsd) if cfg.pcs_sub and pcs and pcsd else 0
    xb = parsed(cfg, "xb")
    counts["xb"] = len(xb)
    return counts


def writable_device_count(cfg: AppConfig) -> int:
    """实际会被写入的设备数：只统计挂了测点的节点。

    与 iter_devices 的产出严格一致（某层 measures 为空则该层设备不产出）。
    scale()['devices'] 统计的是全部节点（含空层），做进度分母会偏大，
    导致进度百分比永远到不了 100%、"剩余设备数"也不准。
    """
    counts = node_counts(cfg)
    total = 0
    for levels in cfg.groups.values():
        for lv in levels:
            if measure_count(lv.measures) > 0:
                total += counts.get(lv.key, 0)
    return total


def scale(cfg: AppConfig) -> Dict[str, object]:
    """规模统计：分组序列数、设备数、总数。"""
    counts = node_counts(cfg)
    groups: Dict[str, int] = {}
    for gname, levels in cfg.groups.items():
        total = 0
        for lv in levels:
            node_key = lv.key
            total += counts.get(node_key, 0) * measure_count(lv.measures)
        groups[gname] = total
    device_total = sum(counts.values())
    return {
        "counts": counts,
        "groups": groups,
        "devices": device_total,
        "writable_devices": writable_device_count(cfg),
        "series": sum(groups.values()),
        "points_per_cycle": sum(groups.values()),
    }


def _level_measures_raw(cfg: AppConfig, key: str) -> List[str]:
    lv = cfg.level(key)
    if not lv:
        return []
    out: List[str] = []
    for m in lv.measures:
        out.extend(expand_measure(m))
    return out


def iter_devices(cfg: AppConfig) -> Iterator[Tuple[str, str, str, List[str]]]:
    """惰性展开所有设备。

    Yields:
        (设备路径, 类别 BMS/PCS/XB, 层级 key, 该设备的具体测点名列表)
    """
    pre = prefix_str(cfg)
    d, c, cu, p, m = (parsed(cfg, k) for k in ("d", "c", "cu", "p", "m"))
    if all([d, c, cu, p, m]):
        # 预计算各层测点（展开范围），避免在深层循环中重复解析
        dm, cm, cum, pm, mm = (_level_measures_raw(cfg, k) for k in ("d", "c", "cu", "p", "m"))
        for dv in d:
            if dm:  # 堆级设备在舱循环外产出，避免重复
                yield f"{pre}.{dv}", "BMS", "d", dm
            for cv in c:
                cabin = f"{dv}.{cv}" if cfg.prefix.order == "forward" else f"{cv}.{dv}"
                if cm:
                    yield f"{pre}.{cabin}", "BMS", "c", cm
                for cuv in cu:
                    cu_path = f"{cabin}.{cuv}"
                    if cum:
                        yield f"{pre}.{cu_path}", "BMS", "cu", cum
                    for pv in p:
                        p_path = f"{cu_path}.{pv}"
                        if pm:
                            yield f"{pre}.{p_path}", "BMS", "p", pm
                        for mv in m:
                            m_path = f"{p_path}.{mv}"
                            if mm:
                                yield f"{pre}.{m_path}", "BMS", "m", mm

    pcs = parsed(cfg, "pcs")
    pcs_measures = _level_measures_raw(cfg, "pcs")
    pcsd = parsed(cfg, "pcsd") if cfg.pcs_sub else []
    pcsd_measures = _level_measures_raw(cfg, "pcsd") if cfg.pcs_sub else []
    for pv in pcs:
        if pcs_measures:
            yield f"{pre}.{pv}", "PCS", "pcs", pcs_measures
        for dv in pcsd:
            if pcsd_measures:
                yield f"{pre}.{pv}.{dv}", "PCS", "pcsd", pcsd_measures

    xb = parsed(cfg, "xb")
    xb_measures = _level_measures_raw(cfg, "xb")
    for xv in xb:
        if xb_measures:
            yield f"{pre}.{xv}", "XB", "xb", xb_measures
