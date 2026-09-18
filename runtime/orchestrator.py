"""Deterministic control-plane contracts for the Sovereign orchestrator.

The runtime never creates model workers. The ChatGPT host owns cognitive work.
This module only defines dependencies, sequential isolation, memory-stage
ordering, self-improvement ordering, and fail-closed execution plumbing. There
is no brokerage order-submission path.
"""
from __future__ import annotations

from copy import deepcopy

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


@dataclass(frozen=True)
class AgentJob:
    agent_id: str
    phase: str
    depends_on: tuple[str, ...] = ()
    required: bool = True
    reason: str = ""


SPECIALISTS = (
    "value_fcf",
    "quality_garp",
    "special_situations",
    "options_volatility",
    "futures_macro",
    "relative_value_hedging",
    "insider_institutional",
    "buybacks_restructuring",
    "sentiment_momentum",
    "cross_market",
    "instrument_substitution",
)

_RESERVED_AGENT_IDS = frozenset(
    {
        "portfolio",
        "market_scout",
        "research_director",
        "memory_retrieval",
        "memory_distillation",
        "evidence_arbitration",
        "portfolio_fit",
        "counterfactual",
        "adversarial",
        "governance_review",
        "decision",
        "learning_audit",
        "meta_research",
        "self_improvement",
    }
)


def _validate_specialist_id(name: str) -> None:
    if not name or name in _RESERVED_AGENT_IDS:
        raise ValueError(f"invalid_specialist:{name!r}")


def build_plan(
    *,
    ibkr_current: bool,
    research_needed: bool,
    specialist_ids: Iterable[str] | None = None,
    weekly_learning: bool = True,
    full_execution: bool = True,
    deep_memory: bool = False,
) -> list[AgentJob]:
    """Build the dependency graph; specialist names may be dynamically discovered.

    Memory retrieval is part of every planning path. Deep memory distillation
    and self-improvement are enabled only when the corresponding host modes run.
    """
    jobs = [
        AgentJob(
            "portfolio",
            "observe",
            required=True,
            reason=(
                "IBKR authoritative portfolio state"
                if ibkr_current
                else "IBKR state must be refreshed before decision"
            ),
        ),
        AgentJob(
            "market_scout",
            "discovery",
            ("portfolio",),
            required=True,
            reason="host-authored market discovery and research budget",
        ),
        AgentJob(
            "research_director",
            "direct",
            ("market_scout",),
            required=True,
        ),
        AgentJob(
            "memory_retrieval",
            "memory",
            ("research_director",),
            required=True,
            reason="bounded query-driven context assembly from Active Brain and Research Memory",
        ),
    ]

    if research_needed:
        # The Research Director must SELECT. Code must not invent the
        # selection by defaulting to the whole catalogue.
        #
        # AGENT_ORCHESTRATOR.md is explicit that the baseline catalogue is
        # "a capability map, not a mandatory list and not a ceiling", and
        # that passes are chosen by decision value, portfolio gaps,
        # unresolved questions, source availability and HOST CAPACITY.
        # This function used to ignore all of that and default to all 11,
        # every one required, producing a 23-stage all-or-nothing plan that
        # does not fit in one host wake. An honest host then had to mark
        # everything blocked, which is why no complete cycle receipt ever
        # existed. The deadlock was this default, not a host limitation.
        #
        # PHILOSOPHY.md: no hardcoded sequences, the brain decides each
        # cycle. A silent default here IS a hardcoded sequence.
        if specialist_ids is None:
            raise ValueError(
                "specialist_selection_required: build_plan cannot choose "
                "specialists for you. The Research Director selects them per "
                "cycle from decision value, portfolio gaps, unresolved "
                "questions, source availability and host capacity. "
                f"The capability map is SPECIALISTS ({len(SPECIALISTS)} roles) "
                "and is neither a mandatory list nor a ceiling -- new roles "
                "may be invented when justified. See AGENT_ORCHESTRATOR.md."
            )
        selected = tuple(specialist_ids)
        if not selected:
            raise ValueError(
                "empty_specialist_selection: research_needed=True requires at "
                "least one specialist. To run a cycle with no research pass, "
                "set research_needed=False."
            )
        if len(set(selected)) != len(selected):
            duplicates = sorted({s for s in selected if selected.count(s) > 1})
            raise ValueError(f"duplicate_specialist:{','.join(duplicates)}")
        for name in selected:
            _validate_specialist_id(name)

        jobs.extend(
            AgentJob(
                name,
                "specialist",
                ("memory_retrieval",),
                required=True,
                reason=(
                    "baseline research capability"
                    if name in SPECIALISTS
                    else "dynamically discovered research role"
                ),
            )
            for name in selected
        )
        gate_depends = selected if selected else ("memory_retrieval",)
        jobs += [
            AgentJob("evidence_arbitration", "gate", gate_depends, required=True),
            AgentJob("portfolio_fit", "gate", ("evidence_arbitration",), required=True),
            AgentJob("counterfactual", "gate", ("portfolio_fit",), required=True),
            AgentJob("adversarial", "gate", ("counterfactual",), required=True),
            AgentJob(
                "governance_review",
                "governance",
                ("adversarial",),
                required=True,
                reason="integrity, goals and experiment/prompt governance",
            ),
            AgentJob("decision", "decision", ("governance_review",), required=True),
        ]
    else:
        jobs += [
            AgentJob(
                "governance_review",
                "governance",
                ("memory_retrieval",),
                required=True,
                reason="integrity/goals review even without new research",
            ),
            AgentJob(
                "decision",
                "decision",
                ("governance_review",),
                required=True,
                reason="no new research branch required",
            ),
        ]

    jobs.append(
        AgentJob(
            "learning_audit",
            "learning",
            ("decision",),
            required=True if weekly_learning else full_execution,
            reason=(
                "mandatory outcome/evolution/audit cycle"
                if (weekly_learning or full_execution)
                else "capture only material outcomes/incidents"
            ),
        )
    )
    jobs.append(
        AgentJob(
            "meta_research",
            "meta_research",
            ("learning_audit",),
            required=full_execution,
            reason="measure decision effectiveness and propose evidence-gated system improvement",
        )
    )
    jobs.append(
        AgentJob(
            "self_improvement",
            "self_improvement",
            ("meta_research",),
            required=full_execution,
            reason="turn recurring failures into tested, versioned, rollback-capable mutations",
        )
    )
    if deep_memory:
        jobs.append(
            AgentJob(
                "memory_distillation",
                "memory",
                ("self_improvement",),
                required=full_execution,
                reason="deep memory distillation, reconstruction and Active Brain maintenance",
            )
        )

    if not full_execution:
        jobs = [
            AgentJob(j.agent_id, j.phase, j.depends_on, False, "optional partial mode")
            if j.phase == "specialist"
            else j
            for j in jobs
        ]
    return jobs


