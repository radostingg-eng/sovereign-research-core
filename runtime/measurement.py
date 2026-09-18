"""Paired measurement of a candidate against the baseline it must beat.

evaluate_mutation accepted `primary_delta` and `counter_metric_deltas` as
numbers the caller supplied. Nothing produced them, so the evidence gate was
only ever as honest as whoever filled in the form. This module runs fixed
tasks against BOTH the baseline tree and the candidate tree and reports what
each one actually did.

Two separations are enforced rather than documented:

* Domain. A research tool getting faster or making fewer errors is not
  evidence that returns improved. Those are different claims resting on
  different evidence, and summing them produces a number that means nothing.
  domain_delta refuses to aggregate across domains.

* Protection. Tasks marked protected are the holdout. Tuning against them
  and then citing them as out-of-sample is the oldest way to manufacture a
  result, so they are reported separately and an out-of-sample claim that
  rests on no protected task is refused.

No network. Both trees are exports of the repository, run in temporary
directories that are removed afterwards.

Requires a shell with `git` (and `tar` for the export). The CORE cycle
path -- orchestrator, receipts, journal, production_host -- deliberately
does NOT: it is pure stdlib, so a host with repository access but no
shell can still run a full cognitive cycle and persist a receipt. Only
the self-improvement loop needs this module.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .sandbox import SandboxError, export_head
from .self_improvement import MutationProposal, proposal_digest, validate_mutation

RESEARCH_TOOL = "research_tool"
INVESTMENT_RETURN = "investment_return"
DOMAINS = frozenset({RESEARCH_TOOL, INVESTMENT_RETURN})

HIGHER_IS_BETTER = "higher_is_better"
LOWER_IS_BETTER = "lower_is_better"
DIRECTIONS = frozenset({HIGHER_IS_BETTER, LOWER_IS_BETTER})


@dataclass(frozen=True)
class Task:
    """One fixed, repeatable measurement.

    The command must print a single number as its last line of stdout. A
    task whose output cannot be read as a number has not measured anything,
    and is reported as unusable rather than scored as zero.
    """

    task_id: str
    domain: str
    direction: str
    protected: bool
    command: tuple[str, ...]

    @staticmethod
    def from_mapping(data: Mapping[str, Any]) -> "Task":
        domain = str(data.get("domain", ""))
        direction = str(data.get("direction", ""))
        if domain not in DOMAINS:
            raise ValueError(f"invalid_task_domain:{data.get('task_id')}:{domain}")
        if direction not in DIRECTIONS:
            raise ValueError(f"invalid_task_direction:{data.get('task_id')}:{direction}")
        command = tuple(str(x) for x in data.get("command", ()))
        if not command:
            raise ValueError(f"task_missing_command:{data.get('task_id')}")
        task_id = str(data.get("task_id", "")).strip()
        if not task_id:
            raise ValueError("task_missing_id")
        return Task(task_id, domain, direction, bool(data.get("protected", False)), command)


@dataclass(frozen=True)
class Measurement:
    task_id: str
    domain: str
    direction: str
    protected: bool
    baseline_value: float | None
    candidate_value: float | None
    baseline_exit: int
    candidate_exit: int
    usable: bool
    reason: str

    @property
    def improvement(self) -> float | None:
        """Signed improvement, positive meaning better, or None if unusable."""
        if not self.usable:
            return None
        delta = float(self.candidate_value) - float(self.baseline_value)
        return delta if self.direction == HIGHER_IS_BETTER else -delta


@dataclass(frozen=True)
class MeasurementSuite:
    proposal_digest: str
    measurements: tuple[Measurement, ...]
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_digest": self.proposal_digest,
            "at": self.at,
            "measurements": [
                {
                    "task_id": m.task_id, "domain": m.domain, "direction": m.direction,
                    "protected": m.protected, "baseline_value": m.baseline_value,
                    "candidate_value": m.candidate_value, "usable": m.usable,
                    "reason": m.reason, "improvement": m.improvement,
                }
                for m in self.measurements
            ],
        }


DEFAULT_TASKS_PATH = Path(__file__).resolve().parent.parent / "evaluation_tasks.json"


def default_tasks() -> list[Task]:
    """The committed task suite.

    A caller that invents its own tasks per run can pick ones that flatter
    the candidate, so the suite is committed and versioned rather than
    assembled at call time.
    """
    return load_tasks(DEFAULT_TASKS_PATH)


def load_tasks(path: Path | str) -> list[Task]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("tasks", data) if isinstance(data, Mapping) else data
    rows = [r for r in rows if isinstance(r, Mapping)]
    tasks = [Task.from_mapping(row) for row in rows]
    seen: set[str] = set()
    for task in tasks:
        if task.task_id in seen:
            raise ValueError(f"duplicate_task_id:{task.task_id}")
        seen.add(task.task_id)
    return tasks


def _read_number(text: str) -> float | None:
    for line in reversed((text or "").strip().splitlines()):
        try:
            value = float(line.strip())
        except ValueError:
            continue
        return value if math.isfinite(value) else None
    return None


def _run_task(task: Task, tree: Path, timeout_s: int) -> tuple[float | None, int]:
    try:
        result = subprocess.run(list(task.command), cwd=str(tree), capture_output=True,
                                text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, 124
    if result.returncode != 0:
        return None, result.returncode
    return _read_number(result.stdout), result.returncode


def measure_candidate(
    proposal: MutationProposal,
    tasks: Sequence[Task] | None = None,
    *,
    repo_root: Path | str,
    allowed_prefixes: Sequence[str] = ("runtime/",),
    timeout_s: int = 300,
) -> MeasurementSuite:
    """Run every task against the baseline tree and the candidate tree.

    Paired by construction: the same task, the same command, two trees that
    differ only by the proposal's patch. Comparing a candidate's number to a
    baseline number produced by a different task, or a different tree, is how
    an improvement gets claimed without one existing.
    """
    errors = validate_mutation(proposal, allowed_prefixes=allowed_prefixes)
    if errors:
        raise SandboxError("invalid_proposal:" + ",".join(errors))
    if tasks is None:
        tasks = default_tasks()
    if not tasks:
        raise SandboxError("no_tasks: a measurement suite with no tasks measures nothing")

    repo_root = Path(repo_root).resolve()
    workspace = Path(tempfile.mkdtemp(prefix="sovereign-measure-"))
    try:
        baseline_tree = workspace / "baseline"
        candidate_tree = workspace / "candidate"
        export_head(repo_root, baseline_tree)
        export_head(repo_root, candidate_tree)

        patch_file = workspace / "candidate.patch"
        patch_file.write_text(proposal.patch, encoding="utf-8")
        applied = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file)],
            cwd=str(candidate_tree), capture_output=True, text=True, timeout=120,
        )
        if applied.returncode != 0:
            raise SandboxError(f"patch_did_not_apply:{applied.stderr.strip()[:200]}")

        measurements: list[Measurement] = []
        for task in tasks:
            base_value, base_exit = _run_task(task, baseline_tree, timeout_s)
            cand_value, cand_exit = _run_task(task, candidate_tree, timeout_s)
            if base_value is None or cand_value is None:
                reason = "task_produced_no_number"
                if base_exit != 0 or cand_exit != 0:
                    reason = f"task_failed:baseline_exit={base_exit}:candidate_exit={cand_exit}"
                measurements.append(Measurement(
                    task.task_id, task.domain, task.direction, task.protected,
                    base_value, cand_value, base_exit, cand_exit, False, reason))
                continue
            measurements.append(Measurement(
                task.task_id, task.domain, task.direction, task.protected,
                base_value, cand_value, base_exit, cand_exit, True, "measured"))
        return MeasurementSuite(
            proposal_digest=proposal_digest(proposal),
            measurements=tuple(measurements),
            at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def domain_delta(suite: MeasurementSuite, domain: str, *,
                 protected_only: bool = False) -> dict[str, Any]:
    """Mean improvement within ONE domain.

    Refuses an unknown domain rather than returning an empty result, because
    a typo'd domain silently scoring zero is indistinguishable from a real
    absence of effect.
    """
    if domain not in DOMAINS:
        raise ValueError(f"unknown_domain:{domain}")
    rows = [m for m in suite.measurements
            if m.domain == domain and (m.protected or not protected_only)]
    usable = [m for m in rows if m.usable]
    unusable = [m.task_id for m in rows if not m.usable]
    if not usable:
        return {"domain": domain, "protected_only": protected_only, "n": 0,
                "mean_improvement": None, "unusable": unusable,
                "status": "no_usable_measurements"}
    improvements = [float(m.improvement) for m in usable]
    return {
        "domain": domain, "protected_only": protected_only, "n": len(usable),
        "mean_improvement": sum(improvements) / len(improvements),
        "improved": sum(1 for x in improvements if x > 0),
        "regressed": sum(1 for x in improvements if x < 0),
        "unusable": unusable,
        "status": "measured",
    }


def measurement_tampering_suspected(suite: MeasurementSuite) -> dict[str, Any]:
    """Development metrics improved while every holdout stayed still.

    A self-improving system measures itself with its own detectors, so a
    candidate can improve a metric by WEAKENING the code that reports it
    rather than by fixing anything. Deleting one integrity check scored +3.0
    on open findings while the sixteen stranded records it stopped reporting
    were still stranded.

    The holdout is what catches this. Reported findings fell; ground truth
    did not move. An improvement that no protected task can see is either
    measurement tampering or an improvement that does not matter, and both
    are reasons to withhold the verdict rather than grant it.

    This cannot prove tampering. It refuses to treat an unseen improvement
    as evidence, which is the honest half of the problem.
    """
    protected = [m for m in suite.measurements if m.protected and m.usable]
    development = [m for m in suite.measurements if not m.protected and m.usable]
    if not protected:
        return {"suspected": False, "reason": "no_protected_measurements_to_compare"}
    if not any(float(m.improvement) > 0 for m in development):
        return {"suspected": False, "reason": "no_development_improvement_claimed"}
    if any(float(m.improvement) != 0 for m in protected):
        return {"suspected": False, "reason": "holdout_moved_too"}
    return {
        "suspected": True,
        "reason": "development_improved_while_every_holdout_stood_still",
        "development": [m.task_id for m in development if float(m.improvement) > 0],
        "protected": [m.task_id for m in protected],
    }


def investment_claim_support(suite: MeasurementSuite) -> dict[str, Any]:
    """Whether this suite can support a claim about investment returns.

    A research tool getting faster is not evidence that returns improved.
    The two were interchangeable as long as every measurement fed one
    undifferentiated `primary_delta`, which let a tooling win be spent as an
    economic one. An investment claim needs investment-domain tasks, and an
    out-of-sample claim needs protected ones.
    """
    investment = [m for m in suite.measurements if m.domain == INVESTMENT_RETURN]
    protected = [m for m in investment if m.protected and m.usable]
    if not investment:
        return {"supported": False, "reason": "no_investment_return_tasks",
                "detail": "this suite measures tooling only"}
    if not protected:
        return {"supported": False, "reason": "no_protected_investment_measurements",
                "detail": "every investment task is a development case"}
    result = domain_delta(suite, INVESTMENT_RETURN, protected_only=True)
    return {"supported": True, "reason": "protected_investment_measurements_present",
            "delta": result}


def verify_measurement_suite(suite: Any, proposal: MutationProposal) -> list[str]:
    """Why a suite does not evidence THIS proposal."""
    if suite is None:
        return ["measurement_suite_missing"]
    errors: list[str] = []
    expected = proposal_digest(proposal)
    actual = str(getattr(suite, "proposal_digest", "") or "")
    if actual != expected:
        errors.append(
            f"measurement_suite_for_other_proposal:expected={expected[:12]}:"
            f"got={(actual or 'absent')[:12]}"
        )
    measurements = tuple(getattr(suite, "measurements", ()) or ())
    if not measurements:
        errors.append("measurement_suite_empty")
    elif not any(m.usable for m in measurements):
        errors.append("measurement_suite_has_no_usable_measurements")
    return errors
