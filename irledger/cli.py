"""命令行入口：资料检查、口径报告、主张回查与本地分发登记。

示例：
    python3 -m irledger.cli check data/case.json
    python3 -m irledger.cli report data/case.json
    python3 -m irledger.cli explain data/case.json CL-001
    python3 -m irledger.cli send data/case.json OUT2 --store build/ledger
"""
from __future__ import annotations

import argparse
import json
import sys

from .models import ReviewState
from .pipeline import Pipeline
from .review import DistributionLedger, check_media_reprints
from .store import LocalStore


def _load(path: str) -> Pipeline:
    return Pipeline.from_case_file(path)


def cmd_check(args: argparse.Namespace) -> int:
    pipe = _load(args.case)
    result = pipe.validation_result()
    media_findings = check_media_reprints(result.claims, pipe.reprint_chains)
    findings = result.findings + media_findings
    conflicts = [f for f in findings if f.severity.value == "冲突"]
    hazards = [f for f in findings if f.severity.value == "口径风险"]
    print(f"资料包读取成功：原文 {len(pipe.records)} 条，主张 {len(result.claims)} 条")
    print(f"冲突 {len(conflicts)} 项；口径风险 {len(hazards)} 项；数字撞形 {len(result.digit_collisions)} 组")
    for finding in findings:
        print(f"  [{finding.severity.value}/{finding.code}] {finding.message} 来源={','.join(finding.source_ids)}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    pipe = _load(args.case)
    result = pipe.validation_result()
    if args.json:
        print(json.dumps({
            "claims": [c.to_dict() for c in result.claims],
            "findings": [f.to_dict() for f in result.findings],
            "digit_collisions": result.digit_collisions,
        }, ensure_ascii=False, indent=2))
        return 0
    print("=== 主张复核状态 ===")
    for claim in result.claims:
        ratio = ""
        if claim.computed_ratio is not None:
            from .textnum import format_percent
            ratio = f" 复算={format_percent(claim.computed_ratio)}"
        print(f"{claim.claim_id} [{claim.review.value}] {claim.metric.value}/{claim.basis.value}"
              f"/{claim.statement_kind.value}{ratio}  {claim.text}")
        if claim.review_note:
            print(f"    备注: {claim.review_note}")
    print("\n=== 既有材料 ===")
    for output in pipe.outputs.values():
        status = output.state.value
        print(f"{output.output_id} {status} -> {output.recipient}: {output.text}")
        print(f"    关联主张: {', '.join(output.claim_ids) or '（无匹配主张）'}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    pipe = _load(args.case)
    if args.claim_id not in pipe.claims:
        print(f"主张不存在: {args.claim_id}", file=sys.stderr)
        return 2
    print(json.dumps(pipe.explain_claim(args.claim_id), ensure_ascii=False, indent=2))
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    pipe = _load(args.case)
    output = pipe.outputs.get(args.output_id)
    if output is None:
        print(f"材料不存在: {args.output_id}", file=sys.stderr)
        return 2
    ledger = DistributionLedger()
    ledger.register(output)
    ok, problems = ledger.can_send(output, pipe.claims)
    store = LocalStore(args.store)
    if not ok:
        print(f"发送被拦截（仅本地登记，不执行外发）: {'; '.join(problems)}", file=sys.stderr)
        store.save(pipe)
        return 1
    ledger.send(output, pipe.claims)
    print(f"材料 {args.output_id} 满足复核条件，已登记发送快照（本地记录）: {output.snapshot_text}")
    store.save(pipe)
    return 0


def cmd_corrections(args: argparse.Namespace) -> int:
    pipe = _load(args.case)
    print("=== 已发材料的更正条目（不整批撤回无关内容）===")
    for output in pipe.outputs.values():
        for claim_id in output.claim_ids:
            claim = pipe.claims.get(claim_id)
            if claim and claim.review == ReviewState.INVALIDATED and output.state.value == "sent":
                print(f"{output.output_id} -> {output.recipient}: 主张 {claim_id} 需更正")
                print(f"    原因: {claim.review_note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="irledger", description="指标口径复核与本地分发登记")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="检查资料并列出全部冲突与口径风险")
    p_check.add_argument("case")
    p_check.set_defaults(func=cmd_check)

    p_report = sub.add_parser("report", help="输出主张复核报告")
    p_report.add_argument("case")
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=cmd_report)

    p_explain = sub.add_parser("explain", help="回查一条主张的输入数与口径")
    p_explain.add_argument("case")
    p_explain.add_argument("claim_id")
    p_explain.set_defaults(func=cmd_explain)

    p_send = sub.add_parser("send", help="按复核结果登记发送（仅本地）")
    p_send.add_argument("case")
    p_send.add_argument("output_id")
    p_send.add_argument("--store", default="build/ledger")
    p_send.set_defaults(func=cmd_send)

    p_corr = sub.add_parser("corrections", help="列已发材料需要更正的具体主张")
    p_corr.add_argument("case")
    p_corr.set_defaults(func=cmd_corrections)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
