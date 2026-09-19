"""Production host adapter for executing and resuming Sovereign cycles.

The ChatGPT host owns all cognitive work. This module provides the durable
execution envelope around that work: predecessor-receipt verification,
per-stage persistence, restart/resume, isolated handler invocation, and final
cycle-receipt creation. It never submits, modifies, cancels, or transmits an
IBKR order.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from .audit_store import AuditJournal
from .cycle_receipt import ALLOWED_DECISIONS, build_receipt, previous_receipt_status
from .orchestrator import AgentJob, Handler, execution_sequence, run_plan

_STAGE_RECORD_TYPE = "cycle_stage"
_SUCCESS_STATUSES = frozenset({"completed", "complete", "ok", "success", "done"})
_STAGE_STATUSES = frozenset({"completed", "blocked", "skipped", "failed"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage_record_id(cycle_id: str, stage_id: str) -> str:
    return f"cycle-stage:{cycle_id}:{stage_id}"


def _as_status(result: Any) -> str:
    if not isinstance(result, Mapping):
        return "completed"
    value = result.get("status")
    if value is None:
        return "completed"
    normalized = str(value).strip().lower()
    if normalized in _SUCCESS_STATUSES:
        return "completed"
    if normalized in _STAGE_STATUSES:
        return normalized
    return "blocked"


def _tools(result: Any) -> list[str]:
    if not isinstance(result, Mapping):
        return []
    values = result.get("tools_used", [])
    if not isinstance(values, list):
        raise ValueError("stage_tools_used_must_be_list")
    return sorted({str(value) for value in values})


def _decision_status(result: Any) -> str | None:
    if not isinstance(result, Mapping):
        return None
    value = result.get("decision_status", result.get("decision"))
    if value is None:
        return None
    return str(value).strip().lower()


@dataclass(frozen=True)
class ResumeState:
    cycle_id: str
    run_id: str
    reused_stage_ids: tuple[str, ...]
    new_stage_ids: tuple[str, ...]


def _plan_fingerprint(context: Mapping[str, Any],
                      sequence: Sequence[AgentJob]) -> str:
    """Hash of the inputs and plan a stage was run under.

    snapshot_id is a LABEL. Binding reuse to it means a caller that keeps the
    label while changing what is underneath silently reuses stale stage
    output: same "SNAP-A", price 1 replaced by 999, and the decision still
    consumes 1. run_host_cycle happens to build a content-addressed
    snapshot_id, so the real path was safe by that caller's convention rather
    than by anything enforced here.

    previous_receipt is excluded: it changes as the journal grows and is not
    an input the stages reason from.

    What this CANNOT see is data a handler closes over rather than receiving
    through context. That is outside the executor's view entirely, so callers
    must pass the inputs stages depend on rather than capturing them.
    """
    payload = {
        "context": {k: v for k, v in sorted(context.items())
                    if k != "previous_receipt"},
        "plan": [[job.agent_id, list(job.depends_on), bool(job.required)]
                 for job in sequence],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


class ProductionHostExecutor:
    """Execute one host cycle with append-only stage checkpoints.

    ``handlers`` are the actual LLM-host stage handlers. Every handler must
    receive the exact three positional arguments defined by ``run_plan``. The
    executor does not manufacture research conclusions or portfolio state.

    ``context`` is host-owned immutable input for the cycle. The executor
    additionally requires a ``snapshot_id`` and, for portfolio-sensitive
    cycles, the portfolio handler should refresh live IBKR and prove the
    authoritative ``ibkr_as_of`` returned by that call.
    """

    def __init__(self, journal: AuditJournal,
                 all_records: Callable[[], list[dict[str, Any]]] | None = None):
        self.journal = journal
        # The journal is ONE chain spanning SEVERAL files, and an AuditJournal
        # reads a single file. Validating the predecessor against one file
        # sees a partial chain and reports prev_hash_not_in_journal for links
        # that are perfectly intact in a sibling file -- so the executor
        # refused to start on a healthy journal.
        #
        # Appends still go to one file; only the view used for validation
        # spans them all.
        self._all_records = all_records

    def _records(self) -> list[dict[str, Any]]:
        return list(self._all_records()) if self._all_records else self.journal.read()

    def _check_predecessor(self) -> dict[str, Any]:
        records = self._records()
        state = previous_receipt_status(records)
        if not state["ok"]:
            raise RuntimeError("invalid_previous_receipt:" + ";".join(state["errors"]))
        return state

    def _existing_stage_records(self, cycle_id: str) -> dict[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        for record in self._records():
            if record.get("record_type") != _STAGE_RECORD_TYPE:
                continue
            payload = record.get("payload")
            if not isinstance(payload, Mapping) or str(payload.get("cycle_id")) != cycle_id:
                continue
            stage_id = str(payload.get("stage_id", ""))
            if stage_id:
                result[stage_id] = record
        return result

    def run(
        self,
        *,
        jobs: Iterable[AgentJob],
        handlers: Mapping[str, Handler],
        context: Mapping[str, Any],
        cycle_id: str | None = None,
        run_id: str | None = None,
        mode: str = "production",
        snapshot_id: str | None = None,
        host_claim: str,
        self_improvement: Mapping[str, Any],
        started_at: str | None = None,
        host_input_schema_version: int | None = None,
        carry_forward: Mapping[str, Any] | None = None,
        evidence_completeness: str | None = None,
        evidence_advisories: Iterable[str] = (),
    ) -> tuple[Any, dict[str, Any], ResumeState]:
        """Run/resume a cycle and persist each genuinely new stage exactly once."""
        predecessor = self._check_predecessor()
        cycle_id = cycle_id or f"cycle-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        run_id = run_id or f"run-{uuid4().hex}"
        # A resumed cycle is the SAME cycle, so it began when its first stage
        # began. Defaulting to now() made the receipt claim a start time later
        # than stages it already contained, which validate_receipt correctly
        # refuses as stage_started_before_cycle. Reporting the resume moment
        # as the cycle start would also understate how long the cycle took.
        existing_for_start = self._existing_stage_records(cycle_id)
        earliest_persisted = min(
            (str(r["payload"].get("started_at")) for r in existing_for_start.values()
             if isinstance(r.get("payload"), Mapping) and r["payload"].get("started_at")),
            default=None,
        )
        started_at = started_at or earliest_persisted or _now()
        snapshot_id = snapshot_id or str(context.get("snapshot_id", ""))
        if not snapshot_id.strip():
            raise ValueError("snapshot_id_required")
        if not host_claim.strip():
            raise ValueError("host_cognitive_execution_claim_required")

        sequence = execution_sequence(jobs)
        sequence_order = {job.agent_id: index for index, job in enumerate(sequence, start=1)}
        plan_fingerprint = _plan_fingerprint(context, sequence)
        existing = self._existing_stage_records(cycle_id)
        reused: list[str] = []
        new: list[str] = []
        persisted_outputs: dict[str, Any] = {}

        base_context = deepcopy(dict(context))
        base_context.update({
            "cycle_id": cycle_id,
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "previous_receipt": deepcopy(predecessor),
        })

        wrapped: dict[str, Handler] = {}
        stage_meta: dict[str, dict[str, Any]] = {}

        for job in sequence:
            record = existing.get(job.agent_id)
            if record is not None:
                payload = record.get("payload")
                if not isinstance(payload, Mapping):
                    raise RuntimeError(f"invalid_persisted_stage:{job.agent_id}")
                if str(payload.get("run_id")) != run_id:
                    raise RuntimeError(
                        f"persisted_stage_run_mismatch:{job.agent_id}:{payload.get('run_id')}"
                    )
                if str(payload.get("stage_id")) != job.agent_id:
                    raise RuntimeError(f"persisted_stage_id_mismatch:{job.agent_id}")
                # A resumed cycle keeps its cycle_id and run_id, so neither
                # notices that the snapshot underneath changed. Reused stages
                # would then carry observations of one portfolio into a
                # decision the receipt attributes to another -- the receipt
                # names the new snapshot while the evidence came from the old.
                persisted_snapshot = payload.get("snapshot_id")
                if persisted_snapshot is None:
                    # Written before stages recorded their snapshot. What it
                    # ran against is genuinely unknown, and "unknown" is not
                    # "the same"; reusing it would be a guess the receipt
                    # would then state as fact.
                    raise RuntimeError(
                        f"persisted_stage_snapshot_unknown:{job.agent_id}")
                if str(persisted_snapshot) != snapshot_id:
                    raise RuntimeError(
                        "persisted_stage_snapshot_mismatch:"
                        f"{job.agent_id}:{persisted_snapshot}"
                    )
                persisted_plan = payload.get("plan_fingerprint")
                if persisted_plan is None:
                    raise RuntimeError(
                        f"persisted_stage_plan_unknown:{job.agent_id}")
                if str(persisted_plan) != plan_fingerprint:
                    raise RuntimeError(
                        "persisted_stage_plan_mismatch:"
                        f"{job.agent_id}:{persisted_plan}"
                    )
                persisted_outputs[job.agent_id] = deepcopy(payload.get("output"))
                stage_meta[job.agent_id] = {
                    "stage_id": job.agent_id,
                    "agent_id": job.agent_id,
                    "status": str(payload.get("status")),
                    "execution_order": sequence_order[job.agent_id],
                    "started_at": str(payload.get("started_at")),
                    "completed_at": str(payload.get("completed_at")),
                    "tools_used": list(payload.get("tools_used", [])),
                }
                reused.append(job.agent_id)
                continue

            handler = handlers.get(job.agent_id)
            if handler is None:
                if job.required:
                    raise RuntimeError(f"missing_required_handler:{job.agent_id}")
                continue

            def make_wrapper(handler: Handler) -> Handler:
                def wrapped_handler(
                    current_job: AgentJob,
                    current_context: Mapping[str, Any],
                    dependencies: Mapping[str, Any],
                ) -> Any:
                    started = _now()
                    result = handler(current_job, current_context, dependencies)
                    completed = _now()
                    status = _as_status(result)
                    tools_used = _tools(result)
                    output = deepcopy(result)
                    record_payload = {
                        "cycle_id": cycle_id,
                        "run_id": run_id,
                        "snapshot_id": snapshot_id,
                        "plan_fingerprint": plan_fingerprint,
                        "stage_id": current_job.agent_id,
                        "agent_id": current_job.agent_id,
                        "status": status,
                        "execution_order": sequence_order[current_job.agent_id],
                        "started_at": started,
                        "completed_at": completed,
                        "tools_used": tools_used,
                        "output": output,
                    }
                    self.journal.append(
                        record_id=_stage_record_id(cycle_id, current_job.agent_id),
                        record_type=_STAGE_RECORD_TYPE,
                        agent="sovereign-host",
                        payload=record_payload,
                    )
                    stage_meta[current_job.agent_id] = {
                        key: record_payload[key]
                        for key in (
                            "stage_id", "agent_id", "status", "execution_order",
                            "started_at", "completed_at", "tools_used",
                        )
                    }
                    new.append(current_job.agent_id)
                    return result

                return wrapped_handler

            wrapped[job.agent_id] = make_wrapper(handler)

        for agent_id, output in persisted_outputs.items():
            def make_reuse_handler(output: Any) -> Handler:
                def reuse_handler(
                    _job: AgentJob,
                    _context: Mapping[str, Any],
                    _dependencies: Mapping[str, Any],
                ) -> Any:
                    return deepcopy(output)

                return reuse_handler

            wrapped[agent_id] = make_reuse_handler(output)

        result = run_plan(jobs=sequence, handlers=wrapped, context=base_context)
        completed_at = _now()

        for index, job in enumerate(sequence, start=1):
            if job.agent_id not in stage_meta:
                stage_meta[job.agent_id] = {
                    "stage_id": job.agent_id,
                    "agent_id": job.agent_id,
                    "status": "blocked",
                    "execution_order": index,
                    "started_at": completed_at,
                    "completed_at": completed_at,
                    "tools_used": [],
                }
        stages = [stage_meta[job.agent_id] for job in sequence]

        decision = _decision_status(result.outputs.get("decision"))
        if decision is None:
            decision = "blocked" if result.blocked else "wait"
        if decision not in ALLOWED_DECISIONS:
            decision = "blocked"

        receipt = build_receipt(
            cycle_id=cycle_id,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            mode=mode,
            snapshot_id=snapshot_id,
            stages=stages,
            tools_used=sorted({tool for stage in stages for tool in stage.get("tools_used", [])}),
            status="completed" if not result.blocked else "blocked",
            decision_status=decision,
            self_improvement=deepcopy(dict(self_improvement)),
            host={
                "cognitive_execution_claim": host_claim,
                "host_type": "chatgpt-scheduled-task",
            },
            blockers=result.errors,
            # The plan's own required jobs, so the receipt is checked against
            # what this cycle was actually meant to run rather than against a
            # hardcoded list that assumes one plan.
            required_stages=[job.agent_id for job in sequence if job.required],
            host_input_schema_version=host_input_schema_version,
            carry_forward=carry_forward,
            evidence_completeness=evidence_completeness,
            evidence_advisories=evidence_advisories,
        )

        # A rerun rebuilt the receipt with a fresh completed_at, so the value
        # returned to the caller differed from the one in the journal: same
        # cycle, two receipts, and the caller acting on the one that is not
        # the record. The persisted receipt IS the receipt; a later run
        # reports it rather than minting a rival.
        persisted = next(
            (record for record in self.journal.read()
             if record.get("record_id") == f"cycle-receipt:{cycle_id}"),
            None)
        if persisted is not None:
            stored = persisted.get("payload")
            if isinstance(stored, Mapping):
                receipt = deepcopy(dict(stored))
        if persisted is None:
            caused_by = []
            predecessor_id = predecessor.get("audit_record_id")
            if predecessor_id:
                caused_by.append(str(predecessor_id))
            # Only stages that were actually persisted. A blocked stage still
            # appears in the receipt's stage list, which is honest reporting,
            # but it gets a synthetic entry in stage_meta and no journal
            # record. Citing stage_meta therefore pointed the causal graph at
            # records nobody ever wrote, so the receipt named a cause that
            # does not exist.
            persisted_stage_ids = set(reused) | set(new)
            caused_by.extend(
                _stage_record_id(cycle_id, job.agent_id)
                for job in sequence
                if job.agent_id in persisted_stage_ids
            )
            self.journal.append_cycle_receipt(receipt, caused_by=caused_by)

        return result, receipt, ResumeState(
            cycle_id=cycle_id,
            run_id=run_id,
            reused_stage_ids=tuple(reused),
            new_stage_ids=tuple(new),
        )


# These factories intentionally require real host callables. They do not fake
# IBKR or research results. The scheduled ChatGPT task supplies the connected
# tool adapters when it actually runs the cycle.
def make_portfolio_handler(refresh_ibkr: Callable[[], Mapping[str, Any]]) -> Handler:
    """Create the portfolio stage from a real IBKR observation provider."""
    def handler(
        job: AgentJob,
        _context: Mapping[str, Any],
        _dependencies: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        observation = deepcopy(dict(refresh_ibkr()))
        as_of = observation.get("as_of")
        if not isinstance(as_of, str) or not as_of.strip():
            raise ValueError("ibkr_portfolio_as_of_required")
        if str(observation.get("source", "")).lower() != "ibkr":
            raise ValueError("portfolio_source_must_be_ibkr")
        # `.get(...)` is falsy when the key is ABSENT, so an adapter that
        # never mentioned orders passed silently: absence of a denial was
        # being read as a denial. The most safety-critical property in the
        # system was guarded by a flag the caller could simply omit.
        #
        # This is still a self-report. The runtime cannot observe IBKR side
        # effects, so it cannot prove no order was placed -- it can only
        # require the adapter to say so affirmatively, and refuse to infer it
        # from silence. Anything stronger has to come from IBKR itself.
        if "order_submission_used" not in observation:
            raise ValueError(
                "order_submission_declaration_required: the adapter must state "
                "order_submission_used=False explicitly; silence is not a denial"
            )
        if observation["order_submission_used"] is not False:
            raise ValueError("live_order_submission_forbidden")
        return {
            "stage": job.agent_id,
            "status": "completed",
            "snapshot": observation,
            "ibkr_as_of": as_of,
            "tools_used": ["Interactive Brokers (IBKR)"],
        }

    return handler


def make_tool_stage_handler(
    tool_runner: Callable[[AgentJob, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
) -> Handler:
    """Wrap a real research/tool runner and require tool-call evidence in output."""
    def handler(
        job: AgentJob,
        context: Mapping[str, Any],
        dependencies: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        result = deepcopy(dict(tool_runner(job, context, dependencies)))
        calls = result.get("tool_calls")
        if not isinstance(calls, list):
            raise ValueError(f"tool_calls_required:{job.agent_id}")
        result.setdefault("status", "completed")
        if "tools_used" not in result:
            result["tools_used"] = sorted({
                str(call.get("tool"))
                for call in calls
                if isinstance(call, Mapping) and call.get("tool")
            })
        return result

    return handler
