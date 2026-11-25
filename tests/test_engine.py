"""设备树展开与规模统计的用例。

重点验证两个容易出错的语义：
  1. 节点数是「相乘」关系，PCS / 箱变是独立分支相加；
  2. writable_devices 只统计挂了测点的层（否则进度永远到不了 100%）。
"""
import unittest

from server.config import config_from_dict
from server.engine import iter_devices, node_counts, scale, writable_device_count


def make_cfg(pcs_sub: bool = False, order: str = "forward", xb_sub: bool = False):
    """构造一棵小树：2 堆 × 2 舱 × 3 簇 × 2 包 × 2 模组。"""
    return config_from_dict({
        "prefix": {"station": "cm", "zone": "z01", "signal": "yc", "order": order},
        "pcs_sub": pcs_sub,
        "xb_sub": xb_sub,
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
                {"key": "xbd", "name": "子设备 d", "range": "d01-d02",
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

    def test_xb_sub_toggle(self):
        # 箱变子设备层默认关闭，开启后节点数 = 箱变数 × 子设备数
        self.assertEqual(node_counts(make_cfg())["xbd"], 0)
        self.assertEqual(node_counts(make_cfg(xb_sub=True))["xbd"], 2)

    def test_sub_switches_are_independent(self):
        counts = node_counts(make_cfg(pcs_sub=True, xb_sub=False))
        self.assertEqual((counts["pcsd"], counts["xbd"]), (4, 0))
        counts = node_counts(make_cfg(pcs_sub=False, xb_sub=True))
        self.assertEqual((counts["pcsd"], counts["xbd"]), (0, 2))


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

    def test_xb_sub_adds_child_devices(self):
        devices = list(iter_devices(make_cfg(xb_sub=True)))
        sub = [d for d in devices if d[2] == "xbd"]
        self.assertEqual(len(sub), 2)
        self.assertEqual(sub[0][0], "root.cm.z01.yc.35packtran101.d01")
        self.assertEqual(sub[0][1], "XB")

    def test_xb_sub_progress_denominator_matches_produced(self):
        cfg = make_cfg(xb_sub=True)
        produced = len(list(iter_devices(cfg)))
        self.assertEqual(scale(cfg)["target_devices"], produced)
        self.assertEqual(writable_device_count(cfg), produced)
        # 关闭后子设备不再产出，分母同步回落
        off = make_cfg(xb_sub=False)
        self.assertEqual(scale(off)["target_devices"], len(list(iter_devices(off))))


class TestWriteTargets(unittest.TestCase):
    """写入对象（只写 BMS / PCS / 箱变或任意组合）的过滤语义。"""

    def cfg_with(self, targets):
        cfg = make_cfg()
        cfg.write.targets = list(targets)
        return cfg

    def test_filter_keeps_only_selected_groups(self):
        cfg = self.cfg_with(["PCS"])
        devices = list(iter_devices(cfg))
        self.assertEqual(len(devices), 2)
        self.assertTrue(all(d[1] == "PCS" for d in devices))

    def test_bms_plus_xb_skips_pcs(self):
        cfg = self.cfg_with(["BMS", "XB"])
        groups = {d[1] for d in iter_devices(cfg)}
        self.assertEqual(groups, {"BMS", "XB"})

    def test_progress_denominator_matches_produced_devices(self):
        # 进度分母（target_devices）必须等于实际产出，否则百分比收敛不到 100%
        for targets in (["BMS"], ["PCS"], ["XB"], ["PCS", "XB"], ["BMS", "PCS", "XB"]):
            cfg = self.cfg_with(targets)
            produced = len(list(iter_devices(cfg)))
            self.assertEqual(scale(cfg)["target_devices"], produced, targets)
            self.assertEqual(writable_device_count(cfg), produced, targets)

    def test_series_scale_follows_targets(self):
        full = scale(make_cfg())
        only_pcs = scale(self.cfg_with(["PCS"]))
        self.assertEqual(only_pcs["target_series"], 2)
        self.assertEqual(only_pcs["target_devices"], 2)
        # 整站全量不受写入对象影响
        self.assertEqual(only_pcs["series"], full["series"])
        self.assertEqual(only_pcs["writable_devices"], full["writable_devices"])

    def test_empty_selection_falls_back_to_all(self):
        cfg = config_from_dict({"write": {"targets": []}})
        self.assertEqual(cfg.write.targets, ["BMS", "PCS", "XB"])

    def test_targets_are_normalized_and_ordered(self):
        cfg = config_from_dict({"write": {"targets": ["xb", " BMS ", "XYZ", "bms"]}})
        self.assertEqual(cfg.write.targets, ["BMS", "XB"])


if __name__ == "__main__":
    unittest.main()
