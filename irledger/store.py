"""本地台账持久化：只写本地 JSON，不执行任何对外发送。

所有动作（复核、发送登记、更正通知）都落到工作目录下的台账文件，
合成金额与“对外动作”均为本地记录。
"""
from __future__ import annotations

import json
from pathlib import Path

from .pipeline import Pipeline


class LocalStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.directory / "ledger.json"

    def save(self, pipe: Pipeline) -> None:
        payload = {
            "case_records": [rid for rid in pipe.records],
            "claims": [claim.to_dict() for claim in pipe.claims.values()],
            "outputs": [output.to_dict() for output in pipe.outputs.values()],
            "reprint_chains": {k: list(v) for k, v in pipe.reprint_chains.items()},
            "ingest_log": [
                {
                    "new_record_ids": report.new_record_ids,
                    "invalidated_claims": report.invalidated_claims,
                    "corrections": [spec.source_id for spec in report.correction_specs],
                }
                for report in pipe.ingest_log
            ],
        }
        self.ledger_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_raw(self) -> dict:
        if not self.ledger_path.exists():
            return {}
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))