def validate_plan(jobs: Iterable[AgentJob]) -> list[str]:
    jobs = list(jobs)
    errors: list[str] = []
    ids = [j.agent_id for j in jobs]
    if len(ids) != len(set(ids)):
        errors.extend(
            f"duplicate_job:{x}"
            for x in sorted({x for x in ids if ids.count(x) > 1})
        )
    known = set(ids)
    for job in jobs:
        if job.agent_id in job.depends_on:
            errors.append(f"self_dependency:{job.agent_id}")
        for dep in job.depends_on:
            if dep not in known:
                errors.append(f"missing_dependency:{job.agent_id}:{dep}")
    remaining = set(ids)
    deps = {j.agent_id: set(j.depends_on) for j in jobs}
    while remaining:
        ready = {agent for agent in remaining if not (deps[agent] & remaining)}
        if not ready:
            errors.append("cyclic_dependency")
            break
        remaining -= ready
    return errors


def specialist_branches(jobs: Iterable[AgentJob]) -> list[AgentJob]:
    return [job for job in jobs if job.phase == "specialist"]


def execution_layers(jobs: Iterable[AgentJob]) -> list[tuple[AgentJob, ...]]:
    jobs = list(jobs)
    errors = validate_plan(jobs)
    if errors:
        raise ValueError("invalid_plan:" + ",".join(errors))
    by_id = {job.agent_id: job for job in jobs}
    remaining = set(by_id)
    layers: list[tuple[AgentJob, ...]] = []
    while remaining:
        ready = tuple(
            job
            for job in jobs
            if job.agent_id in remaining
            and all(dep not in remaining for dep in job.depends_on)
        )
        if not ready:
            raise ValueError("cyclic_dependency")
        layers.append(ready)
        remaining.difference_update(job.agent_id for job in ready)
    return layers


def execution_sequence(jobs: Iterable[AgentJob]) -> list[AgentJob]:
    """Return the canonical sequential host order."""
    return [job for layer in execution_layers(jobs) for job in layer]


Handler = Callable[[AgentJob, Mapping[str, Any], Mapping[str, Any]], Any]


@dataclass(frozen=True)
class ExecutionResult:
    completed: tuple[str, ...]
    outputs: Mapping[str, Any]
    blocked: tuple[str, ...]
    errors: tuple[str, ...]
    review_only: bool = True


