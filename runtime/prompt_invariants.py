"""Let the host improve its own instructions, but not weaken them.

The host found two real ambiguities in the standing prompt within ten minutes
of being given it -- the as_of definition, and a bootstrap step sitting inside
the text the recurring task executes -- both of which had already cost real
cycles. It is better at reading these than whoever wrote them. Forbidding it
from editing its own instructions would throw that away.

The danger is not editing. It is the one edit that cannot be walked back: a
host that quietly drops "never place an order" from its own constraints, and
is thereafter operating under rules nobody agreed to. Prose has no hash chain,
so a deleted line leaves nothing behind.

So prompt files are editable and the constraints inside them are not. This is
the same split the code side already has: FORBIDDEN_MUTATION_TOKENS lets a
mutation change runtime behaviour while refusing one that adds order
submission. Here the invariants are sentences rather than tokens, checked by
meaning-bearing keywords rather than exact text, so the host can rewrite them
more clearly without being able to remove them.

Rewording is expected. Deletion is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .profile_paths import code_root

PROMPTS_DIR = code_root() / "prompts"
STANDING_PROMPT = "host-standing-schedule.md"
STANDING_PROMPT_MIN_BYTES = 52_000
STANDING_PROMPT_MAX_BYTES = 56_000

REFUSAL_CONTRACT_TOKENS: tuple[str, ...] = (
    "refusal postmortem",
    "durable prevention change",
    "retry_contract",
    "patch_base.path",
    "must_change_paths",
    "json_pointer",
    "required_state",
    "retry_target_unsatisfied",
    "malformed json",
    "pretty-printed json",
)

# Each invariant is (name, [phrases that must all appear]). Matching is
# case-insensitive and substring-based so the host can rewrite the sentence
# around them. The phrases are chosen to be hard to satisfy accidentally and
# impossible to satisfy while having removed the constraint.
REQUIRED_INVARIANTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("no_order_submission",
     ("order_submission_used", "false")),
    # "transmit", not "place". Creating an order INSTRUCTION is the proposal
    # mechanism: it stages the order in IBKR where the operator sees it,
    # reviews it and transmits it, and it executes nothing. The earlier
    # wording forbade "place, modify or cancel an order", which banned the
    # very mechanism the proposal was meant to use, and the host obeyed --
    # it said so explicitly, that it had not created an instruction because
    # the prompt told it the operator executes.
    #
    # What stays forbidden is transmitting a live order. The host cannot do
    # that in any case; the constraint records the boundary rather than
    # enforcing a permission IBKR already withholds.
    ("never_transmit_a_live_order",
     ("never transmit",)),
    ("operator_transmits",
     ("operator transmits",)),
    ("no_rewriting_accepted_files",
     ("never rewrite", "already accepted")),
    ("no_inferred_execution",
     ("never infer", "executed")),
    ("no_fabricated_tool_use",
     ("never claim a tool",)),
    ("staging_commit_required",
     ("staging commit must succeed", "publication failed")),
    ("staging_metadata_retained",
     ("debug details", "staged path", "commit sha", "execution receipt")),
    ("decision_focused_operator_report",
     ("strategy family", "instruments examined", "conviction",
      "strongest supporting evidence", "strongest counterevidence")),
    ("routine_report_boundary",
     ("cycle_id", "staged candidate", "not validator or executor success",
      "prior cycle")),
    ("debug_details_triggers",
     ("publication failed", "refused the prior candidate",
      "explicitly request debug details")),
    ("validator_success_before_claim",
     ("host input validator", "do not claim success")),
    ("instruction_operator_brief",
     ("operator brief", "why now", "do not transmit if")),
    ("instruction_store_disagreement",
     ("connector/app state", "lifecycle `unknown`",
      "call delete once", "never recreate")),
    ("recommended_instruction_contract",
     ("time_in_force", "rationale_one_line", "review_condition",
      "rollback_condition", "contract_description")),
    ("market_session_awareness",
     ("market_sessions", "both_open", "iana timezone")),
    ("accepted_delta_first_research",
     ("delta-first", "accepted cycles", "unchanged question")),
    ("data_led_research_agenda",
     ("research_agenda", "rejected alternative",
      "specialist_stage_id", "fresh trigger")),
    ("research_tool_result_provenance",
     ("result_origin", "host_summary", "source_refs", "do not prove",
      "research[].tool_calls")),
    ("capture_origin_non_attestation",
     ("capture_origin", "host-transcribed response", "do not prove")),
    ("semantic_candidate_builder",
     ("semantic_input_schema_version",
      "host_staging/<unique>.semantic.json",
      "deterministic builder")),
    ("bounded_carry_forward",
     ("unchanged_from_prior", "three consecutive cycles",
      "24 consecutive cycles", "carried cycle cannot")),
    ("learning_stage_dispositions",
     ("learning_stage_dispositions", "artifact | no_change",
      "stage:<stage_id>", "finding:<finding_id>",
      "learning-disposition:<cycle_id>:<stage_id>")),
    ("durable_opportunity_ledger",
     ("opportunity_updates", "opportunity_ledger",
      "same opportunity_id", "append-only")),
    ("opportunity_revisit_state_machine",
     ("research_state", "next_question_id", "revisit",
      "expected information gain", "no_new_information")),
    ("session_aware_research_allocation",
     ("allocation_plan", "new_opportunity", "portfolio_risk",
      "market_session_context", "allocation_variance")),
    ("immutable_ex_ante_forecasts",
     ("forecast_registrations", "confidence_probability",
      "supersedes_forecast_id", "resolution rule", "target_at")),
    ("deterministic_forecast_outcomes",
     ("forecast_outcomes", "tool_call_id",
      "observation_window_seconds", "overdue")),
    ("instruction_operator_reconciliation",
     ("instruction_reconciliations", "accepted_modified",
      "saved_instruction_deleted", "live_order_deleted")),
    ("instruction_expiry_decisions",
     ("instruction_expiry", "instruction_expiry_decisions",
      "activity_indexes", "do not create a canary")),
    ("empirical_calibration_boundaries",
     ("empirical_calibration", "direction and range",
      "not proof", "do not score operator quality")),
    ("research_value_census_boundaries",
     ("research_value_census", "adversarial_disputes",
      "single host thread", "not independently sampled agents")),
    ("privacy_bounded_host_feedback",
     ("host_feedback.md", "host-signal", "rich feedback private")),
    ("market_scout_discovery",
     ("market_scout", "market_scout_report", "budget_variance",
      "scout_candidate_id", "candidate order is not a ranking")),
    ("candidate_registry_rediscovery",
     ("candidate_registry", "unpromoted", "rediscovery_of",
      "last_seen_cycle_id", "last_seen_candidate_id")),
    ("tool_manifest_enumeration",
     ("enumerate every action", "start of each day",
      "tool_manifest_report", "full_inventory_command", "`stale` is true")),
    ("tool_probation_lifecycle",
     ("tool_probations", "tool_inventory_record_id",
      "evidence_tool_call_ids", "capability_review")),
    ("optional_research_inbox",
     ("feedback.json.research_inbox", "worker_attested",
      "ignore stale", "phone path proceeds")),
    ("durable_open_goal_creation",
     ("goal_observations", "\"mode\": \"create\"",
      "created_at", "second goal is refused")),
    ("durable_open_goal_progress",
     ("\"mode\": \"progress\"", "observed_value",
      "assessment", "does not close")),
    ("durable_goal_closure",
     ("\"mode\": \"close\"", "partial_target",
      "closure_basis", "runtime computes")),
    ("goal_quality_attribution",
     ("goal_attribution", "not instructions", "not proof")),
    ("morning_brief",
     ("morning brief", "overnight", "instruction/order/fill")),
    ("staged_publication",
     ("host_staging/", "never directly", "host_input/")),
    ("asynchronous_staging",
     ("do not poll", "next scheduled run")),
    ("productive_runtime_budget",
     ("up to 15 minutes", "do not pad runtime",
      "persist the exact continuation point")),
    ("refusal_driven_adaptation",
     ("refusal postmortem", "durable prevention change",
      "before new research")),
    ("targeted_refusal_retry",
     ("retry_contract", "must_change_paths", "json_pointer")),
    ("same_slot_semantic_retry",
     ("retry_contract.patch_base.path",
      "patch that exact archived semantic source",
      "iterate inside the same slot")),
    ("json_emission_discipline",
     ("parse your own output", "pretty-printed json")),
    ("schema_prevention_is_executable",
     ("runtime actually reads", "canonical pretty-printed")),
    ("optional_memory_is_null",
     ("memory_distillation", "to `null`", "never use")),
    ("research_memory_lifecycle",
     ("feedback.json.research_memory", "evidence-gated retirement",
      "`stale` is reversible", "`archived` requires a new id")),
)


def check_prompt(text: str) -> list[str]:
    """Which invariants a prompt no longer states."""
    haystack = " ".join(text.lower().split())
    missing = []
    for name, phrases in REQUIRED_INVARIANTS:
        absent = [p for p in phrases if p not in haystack]
        if absent:
            missing.append(f"{name}:missing={'|'.join(absent)}")
    return missing


def check_prompt_size(text: str) -> list[str]:
    """Keep compaction bounded in both directions."""
    size = len(text.encode("utf-8"))
    if size < STANDING_PROMPT_MIN_BYTES:
        return [
            "standing_prompt_too_small:"
            f"{size}<{STANDING_PROMPT_MIN_BYTES}"
        ]
    if size > STANDING_PROMPT_MAX_BYTES:
        return [
            "standing_prompt_too_large:"
            f"{size}>{STANDING_PROMPT_MAX_BYTES}"
        ]
    return []


def check_refusal_contract_tokens(text: str) -> list[str]:
    """Critical retry vocabulary must survive prose compaction."""
    haystack = " ".join(text.lower().split())
    missing = [
        token for token in REFUSAL_CONTRACT_TOKENS
        if token not in haystack
    ]
    if not missing:
        return []
    return [f"refusal_contract_tokens:missing={'|'.join(missing)}"]


def check_standing_prompt(prompts_dir: Path | str = PROMPTS_DIR) -> list[str]:
    """The standing prompt must exist and must still state its constraints."""
    path = Path(prompts_dir) / STANDING_PROMPT
    if not path.exists():
        # Deleting the file is the most complete way to remove every
        # constraint at once, so its absence is the loudest failure here.
        return [f"standing_prompt_missing:{STANDING_PROMPT}"]
    text = path.read_text(encoding="utf-8")
    return (
        check_prompt(text)
        + check_prompt_size(text)
        + check_refusal_contract_tokens(text)
    )


def main(argv: Sequence[str] | None = None) -> int:
    problems = check_standing_prompt()
    if not problems:
        print(f"{STANDING_PROMPT}: all "
              f"{len(REQUIRED_INVARIANTS)} invariants present")
        return 0
    print(f"{STANDING_PROMPT}: constraints removed or reworded past "
          f"recognition:")
    for problem in problems:
        print(f"  {problem}")
    print("\nThe prompt may be improved. These may not be dropped. If an "
          "invariant is genuinely wrong, change it here in code with the "
          "reasoning, rather than by deleting the sentence.")
    return 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
