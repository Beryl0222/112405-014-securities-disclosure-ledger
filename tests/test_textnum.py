"""数字解析与单位换算测试：全部用精确有理数，不走浮点。"""
import unittest
from fractions import Fraction

from irledger.textnum import (
    extract_amounts, extract_ratio, format_percent, parse_chinese_number,
    ratio_from_cheng, to_yuan,
)


class ChineseNumberTest(unittest.TestCase):
    def test_basic_digits_and_tens(self):
        self.assertEqual(parse_chinese_number("两"), 2)
        self.assertEqual(parse_chinese_number("二十"), 20)
        self.assertEqual(parse_chinese_number("三百零五"), 305)

    def test_cheng_ratio(self):
        self.assertEqual(ratio_from_cheng("增长两成"), Fraction(1, 5))
        self.assertEqual(ratio_from_cheng("下降两成"), -Fraction(1, 5))
        self.assertIsNone(ratio_from_cheng("没有变化措辞"))

    def test_percent_ratio(self):
        self.assertEqual(extract_ratio("增长20%"), Fraction(1, 5))
        self.assertEqual(extract_ratio("下降14%"), Fraction(-14, 100))

    def test_unit_conversion_is_exact(self):
        self.assertEqual(to_yuan(Fraction(300), "百万元"), Fraction(300_000_000))
        self.assertEqual(to_yuan(Fraction(800), "万元"), Fraction(8_000_000))
        # 百万元与万元字面相同数字，换算后量级不同。
        self.assertNotEqual(
            to_yuan(Fraction(800), "百万元"),
            to_yuan(Fraction(800), "万元"),
        )

    def test_chinese_amount_extraction(self):
        amounts = extract_amounts("明年收入能到四亿元")
        self.assertEqual(amounts, [(Fraction(400_000_000), "亿元")])

    def test_default_unit_only_when_explicitly_given(self):
        # 不传默认单位时，裸数字不会被猜成金额。
        self.assertEqual(extract_amounts("数字 120 没有单位"), [])
        # 显式继承表标题单位。
        amounts = extract_amounts("海外订单120", default_unit="百万元")
        self.assertEqual(amounts, [(Fraction(120_000_000), "百万元")])

    def test_unknown_unit_rejected(self):
        with self.assertRaises(ValueError):
            to_yuan(Fraction(1), "亿亿美元")

    def test_format_percent_fraction(self):
        self.assertEqual(format_percent(Fraction(1, 4)), "25%")
        self.assertEqual(format_percent(Fraction(1, 3)), "33.33%")


if __name__ == "__main__":
    unittest.main()