# A stage signals it could not do its work by returning one of these in
# its result. Anything else (including no status key at all) is success,
# which keeps handlers that return plain payloads working unchanged.
# A denylist of failure words is the wrong polarity for a safety gate: any
# status the host invents that is not listed counts as success. `skipped`,
# `deferred` and `not_run` all reported a stage that did not execute and
# their dependents ran anyway, which is precisely what AGENT_ORCHESTRATOR.md
# forbids -- "record skipped passes and reasons, and never claim work that
# did not execute".
#
# The success vocabulary is the one cycle_receipt already enforces, where
# `completed` is the only successful stage status and `skipped` sits beside
# `blocked` and `failed`. `ok`/`success` are accepted as synonyms.
#
# A handler that returns no "status" key at all is unaffected: most handlers
# return plain data, and absence of a self-report is not a failure report.
_SUCCESS_STATUSES = frozenset({"completed", "complete", "ok", "success", "done"})


def _reported_status(result: Any) -> str | None:
    """The status a handler reported about its own execution, if any."""
    if isinstance(result, Mapping):
        value = result.get("status")
        if isinstance(value, str):
            return value.strip().lower()
    return None


def run_plan(
    *,
    jobs: Iterable[AgentJob],
    handlers: Mapping[str, Handler],
    context: Mapping[str, Any] | None = None,
) -> ExecutionResult:
    """Execute handlers sequentially with dependency-scoped visibility."""
    sequence = execution_sequence(jobs)
    base_context = dict(context or {})
    outputs: dict[str, Any] = {}
    completed: list[str] = []
    blocked: list[str] = []
    errors: list[str] = []

    for job in sequence:
        failed = [dep for dep in job.depends_on if dep in blocked]
        missing = [dep for dep in job.depends_on if dep not in outputs]
        if failed or missing:
            reason = failed or missing
            if job.required:
                blocked.append(job.agent_id)
                errors.append(
                    f"blocked_dependency:{job.agent_id}:{','.join(reason)}"
                )
            else:
                errors.append(
                    f"skipped_optional:{job.agent_id}:{','.join(reason)}"
                )
            continue

        handler = handlers.get(job.agent_id)
        if handler is None:
            if job.required:
                blocked.append(job.agent_id)
                errors.append(f"missing_required_handler:{job.agent_id}")
            else:
                errors.append(f"skipped_optional_handler:{job.agent_id}")
            continue

        try:
            # Every stage gets its OWN deep copy of the context and of its
            # dependency outputs. They previously shared one object, so a
            # specialist mutating a nested value -- say adding a key to the
            # portfolio snapshot -- leaked that mutation into every later
            # specialist and into the caller's own dict.
            #
            # Two invariants broke at once. AGENT_ORCHESTRATOR.md promises
            # each pass "the same immutable portfolio snapshot", and it was
            # neither immutable nor necessarily the same. Worse, it is an
            # anchoring channel: sibling conclusions must stay hidden until
            # Evidence Arbitration, and a shared mutable context is a
            # side-door between specialists that the dependency graph does
            # not show.
            result = handler(
                job,
                deepcopy(base_context),
                {dep: deepcopy(outputs[dep]) for dep in job.depends_on},
            )
        except Exception as exc:
            if job.required:
                blocked.append(job.agent_id)
                errors.append(
                    f"handler_error:{job.agent_id}:{type(exc).__name__}:{exc}"
                )
            else:
                errors.append(
                    f"optional_handler_error:{job.agent_id}:{type(exc).__name__}:{exc}"
                )
            continue

        # A handler that REPORTS itself blocked must block. Previously any
        # return value at all counted as completion: a stage could return
        # {"status": "blocked"} and be recorded completed, with no error,
        # and its dependents would run. That is a false-success path
        # straight into the decision stage, and it defeats the one
        # behaviour the host gets right -- refusing to claim work it did
        # not do. The host marks stages blocked; the orchestrator was
        # counting them as done.
        reported = _reported_status(result)
        if reported is not None and reported not in _SUCCESS_STATUSES:
            if job.required:
                blocked.append(job.agent_id)
                errors.append(f"stage_reported_{reported}:{job.agent_id}")
            else:
                errors.append(f"optional_stage_reported_{reported}:{job.agent_id}")
            continue

        outputs[job.agent_id] = result
        completed.append(job.agent_id)
    return ExecutionResult(
        tuple(completed), outputs, tuple(blocked), tuple(errors), True
    )
