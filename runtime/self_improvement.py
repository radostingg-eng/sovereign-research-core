"""Evidence-gated self-modification primitives.

The LLM host authors hypotheses and candidate patches. This deterministic module
validates the mutation envelope, preserves immutable boundaries, records causal
identity, evaluates sandbox evidence, and decides whether a candidate is eligible
for promotion. It never writes broker orders and never rewrites historical audit
records or ex-ante evidence.
"""
from __future__ import annotations

from copy import deepcopy

import hashlib
import math
import re

import posixpath

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .engine import canonical_json, sha256_text
from .prompt_invariants import STANDING_PROMPT_TARGET

if TYPE_CHECKING:
    from .audit_store import AuditJournal


IMMUTABLE_PATH_PREFIXES = (
    "SYSTEM.md",
    "LLM_HOST_CONTRACT.md",
    "META_RESEARCH_CONTRACT.md",
    "EFFECTIVENESS.md",
    "SELF_IMPROVEMENT_CONTRACT.md",
    "runtime/engine.py",
    "runtime/audit_store.py",
    "runtime/self_improvement.py",
)
FORBIDDEN_MUTATION_TOKENS = (
    "order_submission",
    "place_order",
    "submit_order",
    "live_order",
    "broker_execution",
    "delete_audit",
    "rewrite_history",
    "rewrite_ex_ante",
)
MUTATION_PROPOSAL_FIELDS = (
    "mutation_id",
    "parent_version",
    "mutation_type",
    "targets",
    "rationale",
    "failure_ids",
    "patch",
    "expected_effect",
    "counter_metrics",
    "sample_requirement",
    "evaluation_window",
    "rollback_condition",
    "created_at",
)
MUTATION_PROPOSAL_STATUSES = frozenset({
    "testing",
    "proposed_not_evaluated",
})
_MUTATION_TEXT_FIELDS = (
    "mutation_id",
    "parent_version",
    "mutation_type",
    "rationale",
    "patch",
    "expected_effect",
    "evaluation_window",
    "rollback_condition",
    "created_at",
)
_MUTATION_SEQUENCE_FIELDS = (
    "targets",
    "failure_ids",
    "counter_metrics",
)


@dataclass(frozen=True)
class FailureRecord:
    failure_id: str
    run_id: str
    stage: str
    failure_class: str
    symptom: str
    evidence: tuple[str, ...]
    severity: str
    detected_at: str
    caused_by: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "failure_id": self.failure_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "failure_class": self.failure_class,
            "symptom": self.symptom,
            "evidence": list(self.evidence),
            "severity": self.severity,
            "detected_at": self.detected_at,
            "caused_by": list(self.caused_by),
        }


@dataclass(frozen=True)
class MutationProposal:
    mutation_id: str
    parent_version: str
    mutation_type: str
    targets: tuple[str, ...]
    rationale: str
    failure_ids: tuple[str, ...]
    patch: str
    expected_effect: str
    counter_metrics: tuple[str, ...]
    sample_requirement: int
    evaluation_window: str
    rollback_condition: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "parent_version": self.parent_version,
            "mutation_type": self.mutation_type,
            "targets": list(self.targets),
            "rationale": self.rationale,
            "failure_ids": list(self.failure_ids),
            "patch": self.patch,
            "expected_effect": self.expected_effect,
            "counter_metrics": list(self.counter_metrics),
            "sample_requirement": self.sample_requirement,
            "evaluation_window": self.evaluation_window,
            "rollback_condition": self.rollback_condition,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class MutationEvaluation:
    mutation_id: str
    status: str
    sample_size: int
    primary_delta: float | None
    counter_metric_deltas: Mapping[str, float]
    counter_metric_directions: Mapping[str, str]
    out_of_sample: bool
    sandbox_passed: bool
    rollback_triggered: bool
    reason: str
    # Empty on a hand-built evaluation. promote_mutation requires it to match
    # the proposal being promoted, so an evaluation that did not come from
    # evaluate_mutation cannot authorize a promotion.
    proposal_digest: str = ""


