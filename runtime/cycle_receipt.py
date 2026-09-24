"""Deterministic cycle-receipt contract for the Sovereign LLM host.

The host supplies the cognitive facts; this module validates and canonicalizes
the receipt. It never claims that an LLM stage ran unless that stage is present
in the host-supplied execution list, and it never submits brokerage orders.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from .engine import hash_record
from .schema_versions import SUPPORTED_FULL_CYCLE_VERSIONS
from .self_improvement import MUTATION_PROPOSAL_STATUSES

REQUIRED_FIELDS = frozenset({
    "cycle_id", "run_id", "started_at", "completed_at", "mode",
    "snapshot_id", "stages", "tools_used", "status", "decision_status",
    "self_improvement", "host"
})

REQUIRED_STAGE_FIELDS = frozenset({
    "stage_id", "agent_id", "status", "execution_order", "started_at",
    "completed_at", "tools_used"
})

ALLOWED_STAGE_STATUS = frozenset({"completed", "blocked", "skipped", "failed"})
ALLOWED_DECISIONS = frozenset({"blocked", "wait", "researching", "experiment", "recommended"})
ALLOWED_RECEIPT_STATUS = frozenset({"completed", "blocked", "failed"})
ALLOWED_EVIDENCE_COMPLETENESS = frozenset({"complete", "partial"})
ALLOWED_EXECUTOR_ORIGINS = frozenset({
    "local_primary", "github_fallback", "manual",
})
FINALIZATION_SCHEMA_VERSION = 1

# status, decision_status and every stage status were checked against a
# vocabulary. mode was required and accepted any string, so a typo produced a
# receipt that validated cleanly and then grouped with nothing.
#
# This list is the modes actually present in the journal, not an invented
# taxonomy: each one was produced by a real code path and rejecting them would
# invalidate stored receipts that describe real cycles. The point is not to
# prune history, it is that a TENTH value cannot appear silently.
ALLOWED_MODES = frozenset({
    # The live paths.
    "production",
    "host_input_replay",      # host commits observations, executor runs stages
    "maintenance",
    # Host-execution paths from before the host_input bridge.
    "production-host-full-cycle",
    "production-host-validation",
    "production-host-fail-closed",
    # End-to-end exercises.
    "e2e-multi-run",
    "e2e-failure-shadow",
    "e2e-smoke-manual",
})


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def receipt_hash(receipt: Mapping[str, Any]) -> str:
    body = dict(receipt)
    body.pop("receipt_hash", None)
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def verify_receipt_hash(receipt: Mapping[str, Any]) -> bool:
    expected = receipt.get("receipt_hash")
    return isinstance(expected, str) and expected == receipt_hash(receipt)


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timezone_required")
    return dt


# A receipt was valid with ANY non-empty stage list, so a "completed"
# production cycle carrying only the portfolio stage and a decision_status of
# "recommended" validated cleanly. That receipt claims a trade recommendation
# produced without research and without a decision stage ever running: half a
# cycle asserting it was a whole one.
#
# Hardcoding stage names per mode was the first attempt and it is wrong: it
# assumes one plan, so any caller running a different one fails for no real
# reason. The plan is not the envelope's business. What IS its business is
# that the receipt declare which stages its plan required, and that every one
# of them actually appear. A producer declaring a thin plan is then visible
# in the receipt and auditable, which silent absence never was.
REQUIRED_STAGE_MODES = frozenset({
    "production", "host_input_replay", "production-host-full-cycle",
})


def _required_stage_errors(receipt: Mapping[str, Any]) -> list[str]:
    """A completed cycle must have actually run the plan it declares."""
    mode = str(receipt.get("mode", ""))
    declared = receipt.get("required_stages")
    if declared is None:
        # NOT an error here. Every receipt already in the journal predates
        # this field, and previous_receipt_status validates the predecessor
        # before each cycle, so failing on absence would invalidate real
        # history and refuse to start any new cycle at all. The requirement
        # is enforced where it can be met, at construction, by build_receipt.
        return []
    if not isinstance(declared, Sequence) or isinstance(declared, str):
        return ["required_stages_malformed"]
    stages = receipt.get("stages")
    if not isinstance(stages, Sequence):
        return []
    present = {str(s.get("stage_id")): str(s.get("status"))
               for s in stages if isinstance(s, Mapping)}
    # A blocked cycle still LISTS every stage, marking the ones that did not
    # run. An absent stage is the different, stronger claim that the plan
    # never included it.
    errors = [f"required_stage_missing:{stage_id}"
              for stage_id in declared if str(stage_id) not in present]
    # Presence was all that was checked, so a required decision stage marked
    # "skipped" satisfied the plan while the receipt still said completed and
    # recommended. A stage that did not run has not run, whatever word the
    # producer chose for it.
    if str(receipt.get("status")) == "completed":
        errors.extend(
            f"required_stage_not_completed:{stage_id}:{present[str(stage_id)]}"
            for stage_id in declared
            if str(stage_id) in present
            and present[str(stage_id)] != "completed")
    return errors


def validate_receipt(receipt: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(f"missing:{key}" for key in sorted(REQUIRED_FIELDS - set(receipt)))

    if errors:
        return errors

    try:
        started = _parse_ts(str(receipt["started_at"]))
        completed = _parse_ts(str(receipt["completed_at"]))
        if completed < started:
            errors.append("completed_before_started")
    except (TypeError, ValueError):
        errors.append("invalid_cycle_timestamps")

    provenance = receipt.get("executor_provenance")
    if "executor_provenance" in receipt:
        if not isinstance(provenance, Mapping) or set(provenance) != {
            "schema_version", "receipt_writer",
            "input_commit_sha", "input_committed_at",
        }:
            errors.append("executor_provenance_fields")
        else:
            if (
                type(provenance["schema_version"]) is not int
                or provenance["schema_version"] != 1
            ):
                errors.append("executor_provenance_version")
            if (
                not isinstance(provenance["receipt_writer"], str)
                or provenance["receipt_writer"] not in ALLOWED_EXECUTOR_ORIGINS
            ):
                errors.append("executor_origin_invalid")
            sha = provenance["input_commit_sha"]
            committed = provenance["input_committed_at"]
            if (sha is None) != (committed is None):
                errors.append("executor_input_commit_incomplete")
            if sha is not None and (
                not isinstance(sha, str)
                or len(sha) != 40
                or any(c not in "0123456789abcdef" for c in sha)
            ):
                errors.append("executor_input_commit_invalid")
            if committed is not None:
                try:
                    _parse_ts(committed)
                except (TypeError, ValueError, AttributeError):
                    errors.append("executor_input_committed_at_invalid")

    for field in ("cycle_id", "run_id", "mode", "snapshot_id", "status"):
        if not str(receipt[field]).strip():
            errors.append(f"empty:{field}")
    if receipt["status"] not in ALLOWED_RECEIPT_STATUS:
        errors.append(f"invalid_receipt_status:{receipt['status']}")

    stages = receipt["stages"]
    if not isinstance(stages, list) or not stages:
        errors.append("stages_must_be_nonempty_list")
        stages = []

    orders: list[int] = []
    stage_ids: set[str] = set()
    stage_statuses: list[tuple[str, str]] = []
    for stage in stages:
        if not isinstance(stage, Mapping):
            errors.append("stage_not_object")
            continue
        missing = REQUIRED_STAGE_FIELDS - set(stage)
        errors.extend(f"stage_missing:{key}" for key in sorted(missing))
        if missing:
            continue
        sid = str(stage["stage_id"])
        if (
            "executor_origin" in stage
            and (
                not isinstance(stage["executor_origin"], str)
                or stage["executor_origin"] not in ALLOWED_EXECUTOR_ORIGINS
            )
        ):
            errors.append(f"stage_executor_origin_invalid:{sid}")
        if sid in stage_ids:
            errors.append(f"duplicate_stage:{sid}")
        stage_ids.add(sid)
        status = str(stage["status"])
        if status not in ALLOWED_STAGE_STATUS:
            errors.append(f"invalid_stage_status:{sid}:{status}")
        try:
            order = int(stage["execution_order"])
            orders.append(order)
        except (TypeError, ValueError):
            errors.append(f"invalid_execution_order:{sid}")
            continue
        try:
            if _parse_ts(str(stage["completed_at"])) < _parse_ts(str(stage["started_at"])):
                errors.append(f"stage_completed_before_started:{sid}")
        except (TypeError, ValueError):
            errors.append(f"invalid_stage_timestamps:{sid}")
        if not isinstance(stage["tools_used"], list):
            errors.append(f"stage_tools_must_be_list:{sid}")
        # A stage naming a different cycle is evidence from somewhere else.
        # Receipts are the proof that THIS cycle ran, so borrowing a stage
        # from another one is the cheapest possible way to look complete.
        if stage.get("cycle_id") is not None and str(stage["cycle_id"]) != str(receipt["cycle_id"]):
            errors.append(f"stage_belongs_to_other_cycle:{sid}:{stage['cycle_id']}")
        # Stage work must fall inside the cycle that claims it. Without this
        # a receipt could carry stages that ran days earlier or finished
        # after the cycle closed, which is the same borrowing by another
        # route.
        try:
            cycle_start = _parse_ts(str(receipt["started_at"]))
            cycle_end = _parse_ts(str(receipt["completed_at"]))
            if _parse_ts(str(stage["started_at"])) < cycle_start:
                errors.append(f"stage_started_before_cycle:{sid}")
            if _parse_ts(str(stage["completed_at"])) > cycle_end:
                errors.append(f"stage_completed_after_cycle:{sid}")
        except (TypeError, ValueError, KeyError):
            pass
        stage_statuses.append((sid, status))

    if orders and sorted(orders) != list(range(1, len(orders) + 1)):
        errors.append("execution_order_not_contiguous")

    # A receipt must not claim more than its own stages support. A cycle
    # reporting status "completed" while one of its stages is blocked or
    # failed, or a decision of "recommended" resting on a blocked decision
    # stage, is a receipt contradicting itself -- and the receipt is the
    # artifact everything downstream trusts.
    unfinished = [(sid, st) for sid, st in stage_statuses if st in {"blocked", "failed"}]
    if unfinished and str(receipt["status"]) == "completed":
        errors.append("completed_receipt_has_unfinished_stages:"
                      + ",".join(f"{sid}={st}" for sid, st in sorted(unfinished)))
    if unfinished and str(receipt["decision_status"]) == "recommended":
        errors.append("recommendation_rests_on_unfinished_stages:"
                      + ",".join(f"{sid}={st}" for sid, st in sorted(unfinished)))

    if not isinstance(receipt["tools_used"], list):
        errors.append("tools_used_must_be_list")
    if str(receipt.get("mode", "")) not in ALLOWED_MODES:
        errors.append(f"mode_not_allowed:{receipt.get('mode')}")
    errors.extend(_required_stage_errors(receipt))
    if receipt["decision_status"] not in ALLOWED_DECISIONS:
        errors.append(f"invalid_decision_status:{receipt['decision_status']}")

    if "host_input_schema_version" in receipt:
        version = receipt["host_input_schema_version"]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version not in SUPPORTED_FULL_CYCLE_VERSIONS
        ):
            errors.append("invalid_host_input_schema_version")
    if "corrects_candidate_id" in receipt:
        corrects_candidate_id = receipt["corrects_candidate_id"]
        if (
            corrects_candidate_id is not None
            and (
                not isinstance(corrects_candidate_id, str)
                or not corrects_candidate_id
                or corrects_candidate_id != corrects_candidate_id.strip()
            )
        ):
            errors.append("invalid_corrects_candidate_id")
    if "decision_repetition" in receipt:
        repetition = receipt["decision_repetition"]
        if not isinstance(repetition, Mapping):
            errors.append("invalid_decision_repetition")
        else:
            expected = {
                "review_required",
                "prior_cycle_id",
                "prior_decision_status",
                "review",
                "selection_advisories",
            }
            if set(repetition) != expected:
                errors.append("invalid_decision_repetition_fields")
            if not isinstance(repetition.get("review_required"), bool):
                errors.append("invalid_decision_repetition_required")
            for field in ("prior_cycle_id", "prior_decision_status"):
                value = repetition.get(field)
                if value is not None and (
                    not isinstance(value, str)
                    or not value.strip()
                ):
                    errors.append(
                        f"invalid_decision_repetition_{field}"
                    )
            review = repetition.get("review")
            if review is not None and not isinstance(review, Mapping):
                errors.append("invalid_decision_repetition_review")
            if not isinstance(
                repetition.get("selection_advisories"),
                list,
            ):
                errors.append(
                    "invalid_decision_repetition_selection_advisories"
                )
            elif any(
                not isinstance(value, str) or not value.strip()
                for value in repetition["selection_advisories"]
            ):
                errors.append(
                    "invalid_decision_repetition_selection_advisory"
                )
            if (
                repetition.get("review_required") is True
                and (
                    not isinstance(
                        repetition.get("prior_cycle_id"), str
                    )
                    or not repetition["prior_cycle_id"].strip()
                    or not isinstance(
                        repetition.get("prior_decision_status"), str
                    )
                    or not repetition["prior_decision_status"].strip()
                )
            ):
                errors.append(
                    "incomplete_required_decision_repetition_context"
                )
            if (
                repetition.get("review_required") is False
                and review is not None
            ):
                errors.append("unexpected_decision_repetition_review")
    if (
        "finalization_schema_version" in receipt
        and receipt["finalization_schema_version"]
        != FINALIZATION_SCHEMA_VERSION
    ):
        errors.append("invalid_finalization_schema_version")
    if "evidence_completeness" in receipt:
        completeness = receipt["evidence_completeness"]
        if completeness not in ALLOWED_EVIDENCE_COMPLETENESS:
            errors.append("invalid_evidence_completeness")
        advisories = receipt.get("evidence_advisories")
        if not isinstance(advisories, list):
            errors.append("evidence_advisories_must_be_list")
        elif completeness == "complete" and advisories:
            errors.append("complete_receipt_has_evidence_advisories")
        elif completeness == "partial" and not advisories:
            errors.append("partial_receipt_requires_evidence_advisories")

    si = receipt["self_improvement"]
    if not isinstance(si, Mapping):
        errors.append("self_improvement_must_be_object")
    else:
        for key in ("status", "mutation_ids", "gates"):
            if key not in si:
                errors.append(f"self_improvement_missing:{key}")
        if "mutation_ids" in si and not isinstance(si["mutation_ids"], list):
            errors.append("self_improvement_mutation_ids_must_be_list")
        if "gates" in si and not isinstance(si["gates"], Mapping):
            errors.append("self_improvement_gates_must_be_object")
        if "proposal" in si:
            proposal = si["proposal"]
            if not isinstance(proposal, Mapping):
                errors.append("self_improvement_proposal_must_be_object")
            else:
                for key in ("record_id", "proposal_digest", "status"):
                    if key not in proposal:
                        errors.append(
                            f"self_improvement_proposal_missing:{key}"
                        )
                record_id = proposal.get("record_id")
                if (
                    not isinstance(record_id, str)
                    or not record_id.startswith("mutation-proposal:")
                ):
                    errors.append(
                        "self_improvement_proposal_record_id_invalid"
                    )
                digest = proposal.get("proposal_digest")
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef"
                           for character in digest)
                ):
                    errors.append(
                        "self_improvement_proposal_digest_invalid"
                    )
                if proposal.get("status") not in MUTATION_PROPOSAL_STATUSES:
                    errors.append(
                        "self_improvement_proposal_status_invalid"
                    )
                mutation_ids = si.get("mutation_ids")
                if (
                    isinstance(mutation_ids, list)
                    and isinstance(record_id, str)
                    and record_id.removeprefix("mutation-proposal:")
                    not in mutation_ids
                ):
                    errors.append(
                        "self_improvement_proposal_mutation_id_mismatch"
                    )

    host = receipt["host"]
    if not isinstance(host, Mapping):
        errors.append("host_must_be_object")
    elif not host.get("cognitive_execution_claim"):
        errors.append("missing_cognitive_execution_claim")

    if "receipt_hash" in receipt and not verify_receipt_hash(receipt):
        errors.append("receipt_hash_mismatch")

    return errors


def build_receipt(*, cycle_id: str, run_id: str, started_at: str,
                  completed_at: str, mode: str, snapshot_id: str,
                  stages: Iterable[Mapping[str, Any]], tools_used: Iterable[str],
                  status: str, decision_status: str,
                  self_improvement: Mapping[str, Any], host: Mapping[str, Any],
                  blockers: Iterable[str] = (),
                  required_stages: Iterable[str] | None = None,
                  host_input_schema_version: int | None = None,
                  finalization_schema_version: int | None = (
                      FINALIZATION_SCHEMA_VERSION
                  ),
                  corrects_candidate_id: str | None = None,
                  corrects_candidate_id_declared: bool = False,
                  carry_forward: Mapping[str, Any] | None = None,
                  evidence_completeness: str | None = None,
                  evidence_advisories: Iterable[str] = (),
                  decision_repetition: Mapping[str, Any] | None = None,
                  executor_provenance: Mapping[str, Any] | None = None,
                  ) -> dict[str, Any]:
    receipt = {
        "cycle_id": cycle_id,
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "mode": mode,
        "snapshot_id": snapshot_id,
        # What the plan required, so a subset cannot pass as the whole.
        "required_stages": (list(required_stages)
                            if required_stages is not None else None),
        "stages": [dict(s) for s in stages],
        "tools_used": sorted(set(str(x) for x in tools_used)),
        "status": status,
        "decision_status": decision_status,
        "blockers": sorted(set(str(x) for x in blockers)),
        "self_improvement": dict(self_improvement),
        "host": dict(host),
    }
    if host_input_schema_version is not None:
        receipt["host_input_schema_version"] = host_input_schema_version
    if finalization_schema_version is not None:
        receipt["finalization_schema_version"] = (
            finalization_schema_version
        )
    if (
        corrects_candidate_id_declared
        or corrects_candidate_id is not None
    ):
        receipt["corrects_candidate_id"] = corrects_candidate_id
    if carry_forward is not None:
        receipt["carry_forward"] = dict(carry_forward)
    if evidence_completeness is not None:
        receipt["evidence_completeness"] = evidence_completeness
        receipt["evidence_advisories"] = sorted(set(
            str(value) for value in evidence_advisories
        ))
    if decision_repetition is not None:
        receipt["decision_repetition"] = dict(decision_repetition)
    if executor_provenance is not None:
        receipt["executor_provenance"] = dict(executor_provenance)
    errors = validate_receipt(receipt)
    # Enforced at construction rather than in validate_receipt: a new receipt
    # for a portfolio-affecting cycle CAN declare its plan, while one already
    # in the journal cannot be made to retroactively.
    if mode in REQUIRED_STAGE_MODES and required_stages is None:
        errors.append(f"required_stages_not_declared:{mode}")
    if errors:
        raise ValueError("invalid_cycle_receipt:" + ",".join(errors))
    receipt["receipt_hash"] = receipt_hash(receipt)
    return receipt


def validate_audit_receipt_record(record: Mapping[str, Any]) -> list[str]:
    """Validate a persisted audit record carrying a cycle receipt.

    A receipt being CONSTRUCTED has no receipt_hash yet -- build_receipt
    validates first and stamps the hash after. A receipt being PERSISTED
    must carry one, otherwise the execution evidence has no self-check at
    all and a missing hash reads exactly like a valid receipt. That is why
    the presence requirement lives here and not in validate_receipt.
    """
    errors: list[str] = []
    if record.get("record_type") != "cycle_receipt":
        errors.append("wrong_record_type")
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return errors + ["payload_must_be_object"]
    errors.extend(validate_receipt(payload))
    if not str(payload.get("receipt_hash", "")).strip():
        errors.append("missing_receipt_hash")
    record_id = str(record.get("record_id", ""))
    cycle_id = str(payload.get("cycle_id", ""))
    if record_id != f"cycle-receipt:{cycle_id}":
        errors.append("record_id_mismatch")
    if record.get("record_hash") != hash_record(dict(record)):
        errors.append("audit_record_hash_mismatch")
    return errors


def latest_receipt_record(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The last persisted cycle receipt that has not been retired.

    A superseded receipt is one whose damage has been acknowledged in the
    journal and formally retired. Continuing to validate against it blocks
    every future cycle forever on a defect that was already accepted, which
    makes one historical write a permanent brick.

    Only EXPLICIT supersession is skipped. Damage nobody has acknowledged
    still blocks, which is the whole point of the check: this reads past
    defects that were signed for, not past defects that were ignored.
    """
    from .integrity import superseded_defects

    receipts = [r for r in records if r.get("record_type") == "cycle_receipt"]
    if not receipts:
        return None
    from .integrity import order_chain

    retired = {record_id for record_id, _ in superseded_defects(list(records))}
    live = [r for r in receipts if str(r.get("record_id", "")) not in retired]
    if not live:
        return receipts[-1]
    # Prefer a receipt that is actually ON the chain. Selecting by file order
    # picked a STRANDED receipt as the predecessor, so a new cycle anchored
    # itself onto orphaned history and inherited a break it had no part in --
    # which is exactly what the host was told not to do, done by the runner.
    #
    # Skipping is not silent: previous_receipt_status reports any non-retired
    # receipt passed over, so a NEW broken receipt still surfaces instead of
    # being quietly stepped around.
    # Chain order, not file order. order_chain already resolves which receipt
    # genuinely follows which; taking the last of `live` instead re-imposed
    # the order the lines happened to sit in, so two correctly linked receipts
    # written in the other order selected the earlier one as the predecessor.
    ordered, _ = order_chain(list(records))
    live_ids = {id(r) for r in live}
    on_chain = [r for r in ordered if id(r) in live_ids]
    return on_chain[-1] if on_chain else live[-1]


