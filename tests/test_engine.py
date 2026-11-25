"""设备树展开与规模统计的用例。

重点验证两个容易出错的语义：
  1. 节点数是「相乘」关系，PCS / 箱变是独立分支相加；
  2. writable_devices 只统计挂了测点的层（否则进度永远到不了 100%）。
"""
import unittest

from server.config import config_from_dict
from server.engine import iter_devices, node_counts, scale, writable_device_count


def make_cfg(pcs_sub: bool = False, order: str = "forward", xb_subs=None):
    """构造一棵小树：2 堆 × 2 舱 × 3 簇 × 2 包 × 2 模组，箱变带两个子设备层。"""
    data = {
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
                {"key": "byr", "name": "变压器室", "range": "byr01-byr02",
                 "measures": ["tem"]},
                {"key": "lvr", "name": "低压室", "range": "lvr01",
                 "measures": ["lvvol"]},
            ],
        },
        "rules": [],
    }
    if xb_subs is not None:
        data["xb_subs"] = xb_subs
    return config_from_dict(data)


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
        # 箱变子设备层默认全关，逐个开启后节点数 = 箱变数 × 该层设备数
        counts = node_counts(make_cfg())
        self.assertEqual((counts["byr"], counts["lvr"]), (0, 0))
        counts = node_counts(make_cfg(xb_subs=["byr"]))
        self.assertEqual((counts["byr"], counts["lvr"]), (2, 0))
        counts = node_counts(make_cfg(xb_subs=["byr", "lvr"]))
        self.assertEqual((counts["byr"], counts["lvr"]), (2, 1))

    def test_xb_sub_switches_are_independent(self):
        # 两个子层各开各的：只开 lvr 不影响 byr
        counts = node_counts(make_cfg(xb_subs=["lvr"]))
        self.assertEqual((counts["xb"], counts["byr"], counts["lvr"]), (1, 0, 1))

    def test_sub_switches_are_independent(self):
        counts = node_counts(make_cfg(pcs_sub=True, xb_subs=[]))
        self.assertEqual((counts["pcsd"], counts["byr"], counts["lvr"]), (4, 0, 0))
        counts = node_counts(make_cfg(pcs_sub=False, xb_subs=["byr", "lvr"]))
        self.assertEqual((counts["pcsd"], counts["byr"], counts["lvr"]), (0, 2, 1))

    def test_legacy_xb_sub_bool_enables_every_layer(self):
        # 旧写法 xb_sub: true 等价于"启用配置里定义的所有箱变子层"
        cfg = config_from_dict({
            "xb_sub": True,
            "groups": {"XB": [
                {"key": "xb", "name": "箱变", "range": "35packtran101", "measures": ["hvvol"]},
                {"key": "byr", "name": "变压器室", "range": "byr01", "measures": ["tem"]},
                {"key": "lvr", "name": "低压室", "range": "lvr01", "measures": ["lvvol"]},
            ]},
        })
        self.assertEqual(cfg.xb_subs, ["byr", "lvr"])
        self.assertEqual(node_counts(cfg)["byr"], 1)

    def test_unknown_sub_layer_is_dropped(self):
        # 配置里没定义的层 key 不应被接受，否则会拼出没有定义的路径
        cfg = config_from_dict({"xb_subs": ["byr", "hvr", "xb", " byr "]})
        self.assertEqual(cfg.xb_subs, ["byr"])

    def test_xb_subs_accepts_comma_string(self):
        # 手写 YAML 写成 xb_subs: byr, lvr 也要认
        cfg = config_from_dict({"xb_subs": "byr，lvr"})
        self.assertEqual(cfg.xb_subs, ["byr", "lvr"])

    def test_custom_sub_layer_is_kept(self):
        # 子层定义完全由配置决定：上一版的通用层 xbd 也是一层普通的自定义层，
        # 不再被迁移成 byr / lvr（否则用户自定义的层会被悄悄改掉）
        cfg = config_from_dict({
            "xb_sub": True,
            "groups": {"XB": [
                {"key": "xb", "name": "箱变", "range": "35packtran101", "measures": ["hvvol"]},
                {"key": "xbd", "name": "子设备 d", "range": "d01-d02", "measures": ["hvvol"]},
            ]},
        })
        keys = [lv.key for lv in cfg.groups["XB"]]
        self.assertEqual(keys, ["xb", "xbd"])
        self.assertEqual(cfg.xb_subs, ["xbd"])
        self.assertEqual(node_counts(cfg)["xbd"], 2)

    def test_custom_layers_are_not_backfilled_with_defaults(self):
        # 删掉的默认子层不应在解析时复活：已有 XB 组按配置原样保留
        cfg = config_from_dict({
            "groups": {"XB": [
                {"key": "xb", "name": "箱变", "range": "35packtran101", "measures": ["hvvol"]},
                {"key": "hvr", "name": "高压室", "range": "hvr01-hvr03", "measures": ["hvvol"]},
            ]},
        })
        self.assertEqual([lv.key for lv in cfg.groups["XB"]], ["xb", "hvr"])
        # 整组缺失时才回落到默认定义
        cfg2 = config_from_dict({"groups": {}})
        self.assertEqual([lv.key for lv in cfg2.groups["XB"]], ["xb", "byr", "lvr"])

    def test_blank_and_duplicate_layer_keys_are_dropped(self):
        cfg = config_from_dict({
            "groups": {"XB": [
                {"key": " xb ", "name": "箱变", "range": "35packtran101", "measures": ["hvvol"]},
                {"key": "byr", "name": "变压器室", "range": "byr01", "measures": ["tem"]},
                {"key": " byr ", "name": "重复层", "range": "byr02", "measures": ["hum"]},
                {"key": "  ", "name": "空 key", "range": "d01", "measures": ["x"]},
            ]},
            "xb_subs": ["byr"],
        })
        self.assertEqual([lv.key for lv in cfg.groups["XB"]], ["xb", "byr"])
        self.assertEqual(cfg.xb_subs, ["byr"])


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
        devices = list(iter_devices(make_cfg(xb_subs=["byr"])))
        sub = [d for d in devices if d[2] == "byr"]
        self.assertEqual(len(sub), 2)
        self.assertEqual(sub[0][0], "root.cm.z01.yc.35packtran101.byr01")
        self.assertEqual(sub[0][1], "XB")

    def test_multiple_xb_sub_layers_are_siblings(self):
        # byr 与 lvr 都挂在箱变下，是兄弟节点而不是父子，路径深度一致
        devices = list(iter_devices(make_cfg(xb_subs=["byr", "lvr"])))
        byr = [d for d in devices if d[2] == "byr"]
        lvr = [d for d in devices if d[2] == "lvr"]
        self.assertEqual([d[0] for d in byr],
                         ["root.cm.z01.yc.35packtran101.byr01",
                          "root.cm.z01.yc.35packtran101.byr02"])
        self.assertEqual([d[0] for d in lvr], ["root.cm.z01.yc.35packtran101.lvr01"])

    def test_xb_sub_progress_denominator_matches_produced(self):
        for subs in (["byr"], ["lvr"], ["byr", "lvr"]):
            cfg = make_cfg(xb_subs=subs)
            produced = len(list(iter_devices(cfg)))
            self.assertEqual(scale(cfg)["target_devices"], produced, subs)
            self.assertEqual(writable_device_count(cfg), produced, subs)
        # 全关后子设备不再产出，分母回到只有箱变本体
        off = make_cfg(xb_subs=[])
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