def proposal_digest(proposal: MutationProposal) -> str:
    """Content identity of what a proposal would actually apply.

    mutation_id is a NAME, and approval was bound to the name alone. A
    proposal could be evaluated with one patch, keep its id, have its patch
    replaced, and then be promoted on the earlier evaluation -- the evidence
    attested to code that was no longer the code being shipped.

    Only fields that change what lands or what the evidence had to clear are
    included. Rationale, created_at and expected_effect are prose about the
    change rather than the change itself, so editing them does not invalidate
    a completed evaluation.
    """
    body = canonical_json({
        "parent_version": proposal.parent_version,
        "mutation_type": proposal.mutation_type,
        "targets": sorted(proposal.targets),
        "patch": proposal.patch,
        "counter_metrics": sorted(proposal.counter_metrics),
        "sample_requirement": proposal.sample_requirement,
        "rollback_condition": proposal.rollback_condition,
    })
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def failure_fingerprint(*, stage: str, failure_class: str, symptom: str,
                        evidence: Sequence[str] = ()) -> str:
    """Stable identity for a failure, independent of any one occurrence.

    The evidence record ids were hashed into the fingerprint. Evidence is
    occurrence-specific by definition, so the SAME recurring failure produced
    a different fingerprint every time it recurred -- which defeats the one
    thing a fingerprint exists to do. Grouping recurrences is also how a fix
    gets proven: if every occurrence is a new identity, nothing can be shown
    to have stopped happening.

    `evidence` is still accepted so existing call sites keep working, and it
    belongs on the occurrence record, not in the identity.
    """
    body = canonical_json(
        {"stage": stage, "failure_class": failure_class, "symptom": symptom}
    )
    return "failure-" + sha256_text(body)[:20]


def _scope_errors(normalized: str, allowed_prefixes: Sequence[str]) -> list[str]:
    """Immutable-target and allowlist checks against an already-normalized path."""
    errors: list[str] = []
    if any(normalized == p or normalized.startswith(p + "/") for p in IMMUTABLE_PATH_PREFIXES):
        errors.append(f"immutable_target:{normalized}")
    if allowed_prefixes:
        allowed = [str(p).strip().rstrip("/") for p in allowed_prefixes]
        if not any(normalized == p or normalized.startswith(p + "/") for p in allowed):
            errors.append(f"target_outside_allowlist:{normalized}")
    return errors


def _normalize_target(target: str) -> tuple[str | None, str | None]:
    """Resolve a declared target to a repo-relative path, or explain why not.

    Returns (normalized, error). Exactly one is not None.

    Scope checks used to be raw string comparisons, so "runtime/../SYSTEM.md"
    cleared the allowlist (it starts with "runtime/") AND cleared the
    immutable check (it does not equal "SYSTEM.md"). It resolves to the
    constitution. Same for "runtime/a/../../LLM_HOST_CONTRACT.md". Any
    guard that compares unnormalized paths is decorative.
    """
    raw = str(target).strip()
    if not raw:
        return None, "empty_target"
    if "\\" in raw:
        # Windows separators would survive posixpath.normpath untouched and
        # could re-enter as a separator on another platform.
        return None, f"backslash_in_target:{raw}"
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return None, f"absolute_target:{raw}"
    normalized = posixpath.normpath(raw)
    if normalized == ".." or normalized.startswith("../"):
        return None, f"target_escapes_repo:{raw}"
    if normalized.startswith("/"):
        return None, f"absolute_target:{raw}"
    return normalized, None


# Unified-diff file headers. Used to cross-check what a patch actually
# touches against what it declared. Patches here are not guaranteed to be
# unified diffs, so this extracts what it can and the caller decides what
# an unverifiable patch means.
_DIFF_FILE_RE = re.compile(r"^(?:---|\+\+\+)[ \t]+(?:[ab]/)?(\S+)", re.MULTILINE)
# A pure git rename carries NO ---/+++ hunk headers at all:
#
#     diff --git a/SYSTEM.md b/runtime/ok.py
#     similarity index 100%
#     rename from SYSTEM.md
#     rename to runtime/ok.py
#
# so the ---/+++ scan returned an empty set, no path was cross-checked
# against the declared targets, and validate_mutation returned zero errors
# for a patch that moves a protected file out from under the immutable
# check. Moving a file is a write to both paths and must be read as one.
_DIFF_GIT_RE = re.compile(r"^diff --git[ \t]+[ab]/(\S+)[ \t]+[ab]/(\S+)", re.MULTILINE)
_DIFF_MOVE_RE = re.compile(r"^(?:rename|copy)[ \t]+(?:from|to)[ \t]+(\S+)", re.MULTILINE)


