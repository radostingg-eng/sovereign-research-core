"""Exercise an unavailable platform run ID through the real cycle path."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]

_SMOKE = """
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from runtime.audit_store import AuditJournal
from runtime.run_host_cycle import main as execute
from runtime.schedule_ledger import reliability_gate_summary, run_watchdog
from runtime.staged_intake import process_staging

root = Path(os.environ["SOVEREIGN_PROFILE_DIR"])
code = Path(os.environ["SOVEREIGN_CODE_ROOT"])
staging = root / "host_staging"
inputs = root / "host_input"
audit = root / "audit"
for directory in (staging, inputs, audit, root / "runs"):
    directory.mkdir()
(inputs / ".promotion_policy.json").write_text(
    '{"schema_version":1,"legacy_files":[]}', encoding="utf-8"
)
slot = "2026-09-17T16:00:00Z"
identity = {
    "effective_core_commit": "a" * 40,
    "effective_prompt_sha256": "b" * 64,
    "effective_host_input_schema_version": 1,
}
(root / "runs" / "SCHEDULE.json").write_text(json.dumps({
    "schema_version": 1,
    "enabled": True,
    "task_id": "synthetic-e2e",
    "task_name": "synthetic-e2e",
    "timezone": "UTC",
    "cadence_minutes": 60,
    "anchor_at": slot,
    "reliability_gate_activation_at": slot,
    "grace_minutes": 15,
    "source_max_age_minutes": 30,
    "accounting_window_hours": 48,
    "min_workflow_version": 2,
    **identity,
}), encoding="utf-8")
semantic = json.loads(
    (code / "schemas" / "host_semantic_v1.example.json").read_text()
)
semantic["schedule_context"]["task_id"] = "synthetic-e2e"
semantic["schedule_context"]["platform_run_id"] = None
semantic["schedule_context"]["platform_run_id_status"] = "unavailable"
source = staging / "synthetic.semantic.json"
source.write_text(json.dumps(semantic, indent=2) + "\\n", encoding="utf-8")
source_bytes = source.read_bytes()
promoted, refusals = process_staging(staging, inputs, records=[])
assert promoted == ["synthetic.json"] and not refusals, (promoted, refusals)
archives = [
    path for path in (staging / "accepted_sources").glob(
        "synthetic.semantic-*.json"
    ) if not path.name.endswith(".build.json")
]
assert len(archives) == 1 and archives[0].read_bytes() == source_bytes
journal = AuditJournal(audit / "synthetic.jsonl")
assert execute([
    "--input-dir", str(inputs), "--journal", str(journal.path),
]) == 0
records = journal.read()
receipt_id = "cycle-receipt:cycle-semantic"
finalization_id = "cycle-finalization:cycle-semantic"
assert any(row["record_id"] == receipt_id for row in records)
assert any(
    row["record_id"] == finalization_id
    and receipt_id in row["caused_by"]
    for row in records
)
assert journal.validate()["valid"]
watchdog = run_watchdog(
    root,
    now=datetime(2026, 9, 17, 16, 20, tzinfo=timezone.utc),
    metadata_reader=lambda _root, _path: {
        "commit_sha": "c" * 40,
        "committed_at": "2026-09-17T16:05:00+00:00",
        "committer_email": "host@example.com",
        "subject": "synthetic source",
    },
    configuration_reader=lambda _root: identity,
)
gate = reliability_gate_summary(root, records=records)
print(json.dumps({
    "promoted": promoted,
    "receipt": receipt_id,
    "finalization": finalization_id,
    "schedule_status": watchdog["slots"][0]["status"],
    "gate_a": gate["gate_a"]["complete_count"],
    "gate_b": gate["gate_b"]["complete_count"],
    "healthy": watchdog["healthy"],
}))
"""


def test_semantic_v2_reaches_finalization_without_autonomy_credit(
    tmp_path: Path,
) -> None:
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", os.defpath),
        "SOVEREIGN_PROFILE_DIR": str(tmp_path),
        "SOVEREIGN_CODE_ROOT": str(ROOT),
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", _SMOKE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "promoted": ["synthetic.json"],
        "receipt": "cycle-receipt:cycle-semantic",
        "finalization": "cycle-finalization:cycle-semantic",
        "schedule_status": "scheduled_unverified_run_id",
        "gate_a": 0,
        "gate_b": 1,
        "healthy": True,
    }
