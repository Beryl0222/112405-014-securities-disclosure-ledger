"""更正与澄清的解析、单元版本管理及主张影响传播。

规则要点：
* 更正只改其指明的列/单元，范围外的行、期间保持原值；
* 分母更正、期间重述、单位澄清后，依赖该单元的派生比率一律失去依据，
  即使更正文字里没有出现比率字符串——按依赖图而不是数字字符串搜索；
* 同答复中未被更正的另一项指标，其已确认说明继续有效。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

from .models import Cell, Claim
from .table_parser import table_ordinal
from .textnum import UNIT_TO_YUAN, to_yuan

_CORRECTION_RE = re.compile(
    r"表(?P<table>[一二三四五六七八九十]+)(?P<head>.+?)"
    r"应为\s*(?P<new>\d+(?:\.\d+)?)\s*(?P<unit>亿元|百万元|万元|元)，原\s*(?P<old>\d+(?:\.\d+)?)"
)
# 行标题以这些指标词开头，用于把“上财年首季集团确认收入”切成列+行。
_ROW_START_WORDS = ("集团确认收入", "确认收入", "预收款", "海外订单", "订单", "存量", "余额")
_COLUMN_HINT_WORDS = ("本财年", "上财年", "本季", "上一季", "首季")
_SCOPE_KEEP_RE = re.compile(r"其余(.+?)不变")
_CLARIFY_RE = re.compile(r"表(?P<table>[一二三四五六七八九十]+)(?P<values>[\d和、\s]+?)都是(?P<unit>亿元|百万元|万元|元)")
_WRONG_UNIT_RE = re.compile(r"误写(?P<unit>亿元|百万元|万元|元)不适用")


def _split_column_row(head: str) -> tuple[str | None, str | None]:
    """把“上财年首季集团确认收入”拆成列关键字与行关键字。

    列标题由期间词引导，行标题由指标词引导；识别不出时保留整串给行匹配，
    并把列留空，由单元检索决定，不猜测。
    """
    for row_word in _ROW_START_WORDS:
        idx = head.find(row_word)
        if idx > 0:
            return head[:idx], head[idx:]
    if any(head.startswith(word) for word in _COLUMN_HINT_WORDS):
        return head, None
    return None, head


@dataclass
class CorrectionSpec:
    source_id: str
    kind: str                       # correction / clarification
    table_id: str
    column_keyword: str | None
    row_keyword: str | None
    new_raw_value: str | None
    new_unit: str | None
    wrong_unit: str | None          # 澄清中被否定的单位
    old_raw_value: str | None
    scope_note: str | None
    target_values: tuple[str, ...]  # 澄清类指明的具体数字
    text: str


@dataclass
class CellRegistry:
    """单元登记：按坐标保存所有版本，当前版本号最大。"""

    cells: dict[tuple[str, str, str], list[Cell]] = field(default_factory=dict)

    def add(self, cell: Cell) -> None:
        self.cells.setdefault(cell.identity(), []).append(cell)

    def current(self, table_id: str, row_label: str, column_label: str) -> Cell | None:
        versions = self.cells.get((table_id, row_label, column_label))
        return versions[-1] if versions else None

    def all_current(self) -> list[Cell]:
        return [versions[-1] for versions in self.cells.values()]

    def find(self, table_id: str, row_keyword: str, column_keyword: str | None) -> list[Cell]:
        result = []
        for identity, versions in self.cells.items():
            tid, row_label, column_label = identity
            if tid != table_id or row_keyword not in row_label:
                continue
            if column_keyword is None or column_keyword in column_label:
                result.append(versions[-1])
        return result

    def supersede(self, old: Cell, *, new_raw: str | None = None,
                  new_unit: str | None = None, source_id: str) -> Cell:
        """产生单元新版本；金额按新值/新单位重算。"""
        raw = new_raw if new_raw is not None else old.raw_value
        unit = new_unit if new_unit is not None else old.unit
        amount = to_yuan(Fraction(raw), unit) if unit in UNIT_TO_YUAN else None
        new_cell = Cell(
            table_id=old.table_id,
            row_label=old.row_label,
            column_label=old.column_label,
            raw_value=raw,
            amount_yuan=amount,
            unit=unit,
            unit_inherited=old.unit_inherited and new_unit is None,
            source_id=source_id,
            period_id=old.period_id,
            version=old.version + 1,
        )
        self.cells[old.identity()].append(new_cell)
        return new_cell


def parse_correction(source_id: str, kind: str, text: str, table_id_by_ordinal: dict[int, str]) -> CorrectionSpec:
    scope_match = _SCOPE_KEEP_RE.search(text)
    scope_note = scope_match.group(0) if scope_match else None

    if kind == "correction":
        match = _CORRECTION_RE.search(text)
        if not match:
            raise ValueError(f"{source_id} 更正句式无法解析: {text}")
        ordinal = table_ordinal("表" + match.group("table"))
        column_keyword, row_keyword = _split_column_row(match.group("head").strip())
        return CorrectionSpec(
            source_id=source_id,
            kind=kind,
            table_id=table_id_by_ordinal[ordinal],
            column_keyword=column_keyword,
            row_keyword=row_keyword,
            new_raw_value=match.group("new"),
            new_unit=match.group("unit"),
            wrong_unit=None,
            old_raw_value=match.group("old"),
            scope_note=scope_note,
            target_values=(),
            text=text,
        )

    # clarification：单位澄清
    clarify = _CLARIFY_RE.search(text)
    wrong = _WRONG_UNIT_RE.search(text)
    if not clarify:
        raise ValueError(f"{source_id} 澄清句式无法解析: {text}")
    ordinal = table_ordinal("表" + clarify.group("table"))
    values = tuple(v for v in re.split(r"[、和\s]+", clarify.group("values").strip()) if v)
    return CorrectionSpec(
        source_id=source_id,
        kind=kind,
        table_id=table_id_by_ordinal[ordinal],
        column_keyword=None,
        row_keyword=None,
        new_raw_value=None,
        new_unit=clarify.group("unit"),
        wrong_unit=wrong.group("unit") if wrong else None,
        old_raw_value=None,
        scope_note=scope_note,
        target_values=values,
        text=text,
    )


def apply_correction(spec: CorrectionSpec, registry: CellRegistry) -> list[Cell]:
    """把更正/澄清落到单元上，返回受影响的新单元列表。范围外单元不动。"""
    affected: list[Cell] = []
    if spec.kind == "correction":
        matches = registry.find(spec.table_id, spec.row_keyword or "", spec.column_keyword)
        if not matches:
            raise ValueError(f"{spec.source_id} 找不到目标单元: 表{spec.table_id} {spec.row_keyword}/{spec.column_keyword}")
        for old in matches:
            if spec.old_raw_value is not None and old.raw_value != spec.old_raw_value:
                # 原数对不上：登记冲突，不自行套用。
                raise ValueError(
                    f"{spec.source_id} 称原值为 {spec.old_raw_value}，"
                    f"实际单元 {old.identity()} 当前为 {old.raw_value}，拒绝自动套用"
                )
            affected.append(registry.supersede(
                old, new_raw=spec.new_raw_value, new_unit=spec.new_unit, source_id=spec.source_id
            ))
        return affected

    # 单位澄清：只对该表、且原文数字在指明清单内的单元生效。
    for identity, versions in list(registry.cells.items()):
        table_id, _row, _column = identity
        if table_id != spec.table_id:
            continue
        old = versions[-1]
        if old.raw_value not in spec.target_values:
            continue
        if old.unit == spec.new_unit:
            continue
        affected.append(registry.supersede(old, new_unit=spec.new_unit, source_id=spec.source_id))
    return affected


def claims_dependent_on(cells: list[Cell], claims: list[Claim]) -> list[Claim]:
    """按依赖图找出依赖受影响单元（含旧版本）的主张。"""
    affected_keys = {(c.table_id, c.row_label, c.column_label) for c in cells}
    hit: list[Claim] = []
    for claim in claims:
        for table_id, row_label, column_label, _version in claim.depends_on_cells:
            if (table_id, row_label, column_label) in affected_keys:
                hit.append(claim)
                break
    return hit


def propagate(changed_cells: list[Cell], claims: list[Claim],
              spec: CorrectionSpec) -> list[tuple[Claim, str]]:
    """传播更正影响，返回 (主张, 失效原因)。状态改写由登记层落账。"""
    result: list[tuple[Claim, str]] = []
    for claim in claims_dependent_on(changed_cells, claims):
        if spec.kind == "correction":
            reason = (
                f"依赖单元被 {spec.source_id} 更正（{spec.row_keyword}/{spec.column_keyword}），"
                "派生说法失去依据，需用新单元重新计算核对"
            )
        else:
            reason = (
                f"单位经 {spec.source_id} 澄清为{spec.new_unit}，"
                "原口径下的派生说法失去依据"
            )
        result.append((claim, reason))
    return result