def patch_touched_paths(patch: str) -> set[str]:
    """Repo-relative paths a unified diff claims to modify.

    Empty when the patch carries no recognizable diff headers, which means
    it cannot be cross-checked here and the applier must enforce scope at
    apply time.
    """
    touched: set[str] = set()
    raws: list[str] = list(_DIFF_FILE_RE.findall(patch or ""))
    for pair in _DIFF_GIT_RE.findall(patch or ""):
        raws.extend(pair)
    raws.extend(_DIFF_MOVE_RE.findall(patch or ""))
    for raw in raws:
        if raw in ("/dev/null", "dev/null"):
            continue
        normalized, error = _normalize_target(raw)
        touched.add(normalized if normalized else raw)
    return touched


def mutation_allowed_prefixes(proposal: MutationProposal) -> tuple[str, ...]:
    """Keep prompt candidates separate from executable runtime patches."""
    declared = {
        normalized
        for target in proposal.targets
        if (normalized := _normalize_target(target)[0]) is not None
    }
    if STANDING_PROMPT_TARGET in (
        declared | patch_touched_paths(proposal.patch)
    ):
        return (STANDING_PROMPT_TARGET,)
    return ("runtime/",)


def validate_mutation(proposal: MutationProposal, *, allowed_prefixes: Sequence[str]) -> list[str]:
    """Validate that a candidate is an evidence proposal, not an escape hatch."""
    errors: list[str] = []
    if not proposal.mutation_id or not proposal.parent_version:
        errors.append("missing_mutation_identity")
    if proposal.sample_requirement <= 0:
        errors.append("sample_requirement_must_be_positive")
    if not proposal.targets:
        errors.append("mutation_targets_empty")

    normalized_targets: set[str] = set()
    for target in proposal.targets:
        normalized, error = _normalize_target(target)
        if error:
            errors.append(error)
            continue
        normalized_targets.add(normalized)
        errors.extend(_scope_errors(normalized, allowed_prefixes))

    # A patch must not touch what it did not declare. Declaring
    # runtime/ok.py while the diff edits SYSTEM.md used to pass, because
    # the patch body was only ever scanned for forbidden tokens.
    for touched in sorted(patch_touched_paths(proposal.patch)):
        if touched not in normalized_targets:
            errors.append(f"patch_touches_undeclared_target:{touched}")
        errors.extend(_scope_errors(touched, allowed_prefixes))

    blob = (proposal.patch + "\n" + "\n".join(proposal.targets)).lower()
    for token in FORBIDDEN_MUTATION_TOKENS:
        if token in blob:
            errors.append(f"forbidden_mutation_token:{token}")
    if not proposal.failure_ids:
        errors.append("mutation_must_be_caused_by_failures")
    if not proposal.expected_effect:
        errors.append("expected_effect_missing")
    if not proposal.counter_metrics:
        errors.append("counter_metrics_missing")
    if not proposal.rollback_condition:
        errors.append("rollback_condition_missing")
    return sorted(set(errors))


def mutation_proposal_from_mapping(
    value: Mapping[str, Any],
) -> MutationProposal:
    """Build the canonical proposal object after envelope validation."""
    return MutationProposal(
        mutation_id=value["mutation_id"],
        parent_version=value["parent_version"],
        mutation_type=value["mutation_type"],
        targets=tuple(value["targets"]),
        rationale=value["rationale"],
        failure_ids=tuple(value["failure_ids"]),
        patch=value["patch"],
        expected_effect=value["expected_effect"],
        counter_metrics=tuple(value["counter_metrics"]),
        sample_requirement=value["sample_requirement"],
        evaluation_window=value["evaluation_window"],
        rollback_condition=value["rollback_condition"],
        created_at=value["created_at"],
    )


def validate_mutation_proposal_envelope(
    value: Any,
    *,
    allowed_prefixes: Sequence[str] | None = None,
) -> list[str]:
    """Validate host mutation input before receipt creation or persistence."""
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return ["mutation_not_an_object"]

    errors: list[str] = []
    for field in MUTATION_PROPOSAL_FIELDS:
        if field not in value or value[field] is None:
            errors.append(f"mutation_missing_field:{field}")
    if errors:
        return errors

    for field in _MUTATION_TEXT_FIELDS:
        item = value[field]
        if not isinstance(item, str) or not item.strip():
            errors.append(f"mutation_invalid_field:{field}")
    for field in _MUTATION_SEQUENCE_FIELDS:
        item = value[field]
        if (
            not isinstance(item, (list, tuple))
            or not item
            or any(not isinstance(entry, str) or not entry.strip()
                   for entry in item)
        ):
            errors.append(f"mutation_invalid_field:{field}")
    sample_requirement = value["sample_requirement"]
    if (
        isinstance(sample_requirement, bool)
        or not isinstance(sample_requirement, int)
    ):
        errors.append("mutation_invalid_field:sample_requirement")
    if errors:
        return sorted(set(errors))

    proposal = mutation_proposal_from_mapping(value)
    return [
        f"mutation_invalid:{error}"
        for error in validate_mutation(
            proposal,
            allowed_prefixes=(
                allowed_prefixes
                if allowed_prefixes is not None
                else mutation_allowed_prefixes(proposal)
            ),
        )
    ]


