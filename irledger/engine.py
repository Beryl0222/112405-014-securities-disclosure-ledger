"""校验引擎：登记主张并执行口径校验。

所有校验只产生 :class:`Finding`，冲突并列来源，不替财务人员选“合理的一边”。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction

from .models import (
    Cell, Claim, CompareBasis, Evidence, EvidenceKind, Finding, MetricKind,
    ReviewState, Severity, StatementKind, TableInfo,
)
from .narrative import DiscourseTracker, RawStatement
from .taxonomy import classify_metric, detect_compare_basis
from .textnum import format_percent

_CLAIM_SEQ = "claim"


@dataclass
class ValidationResult:
    claims: list[Claim]
    findings: list[Finding]
    digit_collisions: dict[str, list[str]]


class ClaimFactory:
    """主张编号与组装。编号在一次资料包内按登记顺序稳定分配。"""

    def __init__(self) -> None:
        self._seq = 0

    def new_id(self) -> str:
        self._seq += 1
        return f"CL-{self._seq:03d}"

    def make(self, *, metric: MetricKind, basis: CompareBasis,
             statement_kind: StatementKind, text: str,
             evidence: list[Evidence], asserted_ratio: Fraction | None = None,
             computed_ratio: Fraction | None = None,
             asserted_amount_yuan: Fraction | None = None,
             period_id: str | None = None,
             numerator_ref: str | None = None,
             denominator_ref: str | None = None,
             depends_on_cells: list[tuple[str, str, str, int]] | None = None,
             flags: list[str] | None = None,
             review: ReviewState = ReviewState.PENDING,
             review_note: str = "") -> Claim:
        return Claim(
            claim_id=self.new_id(),
            metric=metric,
            basis=basis,
            statement_kind=statement_kind,
            text=text,
            evidence=evidence,
            asserted_ratio=asserted_ratio,
            computed_ratio=computed_ratio,
            asserted_amount_yuan=asserted_amount_yuan,
            period_id=period_id,
            numerator_ref=numerator_ref,
            denominator_ref=denominator_ref,
            depends_on_cells=depends_on_cells or [],
            flags=flags or [],
            review=review,
            review_note=review_note,
        )


def _cell_ref(cell: Cell) -> str:
    return f"{cell.table_id}/{cell.row_label}/{cell.column_label}@v{cell.version}"


def build_table_claims(cells: list[Cell], tables: list[TableInfo],
                       factory: ClaimFactory,
                       period_order: dict[str, int],
                       existing_facts: set[tuple] | None = None,
                       existing_ratios: set[tuple] | None = None,
                       table_filter: set[str] | None = None) -> tuple[list[Claim], list[Finding]]:
    """为表的指标行派生比率主张并登记单点事实主张。

    ``existing_facts``：已登记的事实单元坐标+版本，跳过避免重复；
    ``existing_ratios``：已登记的分子/分母单元对，跳过避免重复；
    ``table_filter``：只处理指定表（更正后增量重算时使用）。
    """
    claims: list[Claim] = []
    findings: list[Finding] = []
    # 就地更新调用方传入的集合，保证跨次摄入的去重状态能回写。
    existing_facts = existing_facts if existing_facts is not None else set()
    existing_ratios = existing_ratios if existing_ratios is not None else set()
    grouped: dict[tuple[str, str], list[Cell]] = defaultdict(list)
    for cell in cells:
        if table_filter is not None and cell.table_id not in table_filter:
            continue
        grouped[(cell.table_id, cell.row_label)].append(cell)

    for (table_id, row_label), row_cells in grouped.items():
        metric = classify_metric(row_label)
        fx_basis = None
        for basis_word in ("固定汇率", "报告汇率"):
            if basis_word in row_label:
                fx_basis = detect_compare_basis(row_label)
        ordered = sorted(
            row_cells,
            key=lambda c: period_order.get(c.period_id or "", 999),
        )
        for cell in ordered:
            fact_key = (cell.table_id, cell.row_label, cell.column_label, cell.version)
            if fact_key in existing_facts:
                continue
            # 单点事实主张：表内每个当前单元都可回查。
            claims.append(factory.make(
                metric=metric,
                basis=CompareBasis.NONE,
                statement_kind=StatementKind.FACT,
                text=f"{table_id} {row_label} {cell.column_label} = {cell.raw_value}{cell.unit}",
                evidence=[Evidence(EvidenceKind.DIRECT_QUOTE, (cell.source_id,), _cell_ref(cell))],
                asserted_amount_yuan=cell.amount_yuan,
                period_id=cell.period_id,
                depends_on_cells=[(cell.table_id, cell.row_label, cell.column_label, cell.version)],
            ))
            existing_facts.add(fact_key)
        # 相邻两列构成一个比较；比较维度由表头/脚注决定，缺口径时标“不明”。
        for earlier, later in zip(ordered, ordered[1:]):
            ratio_key = (
                later.table_id, later.row_label, later.column_label, later.version,
                earlier.table_id, earlier.row_label, earlier.column_label, earlier.version,
            )
            if ratio_key in existing_ratios:
                continue
            if earlier.amount_yuan is None or later.amount_yuan is None or earlier.amount_yuan == 0:
                findings.append(Finding(
                    Severity.CONFLICT, "no-denominator",
                    f"{table_id} {row_label} 缺少可作分母的金额单元，无法派生比率",
                    (earlier.source_id, later.source_id),
                ))
                continue
            ratio = (later.amount_yuan - earlier.amount_yuan) / earlier.amount_yuan
            basis = fx_basis or _infer_pair_basis(table_id, tables, earlier, later)
            claims.append(factory.make(
                metric=metric,
                basis=basis,
                statement_kind=StatementKind.DERIVED,
                text=(f"{table_id} {row_label} {later.column_label} 对 {earlier.column_label}"
                      f" 变化 {format_percent(ratio)}"),
                evidence=[Evidence(
                    EvidenceKind.DERIVED_RATIO,
                    (earlier.source_id, later.source_id),
                    f"{_cell_ref(later)} ÷ {_cell_ref(earlier)}",
                )],
                computed_ratio=ratio,
                period_id=later.period_id,
                numerator_ref=_cell_ref(later),
                denominator_ref=_cell_ref(earlier),
                depends_on_cells=[
                    (later.table_id, later.row_label, later.column_label, later.version),
                    (earlier.table_id, earlier.row_label, earlier.column_label, earlier.version),
                ],
                review=ReviewState.CONFIRMED,
                review_note="由实际输入数派生，可回查分子分母",
            ))
            existing_ratios.add(ratio_key)
    return claims, findings


def _infer_pair_basis(table_id: str, tables: list[TableInfo],
                      earlier: Cell, later: Cell) -> CompareBasis:
    table = next((t for t in tables if t.table_id == table_id), None)
    footnote = table.footnote or "" if table else ""
    basis = detect_compare_basis(footnote + later.column_label + earlier.column_label)
    if basis != CompareBasis.NONE:
        return basis
    joined = later.column_label + earlier.column_label
    # 相邻季度命名（本季/上一季）为环比；本财年/上财年首季为去年同期同比。
    if "上一季" in earlier.column_label or "本季" in later.column_label:
        return CompareBasis.QOQ
    if "财年" in joined:
        return CompareBasis.YOY
    return CompareBasis.UNKNOWN


def build_narrative_claims(statements: list[RawStatement],
                           tracker: DiscourseTracker,
                           factory: ClaimFactory) -> tuple[list[Claim], list[Finding]]:
    claims: list[Claim] = []
    findings: list[Finding] = []
    for stmt in statements:
        tracker.feed(stmt)
        evidence = [Evidence(stmt.evidence_kind, (stmt.source_id,), stmt.fragment)]
        review = ReviewState.PENDING
        note = ""
        flags = list(stmt.flags)

        if stmt.statement_kind == StatementKind.FORECAST:
            review = ReviewState.REJECTED
            note = "预测/条件性目标，不能作为已实现事实对外陈述"
            flags.append("预测不得当事实")
        elif stmt.explicit_disclaimer and stmt.metric == MetricKind.REVENUE:
            note = "答复明确否认收入口径，该句是口径澄清而非收入事实"
            flags.append("澄清-否认当前指标")
        elif stmt.statement_kind == StatementKind.QUESTION_PRESUPPOSITION:
            note = "提问者预设，未经答复确认前不是正式陈述"
            flags.append("提问预设")

        claim = factory.make(
            metric=stmt.metric,
            basis=stmt.basis,
            statement_kind=stmt.statement_kind,
            text=stmt.fragment,
            evidence=evidence,
            asserted_ratio=stmt.ratio,
            asserted_amount_yuan=stmt.amounts[0][0] if stmt.amounts else None,
            flags=flags,
            review=review,
            review_note=note,
        )
        claims.append(claim)

    # 承接/换指标发现统一由话语跟踪器产出，每个来源只提示一次。
    seen_resolution_sources: set[str] = set()
    for resolution in tracker.resolutions:
        if resolution.switched and resolution.source_id not in seen_resolution_sources:
            seen_resolution_sources.add(resolution.source_id)
            findings.append(Finding(
                Severity.HAZARD, "ellipsis-resolution",
                resolution.note,
                (resolution.source_id,),
            ))
    return claims, findings


def check_unit_inheritance(cells: list[Cell], tables: list[TableInfo]) -> list[Finding]:
    """表内省略单位只能继承本表标题；无标题单位的表必须显式标注。"""
    findings: list[Finding] = []
    table_units = {t.table_id: t.title_unit for t in tables}
    for cell in cells:
        title_unit = table_units.get(cell.table_id)
        if title_unit is None and not cell.unit:
            findings.append(Finding(
                Severity.CONFLICT, "unit-missing",
                f"{cell.table_id} 无标题单位且单元 {cell.row_label}/{cell.column_label} 未标单位，"
                "禁止借用其他表单位",
                (cell.source_id,),
            ))
        if cell.unit and cell.unit != title_unit and title_unit is not None:
            findings.append(Finding(
                Severity.CONFLICT, "unit-cross-table",
                f"单元 {_cell_ref(cell)} 单位 {cell.unit} 与本表标题单位 {title_unit} 不一致",
                (cell.source_id,),
            ))
    return findings


def build_digit_collision_index(cells: list[Cell]) -> dict[str, list[str]]:
    """同数字字符串在不同单位/表中出现的撞形索引。

    例如 800 万元与 800 百万元字面相同、量级不同，换算核对前不得混用。
    """
    index: dict[str, list[str]] = defaultdict(list)
    for cell in cells:
        anchor = f"{cell.table_id}/{cell.row_label}/{cell.column_label}@{cell.unit}"
        index[cell.raw_value].append(anchor)
    return {raw: anchors for raw, anchors in index.items() if len(anchors) >= 2}


def check_ratio_against_inputs(claims: list[Claim]) -> list[Finding]:
    """派生比率必须能用实际输入数复算；文字声称值与复算值冲突时并列两边。"""
    findings: list[Finding] = []
    for claim in claims:
        if claim.asserted_ratio is None or claim.computed_ratio is None:
            continue
        if claim.asserted_ratio != claim.computed_ratio:
            findings.append(Finding(
                Severity.CONFLICT, "ratio-mismatch",
                f"主张 {claim.claim_id} 文字比率 {format_percent(claim.asserted_ratio)} "
                f"与输入数复算 {format_percent(claim.computed_ratio)} 不一致，并列两边不自行取舍",
                tuple(source for ev in claim.evidence for source in ev.source_ids),
                (claim.claim_id,),
            ))
    return findings


def check_text_number_conflicts(claims: list[Claim], registry) -> list[Finding]:
    """数字与文字不一致：列出冲突及来源，不选合理的一边。

    以主张依赖的具体单元版本为准核对（旧版本主张对旧单元仍然自洽，
    单元被更正后该主张应处于“依据失效”，而不是被误报成数字冲突）。
    只检查以表单元为依据、且为单点事实的主张。
    """
    findings: list[Finding] = []
    for claim in claims:
        if claim.asserted_amount_yuan is None or not claim.depends_on_cells:
            continue
        if claim.statement_kind != StatementKind.FACT or claim.basis != CompareBasis.NONE:
            continue
        for table_id, row_label, column_label, version in claim.depends_on_cells:
            versions = registry.cells.get((table_id, row_label, column_label))
            if not versions:
                continue
            cell = next((c for c in versions if c.version == version), versions[-1])
            if cell.amount_yuan is not None and cell.amount_yuan != claim.asserted_amount_yuan:
                sources = tuple(ev.source_ids[0] for ev in claim.evidence if ev.source_ids)
                findings.append(Finding(
                    Severity.CONFLICT, "text-number-conflict",
                    f"主张 {claim.claim_id} 文字金额与所引用单元 {_cell_ref(cell)} 的数值不一致，"
                    "并列两边，不自行选合理的一边",
                    sources, (claim.claim_id,),
                ))
    return findings
