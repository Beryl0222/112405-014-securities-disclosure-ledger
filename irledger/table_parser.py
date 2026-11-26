"""表格原文解析：保留行列标题与脚注，单元只继承本表标题单位。

关键规则：
* 表内省略单位的单元继承所属表标题单位；
* 禁止越表、越页借用另一张表的单位；
* 脚注中的期间说明原样保留，供期间口径回查。
"""
from __future__ import annotations

import re
from fractions import Fraction

from .models import Cell, TableInfo
from .textnum import UNIT_TO_YUAN, to_yuan

_UNIT_RE = re.compile(r"单位\s*(亿元|百万元|万元|元)")
_TABLE_ID_RE = re.compile(r"表([一二三四五六七八九十]+)")
_CN_ORDINAL = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CELL_RE = re.compile(r"(?P<label>[^；。\d]+?)\s*(?P<values>\d+(?:\.\d+)?(?:\s*[、,，]\s*\d+(?:\.\d+)?)*)\s*$")


def table_ordinal(text: str) -> int | None:
    match = _TABLE_ID_RE.search(text)
    if not match:
        return None
    return _CN_ORDINAL.get(match.group(1))


def _split_clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[；。]", text) if part.strip()]


def _parse_columns(clause: str) -> list[str]:
    _, _, rest = clause.partition("：")
    return [item.strip() for item in re.split(r"[、,，]", rest) if item.strip()]


def _parse_row(clause: str) -> tuple[str, list[str]] | None:
    match = _CELL_RE.match(clause)
    if not match:
        return None
    values = [v.strip() for v in re.split(r"[、,，]", match.group("values")) if v.strip()]
    label = match.group("label").strip()
    if label.startswith("行：") or label.startswith("行:"):
        label = label[2:].strip()
    return label, values


def parse_table(record_id: str, table_id: str, text: str,
                period_resolver) -> tuple[TableInfo, list[Cell]]:
    """解析一张表的原文。

    ``period_resolver(column_label)`` 把列标题映射到登记的期间编号，
    映射不到时期间为 ``None``，由后续校验提示，不做猜测。
    """
    title_unit_match = _UNIT_RE.search(text)
    title_unit = title_unit_match.group(1) if title_unit_match else None

    clauses = _split_clauses(text)
    columns: list[str] = []
    footnote: str | None = None
    cells: list[Cell] = []

    for clause in clauses:
        if clause.startswith("列"):
            columns = _parse_columns(clause)
            continue
        if clause.startswith("脚注"):
            footnote = clause
            continue
        row = _parse_row(clause)
        if row is None:
            continue
        row_label, values = row
        if len(values) != len(columns):
            # 行列数量对不上属于资料冲突，保留原文并抛出由上层登记。
            raise ValueError(f"{record_id} 行 {row_label!r} 数值数 {len(values)} 与列数 {len(columns)} 不一致")
        for column_label, raw in zip(columns, values):
            unit = title_unit  # 省略单位时只继承本表标题单位
            amount = None
            if unit is not None and unit in UNIT_TO_YUAN:
                amount = to_yuan(Fraction(raw), unit)
            cells.append(Cell(
                table_id=table_id,
                row_label=row_label,
                column_label=column_label,
                raw_value=raw,
                amount_yuan=amount,
                unit=unit or "",
                unit_inherited=unit is not None,
                source_id=record_id,
                period_id=period_resolver(column_label, row_label, footnote),
            ))

    info = TableInfo(
        table_id=table_id,
        source_id=record_id,
        title_unit=title_unit,
        columns=tuple(columns),
        footnote=footnote,
    )
    return info, cells