def mutation_proposal_record_id(mutation_id: str) -> str:
    return f"mutation-proposal:{mutation_id}"


def mutation_proposal_reference(
    proposal: MutationProposal,
    *,
    status: str,
) -> dict[str, str]:
    """Return the receipt-safe identity of a durable proposal record."""
    if status not in MUTATION_PROPOSAL_STATUSES:
        raise ValueError(f"invalid_mutation_proposal_status:{status}")
    return {
        "record_id": mutation_proposal_record_id(proposal.mutation_id),
        "proposal_digest": proposal_digest(proposal),
        "status": status,
    }


def _failure_record_causes(
    records: Sequence[Mapping[str, Any]],
    failure_ids: Sequence[str],
) -> list[str]:
    """Resolve cited failure IDs only when their durable records exist."""
    resolved: list[str] = []
    for failure_id in failure_ids:
        matches = []
        for record in records:
            record_id = str(record.get("record_id", ""))
            payload = record.get("payload")
            payload_failure_id = (
                str(payload.get("failure_id", ""))
                if isinstance(payload, Mapping)
                else ""
            )
            if (
                record_id == failure_id
                or record_id == f"failure:{failure_id}"
                or payload_failure_id == failure_id
            ):
                matches.append(record_id)
        for record_id in sorted(set(matches)):
            if record_id and record_id not in resolved:
                resolved.append(record_id)
    return resolved


def persist_mutation_proposal(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> bool:
    """Append or verify the mutation proposal named by a cycle receipt."""
    self_improvement = receipt.get("self_improvement")
    if not isinstance(self_improvement, Mapping):
        return False
    reference = self_improvement.get("proposal")
    if not isinstance(reference, Mapping):
        return False

    mutation = data.get("mutation")
    errors = validate_mutation_proposal_envelope(mutation)
    if errors:
        raise ValueError(
            "invalid_mutation_proposal:" + ",".join(errors)
        )
    if not isinstance(mutation, Mapping):
        raise ValueError("mutation_proposal_missing_from_input")
    proposal = mutation_proposal_from_mapping(mutation)
    status = str(reference.get("status", ""))
    expected_reference = mutation_proposal_reference(
        proposal,
        status=status,
    )
    if dict(reference) != expected_reference:
        raise ValueError(
            f"mutation_proposal_receipt_mismatch:{proposal.mutation_id}"
        )

    cycle_id = str(receipt.get("cycle_id", "")).strip()
    receipt_id = f"cycle-receipt:{cycle_id}"
    records = journal.read()
    receipt_record = next(
        (
            record for record in records
            if record.get("record_id") == receipt_id
        ),
        None,
    )
    if (
        not cycle_id
        or not isinstance(receipt_record, Mapping)
        or receipt_record.get("payload") != dict(receipt)
    ):
        raise ValueError(
            f"mutation_proposal_receipt_not_persisted:{receipt_id}"
        )

    payload = {
        **proposal.as_dict(),
        "proposal_digest": proposal_digest(proposal),
        "status": status,
        "cycle_id": cycle_id,
    }
    causes = [
        receipt_id,
        *_failure_record_causes(records, proposal.failure_ids),
    ]
    _, created = journal.append_idempotent(
        record_id=expected_reference["record_id"],
        record_type="mutation_proposal",
        agent="sovereign-host",
        payload=payload,
        caused_by=causes,
    )
    return created


def mutation_proposal_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = 5,
) -> dict[str, Any]:
    """Project durable proposals without implying evaluation or adoption."""
    proposals = [
        record
        for record in records
        if record.get("record_type") == "mutation_proposal"
        and isinstance(record.get("payload"), Mapping)
    ]
    visible = proposals[-limit:]
    items = [
        {
            "record_id": record.get("record_id"),
            "record_hash": record.get("record_hash"),
            "caused_by": list(record.get("caused_by") or ()),
            **dict(record["payload"]),
        }
        for record in visible
    ]
    counts = {
        status: sum(
            record["payload"].get("status") == status
            for record in proposals
        )
        for status in sorted(MUTATION_PROPOSAL_STATUSES)
    }
    return {
        "count": len(proposals),
        "counts_by_status": counts,
        "items": items,
        "not_shown": max(0, len(proposals) - len(items)),
        "what_this_means": (
            "These are durable mutation proposals, not promotion records. "
            "proposed_not_evaluated means candidate execution was not enabled. "
            "testing means the proposal remains in the candidate lifecycle. "
            "Neither status claims sandbox success, a passing evaluation, "
            "promotion, or adoption."
        ),
    }


