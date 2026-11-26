"""财务复核工作流与分发台账。

复核规则：
* 按具体原子主张确认对外口径，一个答复拆出的两条主张互不连坐；
* 媒体反复引用同一张表不算多项独立依据；
* 已发材料只保留并挂更正条目，草稿在依据失效后不得发送。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    Claim, CorrectionNotice, OutputRecord, OutputState,
    ReviewEvent, ReviewState, Severity, Finding,
)


class ReviewError(ValueError):
    pass


@dataclass
class ReviewLedger:
    events: list[ReviewEvent] = field(default_factory=list)

    def transition(self, claim: Claim, to_state: ReviewState,
                   note: str, source_id: str | None = None) -> ReviewEvent:
        allowed = {
            ReviewState.PENDING: {ReviewState.CONFIRMED, ReviewState.REJECTED, ReviewState.INVALIDATED},
            ReviewState.CONFIRMED: {ReviewState.INVALIDATED, ReviewState.SUPERSEDED, ReviewState.REJECTED},
            ReviewState.REJECTED: {ReviewState.PENDING, ReviewState.INVALIDATED},
            ReviewState.INVALIDATED: {ReviewState.CONFIRMED, ReviewState.REJECTED, ReviewState.SUPERSEDED},
            ReviewState.SUPERSEDED: {ReviewState.CONFIRMED, ReviewState.REJECTED},
        }
        if to_state not in allowed[claim.review]:
            raise ReviewError(
                f"主张 {claim.claim_id} 不能从 {claim.review.value} 转到 {to_state.value}"
            )
        event = ReviewEvent(claim.claim_id, claim.review, to_state, note, source_id)
        self.events.append(event)
        claim.review = to_state
        claim.review_note = note
        return event

    def invalidate(self, claim: Claim, reason: str, source_id: str) -> ReviewEvent:
        return self.transition(claim, ReviewState.INVALIDATED, reason, source_id)


def independent_evidence_count(claim: Claim) -> int:
    """统计独立来源数：媒体转载链只算一次独立观察。"""
    seen: set[str] = set()
    for evidence in claim.evidence:
        if not evidence.independent:
            continue
        for source_id in evidence.source_ids:
            seen.add(source_id)
    return len(seen)


def check_media_reprints(claims: list[Claim], reprint_chains: dict[str, tuple[str, ...]]) -> list[Finding]:
    """``reprint_chains``：转载媒体编号 -> 原始来源编号链。"""
    findings: list[Finding] = []
    for claim in claims:
        source_ids = {s for ev in claim.evidence for s in ev.source_ids}
        for media_id, chain in reprint_chains.items():
            if media_id not in source_ids:
                continue
            independent = independent_evidence_count(claim)
            if independent <= 1:
                findings.append(Finding(
                    Severity.HAZARD, "media-reprint",
                    f"{media_id} 为转载（链：{' -> '.join(chain)}），与原始报道不构成多项独立依据，"
                    f"主张 {claim.claim_id} 独立来源数为 {independent}",
                    tuple(sorted(source_ids)), (claim.claim_id,),
                ))
    return findings


@dataclass
class DistributionLedger:
    outputs: dict[str, OutputRecord] = field(default_factory=dict)
    notices: list[CorrectionNotice] = field(default_factory=list)
    _notice_seq: int = 0

    def register(self, output: OutputRecord) -> None:
        if output.output_id in self.outputs:
            raise ReviewError(f"材料编号重复: {output.output_id}")
        self.outputs[output.output_id] = output

    def can_send(self, output: OutputRecord, claims: dict[str, Claim]) -> tuple[bool, list[str]]:
        """发送前置检查：所有关联主张必须已确认，且不含预测冒充事实。"""
        problems: list[str] = []
        for claim_id in output.claim_ids:
            claim = claims.get(claim_id)
            if claim is None:
                problems.append(f"{claim_id} 未登记")
                continue
            if claim.review != ReviewState.CONFIRMED:
                problems.append(f"{claim_id} 当前状态 {claim.review.value}，不可对外")
            if claim.statement_kind.value == "预测" and "预测不得当事实" in claim.flags:
                problems.append(f"{claim_id} 是预测，不能当作已实现事实发送")
        return (not problems), problems

    def send(self, output: OutputRecord, claims: dict[str, Claim]) -> None:
        ok, problems = self.can_send(output, claims)
        if not ok:
            raise ReviewError(f"材料 {output.output_id} 不满足发送条件: {'; '.join(problems)}")
        output.state = OutputState.SENT
        # 保存发送当时内容快照，追问时查看当时版本。
        output.snapshot_text = output.text

    def attach_correction(self, output_id: str, claim_id: str, reason: str,
                          source_ids: tuple[str, ...]) -> CorrectionNotice:
        output = self.outputs[output_id]
        self._notice_seq += 1
        notice = CorrectionNotice(
            notice_id=f"CN-{self._notice_seq:03d}",
            output_id=output_id,
            recipient=output.recipient,
            claim_id=claim_id,
            reason=reason,
            source_ids=source_ids,
        )
        output.correction_notices.append(notice.to_dict())
        self.notices.append(notice)
        return notice

    def corrections_for_recipient(self, recipient: str) -> list[CorrectionNotice]:
        return [n for n in self.notices if n.recipient == recipient]
