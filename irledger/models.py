"""核心数据模型：资料记录、表单元、主张、复核与分发记录。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from enum import Enum
from fractions import Fraction
from typing import Any


class MetricKind(str, Enum):
    """指标大类：确认收入、订单、预收款、存量。"""

    REVENUE = "确认收入"
    ORDER = "订单"
    ADVANCE_RECEIPT = "预收款"
    BALANCE = "存量"
    UNKNOWN = "未识别"


class CompareBasis(str, Enum):
    """比较维度。"""

    YOY = "同比"          # 与去年同期比
    QOQ = "环比"          # 与相邻季度比
    CONSTANT_FX = "固定汇率"
    REPORTED_FX = "报告汇率"
    NONE = "无比较"       # 单点事实，没有比较口径
    UNKNOWN = "比较口径不明"


class StatementKind(str, Enum):
    """陈述性质：已实现事实、预测、提问预设、媒体转述。"""

    FACT = "已实现事实"
    FORECAST = "预测"
    QUESTION_PRESUPPOSITION = "提问者预设"
    MEDIA = "媒体转述"
    DERIVED = "派生比率"


class EvidenceKind(str, Enum):
    """证据形态：直接引用、问答转写、由两个数字算出的比率。"""

    DIRECT_QUOTE = "直接引用"
    TRANSCRIPT = "问答转写"
    DERIVED_RATIO = "派生比率"
    POINTER = "口径指引"


class ReviewState(str, Enum):
    """财务复核状态。"""

    PENDING = "待复核"
    CONFIRMED = "已确认"
    REJECTED = "不予确认"
    INVALIDATED = "依据失效"   # 更正后失去依据，等待重新核对
    SUPERSEDED = "已被替代"    # 期间重述/单位澄清后被新版本替代


class OutputState(str, Enum):
    DRAFT = "draft"
    SENT = "sent"


class Severity(str, Enum):
    CONFLICT = "冲突"       # 数字与文字不一致等需要人看的冲突
    HAZARD = "口径风险"     # 省略、换指标、单位继承等提示
    INFO = "说明"


@dataclass(frozen=True)
class SourceRecord:
    """一条原始资料（正文/表格/问答/更正/媒体）。"""

    id: str
    kind: str
    text: str


@dataclass(frozen=True)
class Cell:
    """表格单元：数值保留原文写法，金额同时换算为元。"""

    table_id: str
    row_label: str
    column_label: str
    raw_value: str
    amount_yuan: Fraction | None          # 非金额单元为 None
    unit: str                              # 实际生效单位（继承后）
    unit_inherited: bool                   # 单位是否继承自表标题
    source_id: str
    period_id: str | None = None
    version: int = 1                       # 更正后产生新版本

    def identity(self) -> tuple[str, str, str]:
        return self.table_id, self.row_label, self.column_label


@dataclass(frozen=True)
class TableInfo:
    table_id: str
    source_id: str
    title_unit: str | None                 # 表标题声明的单位
    columns: tuple[str, ...]
    footnote: str | None


@dataclass
class Evidence:
    """一条证据：形态、来源编号、以及可回查的具体锚点。"""

    kind: EvidenceKind
    source_ids: tuple[str, ...]
    anchor: str = ""                       # 原句或单元坐标
    independent: bool = True               # 媒体转载不算独立依据

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "source_ids": list(self.source_ids),
            "anchor": self.anchor,
            "independent": self.independent,
        }


@dataclass
class Claim:
    """一条原子主张。一个答复里有两个指标时拆成两条主张分别复核。"""

    claim_id: str
    metric: MetricKind
    basis: CompareBasis
    statement_kind: StatementKind
    text: str                              # 主张文字（原文或规范化描述）
    evidence: list[Evidence] = field(default_factory=list)
    # 比率类主张：分子/分母指向具体单元坐标或来源编号。
    numerator_ref: str | None = None
    denominator_ref: str | None = None
    asserted_ratio: Fraction | None = None     # 来源声称的比率（文字口径）
    computed_ratio: Fraction | None = None     # 系统按实际输入数算出的比率
    asserted_amount_yuan: Fraction | None = None
    period_id: str | None = None
    review: ReviewState = ReviewState.PENDING
    review_note: str = ""
    depends_on_cells: list[tuple[str, str, str, int]] = field(default_factory=list)
    # 同一主张的后续版本号（期间重述/单位澄清时）。
    version: int = 1
    superseded_by: str | None = None
    flags: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """稳定指纹：指标+口径+期间+文字，不随复核状态变化。"""
        key = "|".join([
            self.metric.value, self.basis.value, self.statement_kind.value,
            self.period_id or "", self.text,
        ])
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        def frac(value: Fraction | None) -> str | None:
            return None if value is None else str(value)

        return {
            "claim_id": self.claim_id,
            "metric": self.metric.value,
            "basis": self.basis.value,
            "statement_kind": self.statement_kind.value,
            "text": self.text,
            "evidence": [e.to_dict() for e in self.evidence],
            "numerator_ref": self.numerator_ref,
            "denominator_ref": self.denominator_ref,
            "asserted_ratio": frac(self.asserted_ratio),
            "computed_ratio": frac(self.computed_ratio),
            "asserted_amount_yuan": frac(self.asserted_amount_yuan),
            "period_id": self.period_id,
            "review": self.review.value,
            "review_note": self.review_note,
            "depends_on_cells": [list(c) for c in self.depends_on_cells],
            "version": self.version,
            "superseded_by": self.superseded_by,
            "flags": list(self.flags),
        }


@dataclass
class Finding:
    """校验发现：冲突/风险/说明，全部带来源编号，不替使用者选边。"""

    severity: Severity
    code: str
    message: str
    source_ids: tuple[str, ...]
    claim_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "source_ids": list(self.source_ids),
            "claim_ids": list(self.claim_ids),
        }


@dataclass
class ReviewEvent:
    """复核历史事件（追加，不改写）。"""

    claim_id: str
    from_state: ReviewState
    to_state: ReviewState
    note: str
    source_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "note": self.note,
            "source_id": self.source_id,
        }


@dataclass
class OutputRecord:
    """对外材料登记。系统只登记本地记录，不执行真实发送。"""

    output_id: str
    recipient: str
    state: OutputState
    text: str
    claim_ids: list[str]
    sources: list[str]
    formula_text: str | None = None          # 作者当时算法，仅作记录不可信
    snapshot_text: str | None = None         # 发送当时内容快照
    correction_notices: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data


@dataclass
class CorrectionNotice:
    """针对已发材料的更正条目：只列具体主张和收件人，不整批撤回。"""

    notice_id: str
    output_id: str
    recipient: str
    claim_id: str
    reason: str
    source_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "notice_id": self.notice_id,
            "output_id": self.output_id,
            "recipient": self.recipient,
            "claim_id": self.claim_id,
            "reason": self.reason,
            "source_ids": list(self.source_ids),
        }
