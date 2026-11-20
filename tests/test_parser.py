"""parser 的边界用例。

这些断言同时是「前后端必须一致」的契约：改动 server/parser.py 时，
index.html 里的 parseRange / parseMeasure 必须同步，否则预览与写入会静默偏差。
"""
import unittest

from server.parser import expand_measure, measure_count, parse_measure, parse_range


class TestParseRange(unittest.TestCase):
    def test_numeric_range_keeps_zero_padding(self):
        self.assertEqual(parse_range("d0001-d0003"), ["d0001", "d0002", "d0003"])

    def test_numeric_range_with_word_prefix(self):
        self.assertEqual(parse_range("pcs0101-pcs0103"),
                         ["pcs0101", "pcs0102", "pcs0103"])
        self.assertEqual(parse_range("35packtran101-35packtran102"),
                         ["35packtran101", "35packtran102"])

    def test_letter_suffix_range(self):
        self.assertEqual(parse_range("c101a-c101d"),
                         ["c101a", "c101b", "c101c", "c101d"])

    def test_list_with_mixed_separators(self):
        self.assertEqual(parse_range("c101，c201 c301"), ["c101", "c201", "c301"])

    def test_spaces_around_hyphen_are_normalized(self):
        self.assertEqual(parse_range("d0001 - d0003"), ["d0001", "d0002", "d0003"])

    def test_invalid_inputs_return_none(self):
        for text in ("", "   ", "zzz", "c001-d074", "c1-c"):
            with self.subTest(text=text):
                self.assertIsNone(parse_range(text))


class TestParseMeasure(unittest.TestCase):
    def test_single_measure(self):
        self.assertEqual(parse_measure("soc"), ("soc", "single", 1))
        self.assertEqual(parse_measure("v_01"), ("v_01", "single", 1))

    def test_measure_range_count(self):
        self.assertEqual(parse_measure("v001-v026"), ("v001-v026", "range", 26))
        self.assertEqual(parse_measure("rsv01-rsv07"), ("rsv01-rsv07", "range", 7))

    def test_range_width_follows_start_value(self):
        # 起始值只有 1 位 → 展开为 v1…v99，而不是 v001…v099
        names = expand_measure("v1-v99") or []
        self.assertEqual(len(names), 99)
        self.assertEqual(names[:2], ["v1", "v2"])

    def test_invalid_measures(self):
        for text in ("v001-t012", "1abc", "soc-", ""):
            with self.subTest(text=text):
                self.assertIsNone(parse_measure(text))

    def test_expand_and_count(self):
        self.assertEqual(expand_measure("relaytem01-relaytem03"),
                         ["relaytem01", "relaytem02", "relaytem03"])
        self.assertEqual(measure_count(["totv", "soc", "v001-v010"]), 12)
        self.assertEqual(measure_count([]), 0)


if __name__ == "__main__":
    unittest.main()