def _is_finite_number(value: Any) -> bool:
    """A measurement that is NaN, infinite or nonnumeric is not a measurement."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def evaluate_mutation(
    proposal: MutationProposal,
    *,
    sandbox_report: Any = None,
    measurement_suite: Any = None,
    claim_domain: str = "research_tool",
    **kwargs: Any,
) -> MutationEvaluation:
    """Evaluate a mutation and bind the verdict to the proposal's content.

    `sandbox_passed` was a boolean the caller supplied, so the cheapest way
    to clear the sandbox gate was to pass True. An `eligible` verdict now
    requires a sandbox REPORT produced by runtime.sandbox.run_candidate:
    a real checkout, a real patch application and a real command, whose
    `passed` field is derived from the exit code rather than asserted.

    Without evidence the verdict is `testing`, not `rejected`. An untested
    candidate has not failed; it has not been tested, and those are
    different findings.
    """
    from .measurement import (INVESTMENT_RETURN, domain_delta,
                              investment_claim_support, verify_measurement_suite)
    from .sandbox import verify_sandbox_report

    # A supplied measurement_suite REPLACES the caller's primary_delta with
    # one derived from paired runs. The delta used to be a number on the
    # form; now it is the mean improvement over tasks that actually ran
    # against both trees.
    if measurement_suite is not None:
        problems = verify_measurement_suite(measurement_suite, proposal)
        if problems:
            return MutationEvaluation(
                proposal.mutation_id, "testing", int(kwargs.get("sample_size", 0) or 0),
                None, dict(kwargs.get("counter_metric_deltas", {}) or {}),
                dict(kwargs.get("counter_metric_directions", {}) or {}),
                bool(kwargs.get("out_of_sample", False)), bool(kwargs.get("sandbox_passed", False)),
                bool(kwargs.get("rollback_triggered", False)),
                "measurement_evidence_invalid:" + ",".join(problems),
                proposal_digest(proposal),
            )
        # An improvement in one domain cannot be spent in another. A research
        # tool getting faster is not evidence that returns improved.
        if claim_domain == INVESTMENT_RETURN:
            support = investment_claim_support(measurement_suite)
            if not support["supported"]:
                return MutationEvaluation(
                    proposal.mutation_id, "testing", int(kwargs.get("sample_size", 0) or 0),
                    None, dict(kwargs.get("counter_metric_deltas", {}) or {}),
                    dict(kwargs.get("counter_metric_directions", {}) or {}),
                    bool(kwargs.get("out_of_sample", False)),
                    bool(kwargs.get("sandbox_passed", False)),
                    bool(kwargs.get("rollback_triggered", False)),
                    f"investment_claim_unsupported:{support['reason']}",
                    proposal_digest(proposal),
                )
        from .measurement import measurement_tampering_suspected

        tampering = measurement_tampering_suspected(measurement_suite)
        if tampering["suspected"]:
            return MutationEvaluation(
                proposal.mutation_id, "testing", int(kwargs.get("sample_size", 0) or 0),
                None, dict(kwargs.get("counter_metric_deltas", {}) or {}),
                dict(kwargs.get("counter_metric_directions", {}) or {}),
                bool(kwargs.get("out_of_sample", False)),
                bool(kwargs.get("sandbox_passed", False)),
                bool(kwargs.get("rollback_triggered", False)),
                f"improvement_invisible_to_holdout:{','.join(tampering['development'])}",
                proposal_digest(proposal),
            )
        measured = domain_delta(measurement_suite, claim_domain)
        kwargs["primary_delta"] = measured["mean_improvement"]
        # out_of_sample was caller-supplied too, so a development-only run
        # with no holdout could declare itself out-of-sample. Whether a
        # protected task participated is a fact about the evidence, so it is
        # read off the suite rather than asserted alongside it.
        #
        # sample_size is deliberately NOT derived here. The suite holds one
        # measurement per evaluation task (4 today); sample_size is the count
        # of observations the claim rests on (sample_requirement is typically
        # 30). Substituting one for the other would make every proposal
        # under-sampled forever and brick promotion, which is a different bug
        # from the one being fixed. It stays caller-supplied and unverified.
        usable = [m for m in measurement_suite.measurements if m.usable]
        kwargs["out_of_sample"] = any(m.protected for m in usable)
        # A task that CRASHED on the candidate while the baseline ran it fine
        # is not missing data, it is the candidate breaking something. Only
        # usable rows were being averaged, so the broken one was silently
        # dropped and a candidate that destroyed a protected task could still
        # reach eligible on the tasks it did not break.
        # First attempt only caught a nonzero exit, so a task that exited 0
        # and emitted NaN or nonnumeric text was still silently dropped. The
        # exit code is not the signal: the signal is that the BASELINE
        # produced a usable number and the candidate did not, however it
        # failed to. A task unusable on both sides is broken in itself and is
        # still not blamed on the candidate.
        broke = [m for m in measurement_suite.measurements
                 if not m.usable
                 and m.baseline_exit == 0
                 and _is_finite_number(m.baseline_value)
                 and not (m.candidate_exit == 0
                          and _is_finite_number(m.candidate_value))]
        if broke:
            return dataclasses.replace(
                _evaluate_mutation(proposal, **kwargs),
                proposal_digest=proposal_digest(proposal),
                status="rejected",
                reason="candidate_broke_evaluation_task:" + ",".join(
                    sorted(m.task_id for m in broke)))

    verdict = _evaluate_mutation(proposal, **kwargs)
    verdict = dataclasses.replace(verdict, proposal_digest=proposal_digest(proposal))
    if verdict.status != "eligible":
        return verdict
    problems = verify_sandbox_report(sandbox_report, proposal)
    if problems:
        return dataclasses.replace(
            verdict, status="testing", reason="sandbox_evidence_required:" + ",".join(problems)
        )
    if not sandbox_report.passed:
        return dataclasses.replace(
            verdict, status="rejected", sandbox_passed=False,
            reason=f"sandbox_failed:exit_code={sandbox_report.exit_code}",
        )
    # Without a suite the primary_delta is whatever the caller typed, which is
    # the sandbox_passed problem one level up: a candidate that merely compiles
    # could reach eligible on numbers nobody produced. Checked after the
    # sandbox so "it never ran" is still reported ahead of "it was not
    # measured", which is the more actionable of the two.
    if measurement_suite is None:
        return dataclasses.replace(
            verdict, status="testing",
            reason="measurement_evidence_required:no_measurement_suite")
    return verdict


def _evaluate_mutation(
    proposal: MutationProposal,
    *,
    sample_size: int,
    primary_delta: float | None,
    counter_metric_deltas: Mapping[str, float],
    counter_metric_directions: Mapping[str, str],
    out_of_sample: bool,
    sandbox_passed: bool,
    rollback_triggered: bool,
    min_samples: int = 30,
    min_primary_delta: float = 0.0,
) -> MutationEvaluation:
    """Return a conservative promotion decision; interpretation remains with the LLM."""
    # NaN comparisons are all False under IEEE 754, so `delta <= min` and
    # `delta < 0` below both fall through and a mutation with no measurable
    # effect reports `evidence_gate_passed`. The same defect was fixed in
    # experiments.evaluate_variant; this is its twin on the mutation path,
    # which is the one a self-modifying loop actually promotes through.
    def _finite(label: str, value: float | None) -> MutationEvaluation | None:
        if value is None or math.isfinite(float(value)): return None
        return MutationEvaluation(proposal.mutation_id, "rejected", sample_size, primary_delta,
                                  dict(counter_metric_deltas), dict(counter_metric_directions),
                                  out_of_sample, sandbox_passed, rollback_triggered,
                                  f"non_finite_measurement:{label}")
    bad = _finite("primary_delta", primary_delta)
    if bad is not None: return bad
    for _m, _d in counter_metric_deltas.items():
        bad = _finite(f"counter_metric:{_m}", _d)
        if bad is not None: return bad
    # A proposal names the counter-metrics it will be judged against. Supplying
    # none of them skipped the regression loop entirely, so the cheapest way to
    # pass the counter-metric gate was to measure nothing.
    undeclared = [m for m in proposal.counter_metrics if m not in counter_metric_deltas]
    if undeclared:
        return MutationEvaluation(proposal.mutation_id, "testing", sample_size, primary_delta,
                                  dict(counter_metric_deltas), dict(counter_metric_directions),
                                  out_of_sample, sandbox_passed, rollback_triggered,
                                  "counter_metric_not_measured:" + ",".join(sorted(undeclared)))
    if any(metric not in counter_metric_directions for metric in counter_metric_deltas):
        return MutationEvaluation(proposal.mutation_id, "rejected", sample_size, primary_delta,
                                  dict(counter_metric_deltas), dict(counter_metric_directions),
                                  out_of_sample, sandbox_passed, rollback_triggered,
                                  "counter_metric_direction_missing")
    invalid_directions = [m for m, direction in counter_metric_directions.items()
                          if m in counter_metric_deltas and direction not in {"higher_is_better", "lower_is_better"}]
    if invalid_directions:
        return MutationEvaluation(proposal.mutation_id, "rejected", sample_size, primary_delta,
                                  dict(counter_metric_deltas), dict(counter_metric_directions),
                                  out_of_sample, sandbox_passed, rollback_triggered,
                                  "invalid_counter_metric_direction:" + ",".join(sorted(invalid_directions)))
    if not sandbox_passed:
        return MutationEvaluation(proposal.mutation_id, "rejected", sample_size, primary_delta,
                                  dict(counter_metric_deltas), dict(counter_metric_directions), out_of_sample,
                                  False, rollback_triggered, "sandbox_failed")
    if rollback_triggered:
        return MutationEvaluation(proposal.mutation_id, "rejected", sample_size, primary_delta,
                                  dict(counter_metric_deltas), dict(counter_metric_directions), out_of_sample,
                                  True, True, "rollback_condition_triggered")
    if sample_size < max(min_samples, proposal.sample_requirement):
        return MutationEvaluation(proposal.mutation_id, "testing", sample_size, primary_delta,
                                  dict(counter_metric_deltas), dict(counter_metric_directions), out_of_sample,
                                  True, False, "insufficient_samples")
    if not out_of_sample:
        return MutationEvaluation(proposal.mutation_id, "testing", sample_size, primary_delta,
                                  dict(counter_metric_deltas), dict(counter_metric_directions), False,
                                  True, False, "out_of_sample_required")
    if primary_delta is None or primary_delta <= min_primary_delta:
        return MutationEvaluation(proposal.mutation_id, "rejected", sample_size, primary_delta,
                                  dict(counter_metric_deltas), dict(counter_metric_directions), True,
                                  True, False, "primary_metric_not_improved")
    for metric, delta in counter_metric_deltas.items():
        direction = counter_metric_directions[metric]
        if (direction == "higher_is_better" and delta < 0) or (direction == "lower_is_better" and delta > 0):
            return MutationEvaluation(proposal.mutation_id, "rejected", sample_size, primary_delta,
                                      dict(counter_metric_deltas), dict(counter_metric_directions), True,
                                      True, False, f"counter_metric_regressed:{metric}")
    return MutationEvaluation(proposal.mutation_id, "eligible", sample_size, primary_delta,
                              dict(counter_metric_deltas), dict(counter_metric_directions), True,
                              True, False, "evidence_gate_passed")


_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _require_commit_sha(label: str, value: str | None) -> str:
    """A promotion without commit identity promotes nothing executable."""
    if value is None or not str(value).strip():
        raise ValueError(f"missing_commit_identity:{label}")
    sha = str(value).strip()
    if not _GIT_SHA_RE.match(sha):
        raise ValueError(f"malformed_commit_sha:{label}:{sha!r}")
    return sha


def promote_mutation(
    state: Mapping[str, Any],
    proposal: MutationProposal,
    evaluation: MutationEvaluation,
    *,
    production_parent_sha: str | None = None,
    candidate_sha: str | None = None,
    deployment_report: Any = None,
) -> dict[str, Any]:
    """Create a versioned promotion record without mutating the supplied mapping.

    Approval is bound to the exact proposal, the exact evaluation, and the
    exact pair of commits. Three ways that binding used to be absent:

    * The evaluation was never checked against the proposal, even though
      MutationEvaluation carries a mutation_id. Mutation A could be
      promoted on mutation B's evidence.
    * Both commit SHAs were optional and unvalidated, so a mutation could
      be recorded "active" with production_parent_sha=None and
      candidate_sha=None. Nothing executable is identified by such a
      record, and a later rollback has no version to restore.
    * Retiring prior mutations wrote through to the caller's dicts. The
      list was copied; the entries inside it were the same objects. This
      function's own docstring promised otherwise.
    """
    if evaluation.mutation_id != proposal.mutation_id:
        raise ValueError(
            f"evaluation_mutation_mismatch:proposal={proposal.mutation_id}:"
            f"evaluation={evaluation.mutation_id}"
        )
    # mutation_id is a name. Binding approval to the name alone let a
    # proposal be evaluated with one patch, keep its id, have its patch
    # swapped, and then be promoted on the earlier evaluation -- so the
    # evidence attested to code that was not the code being shipped.
    expected_digest = proposal_digest(proposal)
    if evaluation.proposal_digest != expected_digest:
        raise ValueError(
            f"evaluation_content_mismatch:proposal={expected_digest[:12]}:"
            f"evaluation={(evaluation.proposal_digest or 'absent')[:12]}"
        )
    if evaluation.status != "eligible":
        raise ValueError(f"mutation_not_eligible:{evaluation.reason}")

    # A deployment report is the evidence that the commits named here exist
    # and that the candidate one actually runs. Supplying the two SHAs by
    # hand records a promotion of something that may never have been applied
    # anywhere, which is what promote_mutation did before: it updated a
    # dictionary and nothing went live.
    # REQUIRED, not optional. "Promoted" is supposed to mean the change is
    # live and running. Accepting two SHA-shaped strings from the caller let
    # the system record a promotion of something that was never applied
    # anywhere, which is the same shape as every other defect found here:
    # a claim accepted because it looked like evidence.
    #
    # Verified before making it mandatory: zero production callers passed
    # SHAs by hand, so nothing outside this module's own tests relied on it.
    from .deployment import verify_deployment_report

    problems = verify_deployment_report(deployment_report, proposal)
    if problems:
        raise ValueError("deployment_not_evidenced:" + ",".join(problems))
    production_parent_sha = deployment_report.parent_sha
    candidate_sha = deployment_report.candidate_sha
    parent_sha = _require_commit_sha("production_parent_sha", production_parent_sha)
    child_sha = _require_commit_sha("candidate_sha", candidate_sha)
    if parent_sha == child_sha:
        raise ValueError(f"candidate_sha_equals_parent:{child_sha}")

    current = dict(state.get("self_improvement", {}))
    # deepcopy: entries are retired below, and a shallow list copy shares
    # the dicts with the caller's state.
    history = [deepcopy(entry) for entry in current.get("mutations", [])]
    version = int(current.get("version", 0)) + 1
    entry = {
        **proposal.as_dict(),
        "state": "active",
        "version": version,
        "parent_version": proposal.parent_version,
        "production_parent_sha": parent_sha,
        "candidate_sha": child_sha,
        "evaluation": {
            "mutation_id": evaluation.mutation_id,
            "status": evaluation.status,
            "sample_size": evaluation.sample_size,
            "primary_delta": evaluation.primary_delta,
            "counter_metric_deltas": dict(evaluation.counter_metric_deltas),
            "counter_metric_directions": dict(evaluation.counter_metric_directions),
            "out_of_sample": evaluation.out_of_sample,
            "sandbox_passed": evaluation.sandbox_passed,
            "rollback_triggered": evaluation.rollback_triggered,
            "reason": evaluation.reason,
        },
    }
    for old in history:
        old["state"] = "retired"
    history.append(entry)
    current.update({"version": version, "mutations": history, "active_mutation_id": proposal.mutation_id})
    result = deepcopy(dict(state))
    result["self_improvement"] = current
    return result


def rollback_mutation(state: Mapping[str, Any], *, reason: str, restore_version: str | None = None) -> dict[str, Any]:
    """Retire the active mutation and preserve the causal rollback record.

    Deep-copies for the same reason promote_mutation does: ``list(...)``
    copies the list and shares every dict inside it, so marking an entry
    rolled_back wrote straight through into the caller's historical state.
    Rewriting history in place is the one thing this module exists to
    prevent.
    """
    result = deepcopy(dict(state))
    current = dict(result.get("self_improvement", {}))
    history = [deepcopy(entry) for entry in current.get("mutations", [])]
    active = current.get("active_mutation_id")
    if active:
        for item in history:
            if item.get("mutation_id") == active and item.get("state") == "active":
                item["state"] = "rolled_back"
                item["rollback_reason"] = reason
                item["restore_version"] = restore_version
        current["active_mutation_id"] = None
        current["last_rollback_reason"] = reason
        current["restore_version"] = restore_version
        current["mutations"] = history
        result["self_improvement"] = current
    return result
