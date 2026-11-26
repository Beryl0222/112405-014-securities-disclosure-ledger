"""复核工作流与冲突处理测试。"""
import unittest
from fractions import Fraction

from irledger.models import (
    Claim, CompareBasis, Evidence, EvidenceKind, MetricKind, ReviewState,
    StatementKind,
)
from irledger.review import ReviewError, ReviewLedger, independent_evidence_count
from irledger.engine import ClaimFactory, check_text_number_conflicts
from irledger.corrections import CellRegistry, claims_dependent_on
from irledger.models import Cell


def make_cell(table="T1", row="集团确认收入", column="上财年首季",
              raw="250", unit="百万元", version=1, source="FD-T1"):
    from irledger.textnum import to_yuan
    return Cell(
        table_id=table, row_label=row, column_label=column, raw_value=raw,
        amount_yuan=to_yuan(Fraction(raw), unit), unit=unit, unit_inherited=True,
        source_id=source, version=version,
    )


class ReviewStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.factory = ClaimFactory()
        self.ledger = ReviewLedger()

    def _claim(self):
        return self.factory.make(
            metric=MetricKind.REVENUE, basis=CompareBasis.YOY,
            statement_kind=StatementKind.FACT, text="测试主张",
            evidence=[Evidence(EvidenceKind.DIRECT_QUOTE, ("FD-T1",), "锚点")],
        )

    def test_valid_transitions_record_events(self):
        claim = self._claim()
        self.ledger.transition(claim, ReviewState.CONFIRMED, "复核确认")
        self.assertEqual(claim.review, ReviewState.CONFIRMED)
        self.ledger.invalidate(claim, "分母更正", "FD-C1")
        self.assertEqual(claim.review, ReviewState.INVALIDATED)
        self.assertEqual(len(self.ledger.events), 2)
        self.assertEqual(self.ledger.events[1].source_id, "FD-C1")

    def test_illegal_transition_rejected(self):
        claim = self._claim()
        # 待复核不能直接转到已被替代。
        with self.assertRaises(ReviewError):
            self.ledger.transition(claim, ReviewState.SUPERSEDED, "越级")

    def test_reconfirmed_after_correction_uses_new_claim(self):
        # 失效主张可重新确认（基于更正后重算的新主张）。
        claim = self._claim()
        self.ledger.transition(claim, ReviewState.CONFIRMED, "初版确认")
        self.ledger.invalidate(claim, "依据失效", "FD-C1")
        self.ledger.transition(claim, ReviewState.CONFIRMED, "按新分母重算后确认")
        self.assertEqual(claim.review, ReviewState.CONFIRMED)


class DependencyGraphTest(unittest.TestCase):
    def test_dependency_found_by_cell_not_number_string(self):
        registry = CellRegistry()
        cell = make_cell()
        registry.add(cell)
        factory = ClaimFactory()
        # 主张文字里根本不含被改的数字 250，仍应通过依赖图被找到。
        claim = factory.make(
            metric=MetricKind.REVENUE, basis=CompareBasis.YOY,
            statement_kind=StatementKind.DERIVED, text="收入同比变化（措辞不含原值）",
            evidence=[Evidence(EvidenceKind.DERIVED_RATIO, ("FD-T1",), "算式")],
            computed_ratio=Fraction(1, 5),
            depends_on_cells=[("T1", "集团确认收入", "上财年首季", 1)],
        )
        hit = claims_dependent_on([cell], [claim])
        self.assertEqual(hit, [claim])

    def test_independent_evidence_counts_reprint_once(self):
        claim = Claim(
            claim_id="X", metric=MetricKind.REVENUE, basis=CompareBasis.NONE,
            statement_kind=StatementKind.MEDIA, text="媒体说法",
            evidence=[
                Evidence(EvidenceKind.DIRECT_QUOTE, ("FD-M1",), "", independent=False),
                Evidence(EvidenceKind.DIRECT_QUOTE, ("FD-T1",), "", independent=True),
            ],
        )
        self.assertEqual(independent_evidence_count(claim), 1)


class ConflictNoSidePickingTest(unittest.TestCase):
    def test_text_number_conflict_reports_both_sides(self):
        registry = CellRegistry()
        registry.add(make_cell(raw="250"))
        factory = ClaimFactory()
        # 主张声称的金额与单元不一致。
        from irledger.textnum import to_yuan
        claim = factory.make(
            metric=MetricKind.REVENUE, basis=CompareBasis.NONE,
            statement_kind=StatementKind.FACT, text="文字声称 260百万元",
            evidence=[Evidence(EvidenceKind.DIRECT_QUOTE, ("FD-T1",), "锚点")],
            asserted_amount_yuan=to_yuan(Fraction(260), "百万元"),
            depends_on_cells=[("T1", "集团确认收入", "上财年首季", 1)],
        )
        findings = check_text_number_conflicts([claim], registry)
        self.assertEqual(len(findings), 1)
        self.assertIn("不一致", findings[0].message)
        # 系统不改写任何一边：原值与主张值都保留。
        self.assertEqual(registry.current("T1", "集团确认收入", "上财年首季").raw_value, "250")
        self.assertEqual(claim.asserted_amount_yuan, to_yuan(Fraction(260), "百万元"))


if __name__ == "__main__":
    unittest.main()
