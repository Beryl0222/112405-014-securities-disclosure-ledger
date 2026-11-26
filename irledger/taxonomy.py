"""指标与比较口径的关键字分类。

分类依据资料中实际出现的表头/措辞，命中不了的一律归为“未识别”，
由人工复核，不按最接近的指标猜测。
"""
from __future__ import annotations

from .models import CompareBasis, MetricKind

# 指标关键字按特异性排序，先具体后宽泛。
_METRIC_RULES: tuple[tuple[tuple[str, ...], MetricKind], ...] = (
    (("预收款",), MetricKind.ADVANCE_RECEIPT),
    (("确认收入", "收入"), MetricKind.REVENUE),
    (("订单",), MetricKind.ORDER),
    (("存量", "余额"), MetricKind.BALANCE),
)

_FX_RULES: tuple[tuple[tuple[str, ...], CompareBasis], ...] = (
    (("固定汇率",), CompareBasis.CONSTANT_FX),
    (("报告汇率",), CompareBasis.REPORTED_FX),
)


def classify_metric(label: str) -> MetricKind:
    for keywords, kind in _METRIC_RULES:
        if any(word in label for word in keywords):
            return kind
    return MetricKind.UNKNOWN


def detect_fx_basis(label: str) -> CompareBasis | None:
    for keywords, basis in _FX_RULES:
        if any(word in label for word in keywords):
            return basis
    return None


def detect_compare_basis(label: str) -> CompareBasis:
    """从文字中识别比较维度；同比/环比关键字优先于汇率口径。"""
    if "环比" in label or "相邻季度" in label or "上一季" in label:
        return CompareBasis.QOQ
    if "同比" in label or "去年同期" in label or "上年同期" in label:
        return CompareBasis.YOY
    fx = detect_fx_basis(label)
    if fx is not None:
        return fx
    return CompareBasis.NONE


def is_growth_word(phrase: str) -> bool:
    return any(word in phrase for word in ("增长", "增加", "上升", "增", "下降", "减少", "下跌", "降"))
