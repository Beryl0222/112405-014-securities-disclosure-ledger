"""端到端业务规则测试：基于合成资料包验证口径、更正、复核与分发。"""
import unittest
from fractions import Fraction

from irledger.models import (
    CompareBasis, MetricKind, OutputState, ReviewState, StatementKind,
)
from irledger.pipeline import Pipeline
from irledger.review import DistributionLedger, ReviewError, check_media_reprints

CASE = "data/case.json"


class PipelineFixture(unittest.TestCase):
    def setUp(self):
        self.pipe = Pipeline.from_case_file(CASE)
        self.result = self.pipe.validation_result()

    def claim(self, claim_id):
        return self.pipe.claims[claim_id]

    def claims_by(self, **filters):
        out = []
        for claim in self.pipe.claims.values():
            if all(getattr(claim, key) == value for key, value in filters.items()):
                out.append(claim)
        return out


class TableAndMetricTest(PipelineFixture):
    def test_table_cells_inherit_only_own_title_unit(self):
        t1 = {c.row_label + c.column_label: c for c in self.pipe.registry.all_current()
              if c.table_id == "T1"}
        t2 = {c.row_label + c.column_label: c for c in self.pipe.registry.all_current()
              if c.table_id == "T2"}
        self.assertTrue(all(c.unit == "百万元" for c in t1.values()))
        self.assertTrue(all(c.unit == "万元" for c in t2.values()))
        # T2 的 800 万元不得按 T1 的百万元解释。
        cell = next(c for c in t2.values() if c.raw_value == "800")
        self.assertEqual(cell.amount_yuan, Fraction(8_000_000))

    def test_metric_kinds_separated(self):
        metrics = {c.metric for c in self.result.claims}
        self.assertIn(MetricKind.REVENUE, metrics)
        self.assertIn(MetricKind.ORDER, metrics)
        self.assertIn(MetricKind.ADVANCE_RECEIPT, metrics)

    def test_compare_basis_yoy_vs_qoq_vs_fx(self):
        qoq = self.claims_by(metric=MetricKind.ADVANCE_RECEIPT, basis=CompareBasis.QOQ)
        yoy = [c for c in self.claims_by(basis=CompareBasis.YOY)
               if c.statement_kind == StatementKind.DERIVED]
        cfx = self.claims_by(basis=CompareBasis.CONSTANT_FX)
        rfx = self.claims_by(basis=CompareBasis.REPORTED_FX)
        self.assertTrue(qoq and yoy and cfx and rfx)

    def test_derived_ratios_match_inputs_exactly(self):
        # 固定汇率订单 (120-100)/100 = 20%；报告汇率 14%；预收款环比 -20%。
        order_cfx = next(c for c in self.claims_by(basis=CompareBasis.CONSTANT_FX)
                         if c.statement_kind == StatementKind.DERIVED)
        self.assertEqual(order_cfx.computed_ratio, Fraction(1, 5))
        advance = next(c for c in self.claims_by(basis=CompareBasis.QOQ))
        self.assertEqual(advance.computed_ratio, -Fraction(1, 5))


class DiscourseTest(PipelineFixture):
    def test_ellipsis_two_cheng_resolves_to_order_not_revenue(self):
        # “海外收入也增长两成吗”承接的是前文订单固定汇率，且被标为换指标风险。
        hazard_codes = {f.code for f in self.result.findings}
        self.assertIn("ellipsis-resolution", hazard_codes)
        question = next(c for c in self.pipe.claims.values()
                        if "海外收入也增长两成" in c.text)
        self.assertEqual(question.statement_kind, StatementKind.QUESTION_PRESUPPOSITION)
        self.assertIn("省略承接-换指标", question.flags)

    def test_answer_explicitly_denies_revenue_reading(self):
        answer = next(c for c in self.pipe.claims.values() if "不是收入" in c.text)
        self.assertIn("澄清-否认当前指标", answer.flags)

    def test_forecast_never_treated_as_fact(self):
        forecast = next(c for c in self.pipe.claims.values()
                        if c.statement_kind == StatementKind.FORECAST)
        self.assertEqual(forecast.review, ReviewState.REJECTED)
        self.assertIn("预测不得当事实", forecast.flags)

    def test_question_presupposition_not_a_formal_statement(self):
        presuppositions = self.claims_by(statement_kind=StatementKind.QUESTION_PRESUPPOSITION)
        self.assertTrue(all(c.review == ReviewState.PENDING for c in presuppositions))
        self.assertTrue(any("提问预设" in c.flags for c in presuppositions))


