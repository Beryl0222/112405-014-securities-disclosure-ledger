"""异常资料的防御性处理测试：系统不静默套用、不猜测。"""
import unittest

from irledger.corrections import CellRegistry, apply_correction, parse_correction
from irledger.table_parser import parse_table
from irledger.textnum import to_yuan
from fractions import Fraction
from irledger.models import Cell


def _registry_with_one_cell():
    registry = CellRegistry()
    registry.add(Cell(
        table_id="T1", row_label="集团确认收入", column_label="上财年首季",
        raw_value="250", amount_yuan=to_yuan(Fraction(250), "百万元"),
        unit="百万元", unit_inherited=True, source_id="FD-T1", version=1,
    ))
    return registry


class MalformedInputTest(unittest.TestCase):
    def test_correction_rejected_when_old_value_mismatches(self):
        registry = _registry_with_one_cell()
        spec = parse_correction(
            "FD-CX", "correction",
            "表一上财年首季集团确认收入应为240百万元，原299有误；其余不变。",
            {1: "T1"},
        )
        self.assertEqual(spec.old_raw_value, "299")
        with self.assertRaises(ValueError) as ctx:
            apply_correction(spec, registry)
        self.assertIn("拒绝自动套用", str(ctx.exception))
        # 原单元保持不动。
        self.assertEqual(
            registry.current("T1", "集团确认收入", "上财年首季").raw_value, "250")

    def test_correction_unknown_table_raises(self):
        with self.assertRaises(KeyError):
            parse_correction("FD-CY", "correction",
                             "表九上财年首季集团确认收入应为1百万元，原2有误。", {1: "T1"})

    def test_table_row_column_count_mismatch_raises(self):
        with self.assertRaises(ValueError):
            parse_table(
                "FD-TX", "T9",
                "表九单位百万元；列：本季、上一季。行：预收款900。",
                lambda c, r, f: None,
            )

    def test_clarification_only_touches_named_table_values(self):
        registry = _registry_with_one_cell()
        spec = parse_correction(
            "FD-CZ", "clarification",
            "表二800和1000都是万元，问答稿误写百万元不适用于该表。",
            {1: "T1", 2: "T2"},
        )
        changed = apply_correction(spec, registry)
        # T1 的 250 不在澄清清单内，完全不受影响。
        self.assertEqual(changed, [])
        self.assertEqual(
            registry.current("T1", "集团确认收入", "上财年首季").unit, "百万元")


if __name__ == "__main__":
    unittest.main()
