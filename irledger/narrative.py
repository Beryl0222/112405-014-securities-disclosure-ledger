"""正文、问答转写、媒体文字的陈述解析与话语承接分析。

处理三类易错点：
1. 省略承接——“增长两成”可能承接上一问的订单金额而非当前提问的收入；
2. 问答换指标——提问把前文订单口径的比率套到收入上；
3. 预测/事实/提问预设混写——按措辞性质分别标注，不合并成正式陈述。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

from .models import CompareBasis, EvidenceKind, MetricKind, StatementKind
from .taxonomy import classify_metric, detect_compare_basis
from .textnum import extract_amounts, extract_ratio

_FORECAST_WORDS = ("希望", "预计", "目标", "若", "预期", "展望", "不能当已实现", "不能当作已实现")
_DISCLAIMER_WORDS = ("不能当已实现", "不能当作已实现", "不是收入", "不是")
_ANAPHORA_WORDS = ("也", "这里", "前面", "该", "同上")
_SPLIT_RE = re.compile(r"[；;]")


@dataclass
class RawStatement:
    """从一条原文切出的原始陈述，尚未登记为主张。"""

    source_id: str
    record_kind: str
    fragment: str
    metric: MetricKind
    basis: CompareBasis
    statement_kind: StatementKind
    evidence_kind: EvidenceKind
    ratio: Fraction | None = None
    amounts: list[tuple[Fraction, str]] = field(default_factory=list)  # (元, 原单位)
    anaphoric: bool = False
    forecast_gated: bool = False
    explicit_disclaimer: bool = False
    pointer: str | None = None        # 口径指引，如“请用表一最后一行”
    flags: list[str] = field(default_factory=list)


def _statement_kind(record_kind: str, fragment: str) -> tuple[StatementKind, EvidenceKind, bool, bool]:
    """返回 (陈述性质, 证据形态, 是否预测条件句, 是否含免责/否定澄清)。"""
    forecast = any(word in fragment for word in _FORECAST_WORDS)
    disclaimer = any(word in fragment for word in _DISCLAIMER_WORDS)
    if record_kind == "question":
        return StatementKind.QUESTION_PRESUPPOSITION, EvidenceKind.TRANSCRIPT, forecast, disclaimer
    if record_kind == "media":
        return StatementKind.MEDIA, EvidenceKind.DIRECT_QUOTE, forecast, disclaimer
    if record_kind == "answer":
        kind = StatementKind.FORECAST if forecast else StatementKind.FACT
        return kind, EvidenceKind.TRANSCRIPT, forecast, disclaimer
    # report 正文
    kind = StatementKind.FORECAST if forecast else StatementKind.FACT
    return kind, EvidenceKind.DIRECT_QUOTE, forecast, disclaimer


def parse_statement(source_id: str, record_kind: str, text: str,
                    default_unit: str | None = None,
                    context_metric: MetricKind | None = None) -> list[RawStatement]:
    """把一条原文切成若干陈述片段并分类。

    ``context_metric``：紧邻提问的指标，答复片段本身没有指标词时受控继承，
    并加“指标承接提问”标记，区别于片段自身明示的指标。
    """
    statements: list[RawStatement] = []
    body = re.sub(r"^(分析师|财务负责人|负责人|媒体甲|媒体乙)[:：]", "", text).strip()
    for fragment in [part.strip() for part in _SPLIT_RE.split(body) if part.strip()]:
        metric = classify_metric(fragment)
        inherited_from_question = False
        if metric == MetricKind.UNKNOWN and context_metric is not None and record_kind == "answer":
            metric = context_metric
            inherited_from_question = True
        basis = detect_compare_basis(fragment)
        kind, evidence_kind, forecast, disclaimer = _statement_kind(record_kind, fragment)
        ratio = extract_ratio(fragment)
        amounts = extract_amounts(fragment, default_unit)
        anaphoric = any(word in fragment for word in _ANAPHORA_WORDS) and ratio is not None
        pointer = None
        if "请用" in fragment or "见表" in fragment or "说前面" in fragment or "不是收入" in fragment:
            pointer = fragment
        flags: list[str] = []
        if inherited_from_question:
            flags.append("指标承接提问")
        if "其中" in fragment:
            # “其中”引出的是上级指标的拆分项，不能当成独立的顶层指标。
            flags.append("其中-从属拆分")
        if "同口径" in fragment:
            flags.append("同口径-须回查比较基准")
        if "未拆地区" in fragment or "未拆" in fragment:
            flags.append("维度未拆分")
        if "未提供可比" in fragment or "无可比" in fragment:
            flags.append("缺可比基数")
        statements.append(RawStatement(
            source_id=source_id,
            record_kind=record_kind,
            fragment=fragment,
            metric=metric,
            basis=basis,
            statement_kind=kind,
            evidence_kind=evidence_kind,
            ratio=ratio,
            amounts=amounts,
            anaphoric=anaphoric,
            forecast_gated=forecast,
            explicit_disclaimer=disclaimer,
            pointer=pointer,
            flags=flags,
        ))
    return statements


@dataclass
class AnaphoraResolution:
    """一处省略/承接的解析结果。"""

    source_id: str
    fragment: str
    sentence_metric: MetricKind
    inherited_metric: MetricKind | None
    inherited_basis: CompareBasis | None
    switched: bool
    note: str


class DiscourseTracker:
    """按资料顺序跟踪比率承接，识别“两成”式省略与问答换指标。"""

    def __init__(self) -> None:
        # 最近一次带比率陈述的指标与口径，即后文可能承接的对象。
        self._last_ratio: tuple[MetricKind, CompareBasis, str] | None = None
        self.resolutions: list[AnaphoraResolution] = []
        # 已生成过承接解析的来源，避免同一条原文重复提示。
        self._resolved_sources: set[str] = set()
        # 显式澄清（答复中指明比率真正指向的指标）。
        self.explicit_resolutions: list[tuple[str, str, MetricKind]] = []

    def feed(self, stmt: RawStatement) -> AnaphoraResolution | None:
        resolution: AnaphoraResolution | None = None
        # 只有真正带比率且含承接词的片段才解析，避免同一条原文的其他片段重复触发。
        if (stmt.ratio is not None and stmt.anaphoric and self._last_ratio is not None
                and stmt.source_id not in self._resolved_sources):
            self._resolved_sources.add(stmt.source_id)
            inherited_metric, inherited_basis, inherited_source = self._last_ratio
            switched = inherited_metric != stmt.metric and stmt.metric != MetricKind.UNKNOWN
            note = (
                f"比率措辞为省略承接，最近比率出自 {inherited_source} 的"
                f"{inherited_metric.value}/{inherited_basis.value}"
            )
            if switched:
                note += f"；当前句子主语为{stmt.metric.value}，存在问答换指标风险"
            resolution = AnaphoraResolution(
                source_id=stmt.source_id,
                fragment=stmt.fragment,
                sentence_metric=stmt.metric,
                inherited_metric=inherited_metric,
                inherited_basis=inherited_basis,
                switched=switched,
                note=note,
            )
            self.resolutions.append(resolution)
            stmt.flags.append("省略承接" + ("-换指标" if switched else ""))
        # 答复中的显式澄清：两成说订单，不是收入。
        if stmt.pointer and ("不是收入" in stmt.fragment or "说前面" in stmt.fragment):
            target = MetricKind.ORDER if "订单" in stmt.fragment else MetricKind.UNKNOWN
            self.explicit_resolutions.append((stmt.source_id, stmt.fragment, target))
        if stmt.ratio is not None and not stmt.anaphoric:
            self._last_ratio = (stmt.metric, stmt.basis, stmt.source_id)
        return resolution