class CorrectionTest(PipelineFixture):
    def test_revenue_denominator_correction_invalidates_dependent_claims(self):
        # 旧 250 事实与旧 20% 收入比率都失效（按依赖图，不是数字字符串搜索）。
        stale_fact = next(c for c in self.pipe.claims.values() if "250百万元" in c.text)
        stale_ratio = next(c for c in self.pipe.claims.values()
                           if c.basis == CompareBasis.YOY and "变化 20%" in c.text)
        self.assertEqual(stale_fact.review, ReviewState.INVALIDATED)
        self.assertEqual(stale_ratio.review, ReviewState.INVALIDATED)
        self.assertTrue(any("FD-C1" in flag for flag in stale_ratio.flags))

    def test_unaffected_order_claim_remains_usable(self):
        # 同一材料的另一项指标（订单固定汇率两成）未被更正，仍可使用。
        order = next(c for c in self.pipe.claims.values()
                     if c.metric == MetricKind.ORDER and c.asserted_ratio == Fraction(1, 5))
        self.assertNotEqual(order.review, ReviewState.INVALIDATED)

    def test_rederived_ratio_uses_new_denominator(self):
        new_ratio = next(c for c in self.pipe.claims.values()
                         if "变化 25%" in c.text and c.metric == MetricKind.REVENUE)
        self.assertEqual(new_ratio.computed_ratio, Fraction(1, 4))
        self.assertEqual(new_ratio.review, ReviewState.CONFIRMED)
        self.assertIn("更正后重算:FD-C1", new_ratio.flags)
        # 分母指向单元 v2，来源可回查到 FD-C1。
        self.assertTrue(new_ratio.denominator_ref.endswith("@v2"))

    def test_correction_scope_is_limited_to_named_cell(self):
        # 更正只动上财年首季收入单元；本期 300 与订单行保持 v1。
        current_300 = self.pipe.registry.current("T1", "集团确认收入", "本财年首季")
        self.assertEqual(current_300.raw_value, "300")
        self.assertEqual(current_300.version, 1)
        order_cell = self.pipe.registry.current("T1", "海外订单固定汇率", "上财年首季")
        self.assertEqual(order_cell.version, 1)

    def test_unit_clarification_does_not_cross_table(self):
        # T2 始终是万元；澄清否定的百万元不适用于该表。
        t2_cells = [c for c in self.pipe.registry.all_current() if c.table_id == "T2"]
        self.assertTrue(all(c.unit == "万元" for c in t2_cells))

    def test_digit_collision_index_flags_same_literal(self):
        self.assertIn("100", self.result.digit_collisions)


class MediaTest(PipelineFixture):
    def test_reprint_is_not_independent_evidence(self):
        findings = check_media_reprints(list(self.pipe.claims.values()),
                                        self.pipe.reprint_chains)
        self.assertTrue(any(f.code == "media-reprint" for f in findings))
        self.assertIn("FD-M1", self.pipe.reprint_chains)
        media_claim = next(c for c in self.pipe.claims.values()
                           if c.statement_kind == StatementKind.MEDIA)
        self.assertTrue(all(not ev.independent for ev in media_claim.evidence))


