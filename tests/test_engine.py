"""设备树展开与规模统计的用例。

重点验证两个容易出错的语义：
  1. 节点数是「相乘」关系，PCS / 箱变是独立分支相加；
  2. writable_devices 只统计挂了测点的层（否则进度永远到不了 100%）。
"""
import unittest

from server.config import config_from_dict
from server.engine import iter_devices, node_counts, scale, writable_device_count


def make_cfg(pcs_sub: bool = False, order: str = "forward"):
    """构造一棵小树：2 堆 × 2 舱 × 3 簇 × 2 包 × 2 模组。"""
    return config_from_dict({
        "prefix": {"station": "cm", "zone": "z01", "signal": "yc", "order": order},
        "pcs_sub": pcs_sub,
        "groups": {
            "BMS": [
                {"key": "d", "name": "堆", "range": "d0001-d0002", "measures": []},
                {"key": "c", "name": "舱", "range": "c101-c102", "measures": ["soh"]},
                {"key": "cu", "name": "簇", "range": "c001-c003",
                 "measures": ["totv", "v001-v002"]},
                {"key": "p", "name": "包", "range": "p01-p02", "measures": []},
                {"key": "m", "name": "模组", "range": "m01-m02", "measures": ["t001"]},
            ],
            "PCS": [
                {"key": "pcs", "name": "PCS", "range": "pcs0101-pcs0102",
                 "measures": ["totv"]},
                {"key": "pcsd", "name": "子设备 d", "range": "d01-d02",
                 "measures": ["totv"]},
            ],
            "XB": [
                {"key": "xb", "name": "箱变", "range": "35packtran101",
                 "measures": ["hvvol"]},
            ],
        },
        "rules": [],
    })


class TestNodeCounts(unittest.TestCase):
    def test_counts_multiply_down_the_tree(self):
        counts = node_counts(make_cfg())
        self.assertEqual(
            {k: counts[k] for k in ("d", "c", "cu", "p", "m")},
            {"d": 2, "c": 4, "cu": 12, "p": 24, "m": 48},
        )
        self.assertEqual(counts["pcs"], 2)
        self.assertEqual(counts["xb"], 1)

    def test_pcs_sub_toggle(self):
        self.assertEqual(node_counts(make_cfg())["pcsd"], 0)
        self.assertEqual(node_counts(make_cfg(pcs_sub=True))["pcsd"], 4)


class TestScale(unittest.TestCase):
    def test_series_only_counts_levels_with_measures(self):
        s = scale(make_cfg())
        # c: 4×1 + cu: 12×3 + m: 48×1 + pcs: 2×1 + xb: 1×1 = 91
        self.assertEqual(s["series"], 91)
        self.assertEqual(s["groups"]["BMS"], 88)
        self.assertEqual(s["groups"]["PCS"], 2)
        self.assertEqual(s["groups"]["XB"], 1)

    def test_writable_devices_excludes_empty_levels(self):
        cfg = make_cfg()
        s = scale(cfg)
        # d 与 p 没有测点，不产出设备
        self.assertEqual(s["devices"], 93)
        self.assertEqual(s["writable_devices"], 67)
        self.assertEqual(writable_device_count(cfg), len(list(iter_devices(cfg))))


class TestIterDevices(unittest.TestCase):
    def test_forward_order_puts_pile_before_cabin(self):
        first = next(iter_devices(make_cfg(order="forward")))
        self.assertEqual(first[0], "root.cm.z01.yc.d0001.c101")
        self.assertEqual(first[1:3], ("BMS", "c"))

    def test_reverse_order_puts_cabin_before_pile(self):
        first = next(iter_devices(make_cfg(order="reverse")))
        self.assertEqual(first[0], "root.cm.z01.yc.c101.d0001")

    def test_group_split_and_total(self):
        devices = list(iter_devices(make_cfg()))
        self.assertEqual(len(devices), 67)
        self.assertEqual(sum(1 for d in devices if d[1] == "BMS"), 64)
        self.assertEqual(sum(1 for d in devices if d[1] == "PCS"), 2)
        self.assertEqual(sum(1 for d in devices if d[1] == "XB"), 1)

    def test_pcs_sub_adds_child_devices(self):
        devices = list(iter_devices(make_cfg(pcs_sub=True)))
        sub = [d for d in devices if d[2] == "pcsd"]
        self.assertEqual(len(sub), 4)
        self.assertEqual(sub[0][0], "root.cm.z01.yc.pcs0101.d01")


if __name__ == "__main__":
    unittest.main()
