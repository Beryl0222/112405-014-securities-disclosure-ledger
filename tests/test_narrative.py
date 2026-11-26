"""上下文措辞解析测试：省略、其中、同口径、指标承接。"""
import unittest

from irledger.narrative import DiscourseTracker, parse_statement
from irledger.models import MetricKind
from irledger.taxonomy import classify_metric


class ContextWordingTest(unittest.TestCase):
    def test_qizhong_marks_subordinate_breakout(self):
        stmts = parse_statement("X1", "report", "集团确认收入300百万元，其中海外占比未拆。")
        flagged = [s for s in stmts if "其中-从属拆分" in s.flags]
        self.assertTrue(flagged)

    def test_same_basis_flags_baseline_lookup(self):
        stmts = parse_statement("X2", "report", "同口径下订单保持增长。")
        self.assertTrue(any("同口径-须回查比较基准" in s.flags for s in stmts))

    def test_answer_inherits_question_metric_with_flag(self):
        q = parse_statement("Q", "question", "分析师：海外收入也增长两成吗？")
        metric = q[0].metric
        self.assertEqual(metric, MetricKind.REVENUE)
        a = parse_statement("A", "answer", "希望达到该目标。", context_metric=metric)
        self.assertEqual(a[0].metric, MetricKind.REVENUE)
        self.assertIn("指标承接提问", a[0].flags)

    def test_answer_without_context_keeps_unknown(self):
        a = parse_statement("A", "answer", "这里没有指标词。")
        self.assertEqual(a[0].metric, MetricKind.UNKNOWN)

    def test_discourse_tracker_detects_metric_switch_once(self):
        tracker = DiscourseTracker()
        order_stmts = parse_statement("B1", "report", "海外订单按固定汇率增长两成。")
        for s in order_stmts:
            tracker.feed(s)
        q_stmts = parse_statement("Q1", "question", "海外收入也增长两成吗？")
        resolutions = [tracker.feed(s) for s in q_stmts]
        resolutions = [r for r in resolutions if r is not None]
        self.assertEqual(len(resolutions), 1)
        self.assertTrue(resolutions[0].switched)
        self.assertEqual(resolutions[0].inherited_metric, MetricKind.ORDER)
        self.assertEqual(resolutions[0].sentence_metric, MetricKind.REVENUE)


if __name__ == "__main__":
    unittest.main()
