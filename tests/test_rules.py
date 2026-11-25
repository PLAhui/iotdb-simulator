"""规则匹配与数值生成的用例。

匹配优先级是这个模块最脆弱的点：`t*` 会抢 `totp`、`*de` 会抢 workmode/runmode，
所以优先级必须由用例锁住，改动 RuleMatcher 时先跑这里。
"""
import unittest

from server.config import config_from_dict, default_config
from server.rules import Generator, RuleMatcher


class TestRuleMatching(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()
        self.matcher = RuleMatcher(self.cfg.rules, "yc")

    def test_exact_and_range_rules(self):
        self.assertEqual(self.matcher.match("soc").cat, "荷电状态")
        self.assertEqual(self.matcher.match("rsv03").cat, "预留")

    def test_specificity_beats_short_prefix(self):
        # t* 属于「温度」，但不能抢走以 t 开头的 totp
        self.assertEqual(self.matcher.match("totp").cat, "功率电流")
        # *de 属于「电量」，但不能抢走 workmode / runmode
        self.assertEqual(self.matcher.match("workmode").cat, "状态位")
        self.assertEqual(self.matcher.match("runmode").cat, "状态位")

    def test_numseq_only_matches_letter_plus_digits(self):
        self.assertEqual(self.matcher.match("t001").cat, "温度")
        self.assertEqual(self.matcher.match("v012").cat, "电压")
        # 没有具体规则命中的测点落到通用兜底，不再返回 None（否则遥测恒为 0）
        self.assertEqual(self.matcher.match("unknown_measure").cat, "通用遥测")

    def test_suffix_glob(self):
        self.assertEqual(self.matcher.match("singemaxvol").cat, "电压")
        self.assertEqual(self.matcher.match("avetem").cat, "温度")

    def test_fallback_rule_follows_signal_type(self):
        self.assertEqual(RuleMatcher(self.cfg.rules, "yc").match("need").cat, "通用遥测")
        self.assertEqual(RuleMatcher(self.cfg.rules, "yx").match("need").cat, "通用遥信")

    def test_energy_rule_still_matches_accumulators(self):
        for name in ("dayce", "totce", "dayde", "totde"):
            with self.subTest(name=name):
                self.assertEqual(self.matcher.match(name).cat, "电量")


class TestGenerator(unittest.TestCase):
    def _gen(self, signal: str = "yc") -> Generator:
        cfg = default_config()
        cfg.prefix.signal = signal
        return Generator(cfg, RuleMatcher(cfg.rules, signal))

    def test_float_values_stay_in_rule_range(self):
        gen = self._gen()
        for name, lo, hi in (("soc", 0, 100), ("t001", 15, 45), ("v001", 3.0, 4.2)):
            for i in range(120):
                pv = gen.value_for(name, 1_763_558_400_000 + i * 1000, f"dev.{name}")
                self.assertEqual(pv.dtype, "FLOAT")
                self.assertGreaterEqual(pv.value, lo, name)
                self.assertLessEqual(pv.value, hi, name)

    def test_const_model_returns_lower_bound(self):
        gen = self._gen()
        self.assertEqual(gen.value_for("rsv01", 1_763_558_400_000, "d.rsv01").value, 0.0)

    def test_unmatched_measure_is_not_constant_zero(self):
        # 没被具体规则覆盖的测点（need / worksta / hvcur…）走通用遥测，不能一直 0
        gen = self._gen()
        values = [gen.value_for("need", 1_763_558_400_000 + i * 1000, "d.need").value
                  for i in range(30)]
        self.assertTrue(any(v != 0 for v in values), values)
        self.assertGreater(len(set(values)), 1, values)

    def test_yx_signal_produces_boolean(self):
        gen = self._gen("yx")
        for name in ("soc", "workmode", "rsv01"):
            pv = gen.value_for(name, 1_763_558_400_000, f"dev.{name}")
            self.assertEqual(pv.dtype, "BOOLEAN")
            self.assertIsInstance(pv.value, bool)

    def test_yx_unmatched_measure_toggles(self):
        gen = self._gen("yx")
        values = [gen.value_for("need", 1_763_558_400_000 + i * 1000, "d.need").value
                  for i in range(30)]
        self.assertTrue(all(isinstance(v, bool) for v in values))
        self.assertEqual(set(values), {True, False})

    def test_mono_model_increases_monotonically(self):
        # 电量类测点用 mono：每次取值都比上一次大（到上限才回绕）
        gen = self._gen()
        values = [gen.value_for("totce", 1_763_558_400_000 + i * 1000, "dev.totce").value
                  for i in range(5)]
        self.assertTrue(all(b > a for a, b in zip(values, values[1:])), values)


class TestRuleConfig(unittest.TestCase):
    """老配置迁移：规则表里没有兜底行 / 带旧默认笔误时怎么补。"""

    def _legacy(self):
        return {
            "prefix": {"station": "cm", "zone": "z01", "signal": "yc", "order": "forward"},
            "groups": default_config().to_dict()["groups"],
            "rules": [
                {"cat": "电量", "min": 0, "max": 1000000, "unit": "", "model": "mono",
                 "match": "*ce, *de"},
                {"cat": "状态位", "min": 0, "max": 2, "unit": "", "model": "state",
                 "match": "workmode, relaysta"},
            ],
        }

    def test_legacy_rules_get_fallback_rows(self):
        cfg = config_from_dict(self._legacy())
        cats = [r.cat for r in cfg.rules]
        self.assertIn("通用遥测", cats)
        self.assertIn("通用遥信", cats)
        self.assertEqual([r.signal for r in cfg.rules if r.match == "*"], ["yc", "yx"])

    def test_legacy_match_typos_are_migrated(self):
        cfg = config_from_dict(self._legacy())
        by_cat = {r.cat: r for r in cfg.rules}
        self.assertNotIn("*de", by_cat["电量"].match.split(", "))
        self.assertIn("runmode", by_cat["状态位"].match)
        self.assertEqual(RuleMatcher(cfg.rules, "yc").match("runmode").cat, "状态位")

    def test_custom_star_rule_is_respected(self):
        # 用户自己写了 "*" 兜底就不再补默认两条，也不该出现重复兜底
        cfg = config_from_dict({
            "rules": [{"cat": "自定义兜底", "min": 1, "max": 9, "unit": "",
                       "model": "walk", "match": "*", "signal": "any"}],
        })
        self.assertEqual([r.cat for r in cfg.rules], ["自定义兜底"])


if __name__ == "__main__":
    unittest.main()
