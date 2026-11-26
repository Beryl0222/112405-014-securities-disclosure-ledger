"""端到端管线：读取资料包 -> 解析登记 -> 校验 -> 应用更正 -> 挂接既有材料。

设计原则：
* 资料按编号顺序摄入，更正/澄清在其出现的时点生效；
* 所有金额比较基于换算为元的有理数，单位不跨表借用；
* 新增季度资料可重复调用 :meth:`Pipeline.ingest_records` 增量摄入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .corrections import (
    CellRegistry, CorrectionSpec, apply_correction, parse_correction, propagate,
)
from .engine import (
    ClaimFactory, ValidationResult, build_digit_collision_index,
    build_narrative_claims, build_table_claims, check_ratio_against_inputs,
    check_text_number_conflicts, check_unit_inheritance,
)
from .models import (
    Cell, Claim, CompareBasis, Finding, MetricKind,
    OutputRecord, OutputState, ReviewState, Severity, SourceRecord, StatementKind,
)
from .narrative import DiscourseTracker, parse_statement
from .table_parser import parse_table, table_ordinal
from .taxonomy import classify_metric, detect_compare_basis
from .textnum import extract_ratio


@dataclass
class IngestReport:
    new_record_ids: list[str] = field(default_factory=list)
    correction_specs: list[CorrectionSpec] = field(default_factory=list)
    invalidated_claims: list[tuple[str, str]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


class Pipeline:
    def __init__(self) -> None:
        self.records: dict[str, SourceRecord] = {}
        self.tables: list = []
        self.registry = CellRegistry()
        self.factory = ClaimFactory()
        self.claims: dict[str, Claim] = {}
        self.findings: list[Finding] = []
        self.tracker = DiscourseTracker()
        self.table_id_by_ordinal: dict[int, str] = {}
        self.period_order: dict[str, int] = {}
        self.periods: dict[str, dict[str, str]] = {}
        self.reprint_chains: dict[str, tuple[str, ...]] = {}
        self.outputs: dict[str, OutputRecord] = {}
        self.ingest_log: list[IngestReport] = []
        self._fact_keys: set[tuple] = set()
        self._ratio_keys: set[tuple] = set()
        self._last_question_metric: MetricKind | None = None

    # ------------------------------------------------------------------
    # 装载
    # ------------------------------------------------------------------
    @classmethod
    def from_case_file(cls, path: str | Path) -> "Pipeline":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        pipe = cls()
        pipe._load_objects(data.get("objects", {}))
        pipe.ingest_records(data.get("records", []))
        pipe._register_media_chains(data.get("records", []))
        pipe._load_existing(data.get("existing", []))
        return pipe

    def _load_objects(self, objects: dict) -> None:
        for index, period in enumerate(objects.get("periods", [])):
            self.periods[period["id"]] = {"from": period["from"], "to": period["to"]}
            # 期间按起始日排序，较早的期间作分母列。
        ordered = sorted(objects.get("periods", []), key=lambda p: p["from"])
        self.period_order = {p["id"]: i for i, p in enumerate(ordered)}
        # 表编号映射在摄入表格记录时建立；这里先登记 source->table id。
        self._table_sources = {t["source"]: t["id"] for t in objects.get("tables", [])}

    def _record_text(self, record_id: str, objects_loaded: bool = True) -> str:
        return self.records.get(record_id, SourceRecord(record_id, "", "")).text

    def _resolve_period(self, column_label: str, row_label: str, footnote: str | None) -> str | None:
        # 优先按登记期间的起始月匹配列标题语义。
        month_hint = None
        if footnote and "七月至九月" in footnote:
            month_hint = ("07", "09")
        for period_id, span in self.periods.items():
            if column_label in period_id:
                return period_id
        # 合成语料映射：本财年/本季/本期 -> 期间列表中较晚者，
        # 上财年/上一季/上期 -> 较早者。
        if self.period_order:
            later = max(self.period_order, key=lambda k: self.period_order[k])
            earlier = min(self.period_order, key=lambda k: self.period_order[k])
            if column_label.startswith(("本财年", "本季", "本期")):
                return later
            if column_label.startswith(("上财年", "上一季", "上期")):
                return earlier
        return None

    # ------------------------------------------------------------------
    # 增量摄入
    # ------------------------------------------------------------------
    def ingest_records(self, records: list[dict]) -> IngestReport:
        report = IngestReport()
        for raw in records:
            record_id = raw["id"]
            if record_id in self.records:
                continue  # 稳定编号已摄入，不重复处理
            record = SourceRecord(record_id, raw["kind"], raw["text"])
            self.records[record_id] = record
            report.new_record_ids.append(record_id)
            self._ingest_one(record, report)
        self._merge_findings(report.findings)
        self.ingest_log.append(report)
        return report

    def _merge_findings(self, new_findings: list) -> None:
        """按 (code, 来源, 信息) 去重后合并，避免同一提示重复堆叠。"""
        existing = {(f.code, f.source_ids, f.message) for f in self.findings}
        for finding in new_findings:
            key = (finding.code, finding.source_ids, finding.message)
            if key not in existing:
                self.findings.append(finding)
                existing.add(key)

    def _ingest_one(self, record: SourceRecord, report: IngestReport) -> None:
        if record.kind == "table":
            self._ingest_table(record)
        elif record.kind in ("correction", "clarification"):
            self._ingest_correction(record, report)
        elif record.kind == "media":
            self._ingest_media(record, report)
        else:
            self._ingest_narrative(record, report)

    def _ingest_table(self, record: SourceRecord) -> None:
        ordinal = table_ordinal(record.text)
        table_id = self._table_sources.get(record.id) or f"T{ordinal}"
        if ordinal is not None:
            self.table_id_by_ordinal[ordinal] = table_id
        info, cells = parse_table(record.id, table_id, record.text, self._resolve_period)
        self.tables.append(info)
        for cell in cells:
            self.registry.add(cell)
        claims, findings = build_table_claims(
            self.registry.all_current(), self.tables, self.factory, self.period_order,
            existing_facts=self._fact_keys, existing_ratios=self._ratio_keys,
        )
        self._adopt_claims(claims)
        self._merge_findings(findings)
        self._merge_findings(check_unit_inheritance(self.registry.all_current(), self.tables))

    def _ingest_narrative(self, record: SourceRecord, report: IngestReport) -> None:
        context_metric = self._last_question_metric if record.kind == "answer" else None
        statements = parse_statement(
            record.id, record.kind, record.text, context_metric=context_metric
        )
        if record.kind == "question":
            # 记录提问指标，供紧邻答复受控继承。
            identified = [s.metric for s in statements if s.metric != MetricKind.UNKNOWN]
            if identified:
                self._last_question_metric = identified[0]
        elif record.kind == "answer":
            # 答复结束后清除承接，避免跨问答对串味。
            self._last_question_metric = None
        claims, findings = build_narrative_claims(statements, self.tracker, self.factory)
        self._adopt_claims(claims)
        report.findings.extend(findings)

    def _ingest_media(self, record: SourceRecord, report: IngestReport) -> None:
        statements = parse_statement(record.id, record.kind, record.text)
        claims, findings = build_narrative_claims(statements, self.tracker, self.factory)
        # 媒体转述默认不作为独立依据，待与原始来源比对。
        for claim in claims:
            for evidence in claim.evidence:
                evidence.independent = False
            claim.flags.append("媒体来源待证独立性")
        self._adopt_claims(claims)
        report.findings.extend(findings)

    def _ingest_correction(self, record: SourceRecord, report: IngestReport) -> None:
        spec = parse_correction(record.id, record.kind, record.text, self.table_id_by_ordinal)
        before = {identity: versions[-1] for identity, versions in self.registry.cells.items()}
        changed = apply_correction(spec, self.registry)
        report.correction_specs.append(spec)

        # 受影响主张按依赖图判失效，不搜索数字字符串。
        impacted = propagate(changed, list(self.claims.values()), spec)
        for claim, reason in impacted:
            if claim.review in (ReviewState.CONFIRMED, ReviewState.PENDING):
                claim.review = ReviewState.INVALIDATED
                claim.review_note = reason
                claim.flags.append(f"依据失效:{record.id}")
                report.invalidated_claims.append((claim.claim_id, reason))

        # 单位澄清：把按被否定单位表述的主张标记为口径失效。
        if spec.kind == "clarification" and spec.wrong_unit:
            for claim in self.claims.values():
                if self._claim_uses_wrong_unit(claim, spec):
                    claim.review = ReviewState.INVALIDATED
                    note = f"使用了被 {record.id} 否定的单位 {spec.wrong_unit}，且该单位不适用于 {spec.table_id}"
                    claim.review_note = note
                    claim.flags.append(f"单位失效:{record.id}")
                    report.invalidated_claims.append((claim.claim_id, note))

        # 用新单元重新派生受影响表的比率主张（新版本主张，旧主张保留）。
        self._rederive_for(changed, record.id)

    def _claim_uses_wrong_unit(self, claim: Claim, spec: CorrectionSpec) -> bool:
        """判断主张是否在该表之外使用了被澄清否定的单位。

        命中条件：主张文字使用被否定单位且包含被澄清的数字，
        但其证据/依赖并不来自该表（即越表借单位）。
        """
        if spec.wrong_unit is None:
            return False
        text_hit = spec.wrong_unit in claim.text and any(v in claim.text for v in spec.target_values)
        if not text_hit:
            return False
        own_table = any(
            ref and ref.startswith(spec.table_id)
            for ref in (claim.numerator_ref, claim.denominator_ref)
        )
        own_source = any(
            source_id in ev.source_ids
            for ev in claim.evidence
            for source_id in [self._source_id_of_table(spec.table_id)]
            if source_id
        )
        return not (own_table or own_source)

    def _source_id_of_table(self, table_id: str) -> str | None:
        for table in self.tables:
            if table.table_id == table_id:
                return table.source_id
        return None

    def _rederive_for(self, changed_cells: list[Cell], source_id: str) -> None:
        """更正后只对受影响表的受影响行做增量派生。

        未变动行的事实/比率主张保持原编号；受影响单元产生新版本事实，
        并与同表同列的相邻当前单元重新计算比率。
        """
        affected_tables = {c.table_id for c in changed_cells}
        affected_rows = {(c.table_id, c.row_label) for c in changed_cells}
        cells = [
            c for c in self.registry.all_current()
            if (c.table_id, c.row_label) in affected_rows
        ]
        claims, findings = build_table_claims(
            cells, self.tables, self.factory, self.period_order,
            existing_facts=self._fact_keys, existing_ratios=self._ratio_keys,
            table_filter=affected_tables,
        )
        for claim in claims:
            claim.flags.append(f"更正后重算:{source_id}")
            self._adopt_claims([claim])
        self._merge_findings(findings)

    def _adopt_claims(self, new_claims: list[Claim]) -> None:
        for claim in new_claims:
            self.claims[claim.claim_id] = claim

    # ------------------------------------------------------------------
    # 媒体转载链
    # ------------------------------------------------------------------
    def _register_media_chains(self, records: list[dict]) -> None:
        original_by_media: dict[str, str] = {}
        for raw in records:
            if raw["kind"] != "media":
                continue
            text = raw["text"]
            # 合成语料：媒体甲称……，媒体乙原文转载甲稿。
            if "转载" in text:
                import re
                name_match = re.search(r"(媒体[甲乙丙丁])原文转载", text)
                origin_match = re.search(r"转载(媒体[甲乙丙丁])稿", text)
                reprinted = name_match.group(1) if name_match else "媒体乙"
                origin = origin_match.group(1) if origin_match else "媒体甲"
                original_record = original_by_media.get(origin)
                chain = tuple(x for x in (original_record, origin, raw["id"]) if x)
                self.reprint_chains[raw["id"]] = chain
            else:
                name_match = re.match(r"(媒体[甲乙丙丁])称", text)
                if name_match:
                    original_by_media[name_match.group(1)] = raw["id"]
        # 转载来源的媒体主张本身也不独立：把同指标媒体主张串成非独立证据。
        if self.reprint_chains:
            self._mark_reprinted_claims()

    def _mark_reprinted_claims(self) -> None:
        """把所有媒体主张标记为非独立证据，独立性须回查原始报道。"""
        for claim in self.claims.values():
            if claim.statement_kind == StatementKind.MEDIA:
                for evidence in claim.evidence:
                    evidence.independent = False

    # ------------------------------------------------------------------
    # 既有材料挂接
    # ------------------------------------------------------------------
    def _load_existing(self, existing: list[dict]) -> None:
        for raw in existing:
            output = OutputRecord(
                output_id=raw["id"],
                recipient=raw["recipient"],
                state=OutputState(raw["state"]),
                text=raw["text"],
                claim_ids=[],
                sources=list(raw.get("sources", [])),
                formula_text=raw.get("formula_text"),
            )
            self.outputs[output.output_id] = output
            self._link_output_claims(output)

    def _link_output_claims(self, output: OutputRecord) -> None:
        """把材料文字拆成原子主张并与已登记主张匹配；匹配不到单列发现。"""
        fragments = [part.strip() for part in output.text.replace("。", "，").split("，") if part.strip()]
        for fragment in fragments:
            metric = classify_metric(fragment)
            ratio = extract_ratio(fragment)
            matched = self._match_claim(fragment, metric, ratio)
            if matched is None:
                self.findings.append(Finding(
                    Severity.CONFLICT, "output-claim-unsupported",
                    f"材料 {output.output_id} 的说法 {fragment!r} 找不到对应已登记主张，"
                    "不能作为有依据的对外口径",
                    tuple(output.sources),
                ))
                continue
            output.claim_ids.append(matched.claim_id)
            if output.formula_text:
                matched.flags.append(f"材料公式存证:{output.output_id}")
            if output.state == OutputState.SENT:
                matched.flags.append(f"已发材料:{output.output_id}")
            # 预测被写成已实现事实：直接在主张上留痕，复核时拦截。
            if matched.statement_kind == StatementKind.FORECAST and ("已达" in fragment or "已实现" in fragment):
                matched.flags.append(f"预测冒充事实:{output.output_id}")

    def _match_claim(self, fragment: str, metric: MetricKind,
                     ratio: Fraction | None) -> Claim | None:
        candidates = [
            c for c in self.claims.values()
            if c.metric == metric or metric == MetricKind.UNKNOWN
        ]
        # 显式比率（增长20%/增两成）：同指标优先，防止“两成”在订单与收入间错配。
        if ratio is not None:
            ratio_hits = [c for c in candidates if c.asserted_ratio == ratio
                          or c.computed_ratio == ratio]
            same_metric = [c for c in ratio_hits if c.metric == metric]
            if same_metric:
                return same_metric[0]
            if ratio_hits:
                return ratio_hits[0]
        # 定性方向（环比下降/上升）：按派生比率符号与比较维度匹配。
        direction = self._direction_of(fragment)
        if direction is not None:
            basis = detect_compare_basis(fragment)
            directional = [
                c for c in candidates
                if c.computed_ratio is not None
                and (c.computed_ratio > 0) == direction
                and (basis == CompareBasis.NONE or c.basis == basis)
            ]
            if directional:
                return directional[0]
        # 金额型说法：按换算后的元值匹配。
        from .textnum import extract_amounts
        amounts = extract_amounts(fragment)
        if amounts:
            target = amounts[0][0]
            for c in candidates:
                if c.asserted_amount_yuan == target:
                    return c
        # 预测/目标类说法：匹配同指标的预测主张。
        if any(word in fragment for word in ("明年", "目标", "希望", "预期")):
            forecasts = [c for c in candidates if c.statement_kind == StatementKind.FORECAST]
            if forecasts:
                return forecasts[0]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _direction_of(fragment: str) -> bool | None:
        """返回 True=上升、False=下降、None=没有方向词。"""
        if any(word in fragment for word in ("下降", "减少", "下跌", "回落")):
            return False
        if any(word in fragment for word in ("增长", "增加", "上升", "提升")):
            return True
        return None

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    def validation_result(self) -> ValidationResult:
        claims = list(self.claims.values())
        # 跨切面检查每次现算，不回写到摄入期 findings，避免重复调用造成堆叠。
        findings = list(self.findings)
        findings.extend(check_ratio_against_inputs(claims))
        findings.extend(check_text_number_conflicts(claims, self.registry))
        collisions = build_digit_collision_index(self.registry.all_current())
        for raw_value, anchors in collisions.items():
            findings.append(Finding(
                Severity.HAZARD, "digit-collision",
                f"数字 {raw_value} 在不同单位/表中重复出现: {'; '.join(anchors)}，"
                "换算前字面相同但量级可能不同",
                tuple(sorted({self._source_of_anchor(a) for a in anchors})),
            ))
        return ValidationResult(claims=claims, findings=findings, digit_collisions=collisions)

    def _source_of_anchor(self, anchor: str) -> str:
        table_id = anchor.split('/')[0]
        for table in self.tables:
            if table.table_id == table_id:
                return table.source_id
        return table_id

    def explain_claim(self, claim_id: str) -> dict:
        """回查一条主张的实际输入数、口径、证据与复核轨迹。"""
        claim = self.claims[claim_id]
        cells = []
        for table_id, row_label, column_label, version in claim.depends_on_cells:
            cell = self.registry.cells.get((table_id, row_label, column_label), [])
            current = cell[-1] if cell else None
            cells.append({
                "ref": f"{table_id}/{row_label}/{column_label}@v{version}",
                "current_version": current.version if current else None,
                "raw_value": current.raw_value if current else None,
                "unit": current.unit if current else None,
                "amount_yuan": str(current.amount_yuan) if current and current.amount_yuan is not None else None,
                "source_id": current.source_id if current else None,
            })
        return {
            "claim": claim.to_dict(),
            "input_cells": cells,
            "asserted_ratio": str(claim.asserted_ratio) if claim.asserted_ratio is not None else None,
            "computed_ratio": str(claim.computed_ratio) if claim.computed_ratio is not None else None,
            "period": self.periods.get(claim.period_id or "", {}),
        }
