"""规则匹配与数值生成的用例。

匹配优先级是这个模块最脆弱的点：`t*` 会抢 `totp`、`*de` 会抢 `workmode`，
所以优先级必须由用例锁住，改动 RuleMatcher 时先跑这里。
"""
import unittest

from server.config import default_config
from server.rules import Generator, RuleMatcher


class TestRuleMatching(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()
        self.matcher = RuleMatcher(self.cfg.rules)

    def test_exact_and_range_rules(self):
        self.assertEqual(self.matcher.match("soc").cat, "荷电状态")
        self.assertEqual(self.matcher.match("rsv03").cat, "预留")

    def test_specificity_beats_short_prefix(self):
        # t* 属于「温度」，但不能抢走以 t 开头的 totp
        self.assertEqual(self.matcher.match("totp").cat, "功率电流")
        # *de 属于「电量」，但不能抢走 workmode
        self.assertEqual(self.matcher.match("workmode").cat, "状态位")

    def test_numseq_only_matches_letter_plus_digits(self):
        self.assertEqual(self.matcher.match("t001").cat, "温度")
        self.assertEqual(self.matcher.match("v012").cat, "电压")
        self.assertIsNone(self.matcher.match("unknown_measure"))

    def test_suffix_glob(self):
        self.assertEqual(self.matcher.match("singemaxvol").cat, "电压")
        self.assertEqual(self.matcher.match("avetem").cat, "温度")


class TestGenerator(unittest.TestCase):
    def _gen(self, signal: str = "yc") -> Generator:
        cfg = default_config()
        cfg.prefix.signal = signal
        return Generator(cfg, RuleMatcher(cfg.rules))

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

    def test_yx_signal_produces_boolean(self):
        gen = self._gen("yx")
        for name in ("soc", "workmode", "rsv01"):
            pv = gen.value_for(name, 1_763_558_400_000, f"dev.{name}")
            self.assertEqual(pv.dtype, "BOOLEAN")
            self.assertIsInstance(pv.value, bool)

    def test_mono_model_increases_monotonically(self):
        # 电量类测点用 mono：每次取值都比上一次大（到上限才回绕）
        gen = self._gen()
        values = [gen.value_for("totce", 1_763_558_400_000 + i * 1000, "dev.totce").value
                  for i in range(5)]
        self.assertTrue(all(b > a for a, b in zip(values, values[1:])), values)


if __name__ == "__main__":
    unittest.main()