class ExistingOutputTest(PipelineFixture):
    def test_sent_output_with_invalidated_claim_gets_targeted_notice(self):
        out1 = self.pipe.outputs["OUT1"]
        invalid_ids = [
            cid for cid in out1.claim_ids
            if self.pipe.claims[cid].review == ReviewState.INVALIDATED
        ]
        self.assertEqual(invalid_ids, ["CL-011"])
        # 另一条订单主张仍挂在同一材料上，不整批撤回。
        self.assertEqual(len(out1.claim_ids), 2)

    def test_draft_with_confirmed_claims_can_send(self):
        out2 = self.pipe.outputs["OUT2"]
        ledger = DistributionLedger()
        ledger.register(out2)
        ok, problems = ledger.can_send(out2, self.pipe.claims)
        self.assertTrue(ok, problems)

    def test_forecast_dressed_as_fact_is_blocked(self):
        out3 = self.pipe.outputs["OUT3"]
        self.assertEqual(out3.state, OutputState.SENT)  # 历史已发保留
        linked = self.pipe.claims[out3.claim_ids[0]]
        self.assertIn("预测冒充事实:OUT3", linked.flags)
        ledger = DistributionLedger()
        ledger.register(out3)
        with self.assertRaises(ReviewError):
            ledger.send(out3, self.pipe.claims)

    def test_sent_material_keeps_snapshot_for_followup(self):
        out2 = self.pipe.outputs["OUT2"]
        ledger = DistributionLedger()
        ledger.register(out2)
        ledger.send(out2, self.pipe.claims)
        self.assertEqual(out2.snapshot_text, out2.text)
        self.assertEqual(out2.state, OutputState.SENT)

    def test_correction_notice_is_specific_to_claim_and_recipient(self):
        ledger = DistributionLedger()
        out1 = self.pipe.outputs["OUT1"]
        ledger.register(out1)
        notice = ledger.attach_correction(
            "OUT1", "CL-011", "收入分母更正为240百万元", ("FD-C1",))
        self.assertEqual(notice.recipient, "分析师甲")
        self.assertEqual(notice.claim_id, "CL-011")
        self.assertEqual(len(ledger.corrections_for_recipient("分析师甲")), 1)
        # 无关收件人没有更正条目。
        self.assertEqual(ledger.corrections_for_recipient("分析师乙"), [])


class IncrementalIngestTest(unittest.TestCase):
    def test_new_quarter_records_append_without_duplicates(self):
        pipe = Pipeline.from_case_file(CASE)
        count = len(pipe.claims)
        report = pipe.ingest_records([
            {"id": "FD-T3", "kind": "table",
             "text": "表三单位万元；列：本季、上一季。行：预收款900、800；比较相邻季度，不是去年同期。"},
        ])
        self.assertEqual(report.new_record_ids, ["FD-T3"])
        self.assertGreater(len(pipe.claims), count)
        # 再次摄入同编号资料不产生重复。
        again = pipe.ingest_records([
            {"id": "FD-T3", "kind": "table", "text": "重复投递"},
        ])
        self.assertEqual(again.new_record_ids, [])

    def test_cross_table_unit_never_borrowed(self):
        pipe = Pipeline.from_case_file(CASE)
        pipe.ingest_records([
            {"id": "FD-T3", "kind": "table",
             "text": "表三单位万元；列：本季、上一季。行：预收款900、800；比较相邻季度，不是去年同期。"},
        ])
        amounts = {c.table_id: c.amount_yuan for c in pipe.registry.all_current()
                   if c.raw_value == "800"}
        self.assertEqual(amounts["T2"], Fraction(8_000_000))
        self.assertEqual(amounts["T3"], Fraction(8_000_000))

    def test_new_correction_blocks_unsent_draft_but_keeps_sent_material(self):
        from irledger.review import DistributionLedger
        pipe = Pipeline.from_case_file(CASE)
        out2 = pipe.outputs["OUT2"]
        self.assertEqual(out2.state, OutputState.DRAFT)
        advance_claim = pipe.claims[out2.claim_ids[0]]
        self.assertEqual(advance_claim.review, ReviewState.CONFIRMED)

        # 新来源到达：更正 T2 预收款本季单元。
        pipe.ingest_records([
            {"id": "FD-C3", "kind": "correction",
             "text": "表二本季预收款应为700万元，原800有误；其余行数值及期间不变。"},
        ])
        # 未发说法不得继续沿用失效确认：草稿关联主张失效，发送被拦截。
        self.assertEqual(advance_claim.review, ReviewState.INVALIDATED)
        ledger = DistributionLedger()
        ledger.register(out2)
        ok, problems = ledger.can_send(out2, pipe.claims)
        self.assertFalse(ok)
        self.assertTrue(any("CL-014" in p for p in problems))
        # 已发材料 OUT1 原样保留，不被这次无关更正整批撤回。
        self.assertEqual(pipe.outputs["OUT1"].state, OutputState.SENT)
        self.assertEqual(pipe.outputs["OUT1"].text, "集团收入增20%，海外固定汇率订单增20%。")


if __name__ == "__main__":
    unittest.main()