def skipped_unreachable_receipts(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Non-retired receipts passed over because they are not on the chain."""
    from .integrity import order_chain, superseded_defects

    retired = {record_id for record_id, _ in superseded_defects(list(records))}
    ordered, _ = order_chain(list(records))
    reachable = {id(r) for r in ordered}
    chosen = latest_receipt_record(records)
    by_hash = {str(x.get("record_hash")) for x in records if x.get("record_hash")}
    skipped: list[str] = []
    for record in records:
        if record.get("record_type") != "cycle_receipt":
            continue
        if str(record.get("record_id", "")) in retired or record is chosen:
            continue
        if id(record) in reachable:
            continue
        # A receipt whose OWN parent does not resolve is fresh damage and
        # must not be quietly stepped over -- that is the hole skipping was
        # always at risk of reopening. One stranded by an ancestor inherited
        # the problem, and the ancestor is already reported.
        prev = record.get("prev_hash")
        inherited = prev is not None and str(prev) in by_hash
        skipped.append(
            f"{record.get('record_id')}" if inherited
            else f"UNANCHORED:{record.get('record_id')}"
        )
    return skipped


def latest_receipt(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    record = latest_receipt_record(records)
    return record.get("payload") if record else None


def _ancestry_errors(record: Mapping[str, Any],
                     records: Sequence[Mapping[str, Any]]) -> list[str]:
    """The receipt must be anchored in the chain it claims to belong to.

    A record can be internally perfect -- valid receipt, correct
    receipt_hash, correct record_hash -- and still reference a parent that
    does not exist. Verification used to check only the record itself, so
    such a receipt was reported "verified", ok=True, and the next cycle
    proceeded on an execution record that is not attached to any history.

    That is precisely the failure mode already present in this journal:
    a malformed prev_hash orphaned six records while each of them
    self-validated. Self-consistency is not ancestry.
    """
    errors: list[str] = []
    by_hash: dict[str, Mapping[str, Any]] = {}
    for other in records:
        digest = other.get("record_hash")
        if digest and other is not record:
            by_hash.setdefault(str(digest), other)

    # Checking only the immediate parent proves the record is attached to
    # SOMETHING, not that what it is attached to is sound. A receipt whose
    # parent exists but whose grandparent is missing, or whose ancestor has
    # been tampered with, was reported verified -- which is the live
    # situation in this journal, where a receipt validates while its
    # ancestry still reaches a damaged historical record.
    from .integrity import superseded_defects

    retired = {record_id for record_id, _ in superseded_defects(list(records))}
    seen: set[str] = set()
    current: Mapping[str, Any] = record
    depth = 0
    while True:
        prev = current.get("prev_hash")
        if prev is None:
            break
        prev = str(prev)
        if prev in seen:
            errors.append(f"ancestry_cycle_at_depth_{depth}:{prev[:12]}")
            break
        seen.add(prev)
        parent = by_hash.get(prev)
        if parent is None:
            # Symmetric with the hash-mismatch case below: a broken LINK that
            # has been acknowledged and retired stops the walk without
            # blocking, while an unacknowledged one still refuses. Every
            # defect must be signed for; none may be ignored.
            if str(current.get("record_id", "")) in retired:
                break
            errors.append(
                f"prev_hash_not_in_journal:{prev[:12]}" if depth == 0
                else f"ancestor_missing_at_depth_{depth + 1}:{prev[:12]}"
            )
            break
        # An ancestor whose own content no longer hashes to the digest its
        # child points at is a rewritten record, and everything descending
        # from it inherits that.
        if parent.get("record_hash") != hash_record(dict(parent)):
            # Acknowledged damage is not the same as unacknowledged damage.
            # A superseded ancestor has been signed for in the journal and
            # retired; refusing to walk past it blocks every future cycle
            # forever on a defect that was already accepted, which is what
            # append-only supersession exists to resolve.
            #
            # The provenance is still broken and that stays visible: the
            # integrity gate reports the chain damage, and the supersession
            # record itself names what was lost. What changes here is only
            # whether one historical write permanently bricks the system.
            if str(parent.get("record_id", "")) not in retired:
                errors.append(
                    f"ancestor_hash_mismatch_at_depth_{depth + 1}:"
                    f"{parent.get('record_id')}"
                )
                break
        current = parent
        depth += 1
    return errors


NO_RECEIPT = "no_receipt_persisted"
RECEIPTS_NONE_VALID = "receipts_persisted_none_valid"
VALID_RECEIPT = "valid_receipt_persisted"


def derive_receipt_status(records: Sequence[Mapping[str, Any]]) -> str:
    """What the journal actually shows about persisted receipts.

    STATE.json carried this as hand-written prose and said
    "migration_pending_first_persisted_receipt" while eleven receipt records
    existed, several of them valid. A curated status field drifts from the
    thing it describes and then outranks it, because prose is what gets read.

    Deliberately narrow. This says a valid receipt EXISTS. It says nothing
    about whether the cognitive cycle is proven, which is a different and
    much larger claim that no envelope check can support.
    """
    receipts = [
        record for record in records
        if record.get("record_type") == "cycle_receipt"
        or str(record.get("record_id", "")).startswith("cycle-receipt:")
    ]
    if not receipts:
        return NO_RECEIPT
    for record in receipts:
        if not validate_audit_receipt_record(record):
            return VALID_RECEIPT
    return RECEIPTS_NONE_VALID


def previous_receipt_status(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Verify the most recent cycle receipt before a new cycle proceeds.

    No receipt is an explicit migration/first-run state. Once one exists,
    any validation, envelope or hash failure blocks the next cycle rather than
    letting the host continue on an unverified execution record.
    """
    record = latest_receipt_record(records)
    if record is None:
        return {"status": "missing_initial", "ok": True, "errors": []}
    errors = validate_audit_receipt_record(record)
    errors.extend(_ancestry_errors(record, records))
    # Reported, not refused. These are stranded for reasons already
    # acknowledged in the journal, so blocking on them would brick the loop
    # again; leaving them invisible would hide which receipts were passed
    # over. The caller sees the list and can judge.
    skipped = skipped_unreachable_receipts(records)
    errors.extend(
        f"unanchored_receipt_skipped:{rid.split(':', 1)[1]}"
        for rid in skipped if rid.startswith("UNANCHORED:")
    )
    receipt = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
    return {
        "status": "verified" if not errors else "invalid",
        "ok": not errors,
        "cycle_id": receipt.get("cycle_id"),
        "run_id": receipt.get("run_id"),
        "receipt_hash": receipt.get("receipt_hash"),
        "audit_record_id": record.get("record_id"),
        "skipped_unreachable_receipts": skipped,
        "errors": errors,
    }
