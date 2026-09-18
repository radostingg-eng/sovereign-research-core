"""Tell the host why its input was refused, in terms it can act on.

The host has IBKR access and can commit to the repository, but it cannot run
anything here. When the runner refuses an input, the host never finds out: the
error goes to a terminal nobody is watching, and the next scheduled run repeats
the same mistake because nothing told it there was one. That is a loop that
cannot self-correct, and it stalled exactly that way -- the host drifted at
13:03, the runner refused the file, and the 13:03 cycle was simply lost.

An error CODE alone is not enough either. "missing_research" names what failed
but not what would succeed, so acting on it requires guessing the shape. Every
code therefore carries what the runtime expected and what to do instead, and
the feedback file carries a worked example of a well-formed input.

This writes to the input directory rather than the audit journal on purpose.
The journal is the immutable record of what happened; this is a mutable
message addressed to the next run, and conflating the two would put a file the
host is meant to overwrite inside an append-only chain.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .schema_invariants import (
    CANONICAL_EXAMPLE_PATH,
    canonical_schema_status,
    inspect_example,
)

FEEDBACK_FILENAME = "FEEDBACK.json"
VALIDATION_READ_THIS_FIRST = (
    "This is the staging validator's reply to your last candidate. "
    "If 'refused' is non-empty, commit a corrected NEW file under "
    "host_staging/; never write host_input/ directly."
)

# Every code validate_input can emit. The test suite refuses a code with no
# entry here, so a new refusal cannot ship as an unexplained one.
REFUSAL_GUIDANCE: dict[str, dict[str, str]] = {
    "malformed_json": {
        "means": "The file is not valid JSON, so nothing in it could be read.",
        "fix": "Re-emit the file as strict JSON. The usual causes are a "
               "trailing comma after the last item in an object or array, a "
               "single-quoted string, an unquoted key, a comment, or a "
               "truncated write. The detail includes line, column, character "
               "offset, and escaped nearby text. Extra data with open_depth=0 "
               "means the root object was closed before a later top-level "
               "fragment. Check the closing braces around "
               "tool_manifest_report: manifest_discrepancies and "
               "unreachable_manifest_connectors remain inside it, while "
               "tool_provenance is a later top-level sibling. Rebuild from "
               "the schema and commit a new staging file; nothing else about "
               "the refused cycle was examined.",
    },
    "host_input_not_json_sentinel": {
        "means": "The staged file contains a placeholder word rather than a "
                 "cycle document.",
        "fix": "Create a new candidate from the complete schema. Never commit "
               "PLACEHOLDER, TODO, or TBD as host input.",
    },
    "host_input_not_utf8": {
        "means": "The staged file is not valid UTF-8 text.",
        "fix": "Create a new UTF-8 JSON candidate. The detail gives the first "
               "undecodable byte offset; the original bytes were archived.",
    },
    "duplicate_json_key": {
        "means": "A JSON object repeats a key, so a normal parser would "
                 "silently discard one of the conflicting values.",
        "fix": "Create a new candidate with each object key exactly once. The "
               "detail names the duplicated key.",
    },
    "missing_snapshot": {
        "means": "The top-level 'snapshot' key was absent or was not an object.",
        "fix": "Include 'snapshot' as an object holding the IBKR observation: "
               "source, as_of, order_submission_used, positions, and balances.",
    },
    "contradictory_source": {
        "means": "Top-level 'source' and snapshot.source disagree.",
        "fix": "State it once. If both appear they must be identical; nothing "
               "here can tell which one is wrong.",
    },
    "contradictory_as_of": {
        "means": "Top-level 'as_of' and snapshot.as_of disagree.",
        "fix": "State it once. Two different observation times mean one of them "
               "does not describe this snapshot.",
    },
    "contradictory_order_submission_used": {
        "means": "Top-level and snapshot 'order_submission_used' disagree.",
        "fix": "State it once, as false. A safety declaration that contradicts "
               "itself is not a declaration.",
    },
    "source_must_be_ibkr": {
        "means": "The snapshot did not declare IBKR as its source.",
        "fix": "Set source to 'ibkr'. Portfolio reasoning runs only against a "
               "live brokerage observation, never against recalled figures.",
    },
    "missing_as_of": {
        "means": "The snapshot did not say when it was observed.",
        "fix": "Set as_of to the observation time in ISO 8601 with an explicit "
               "timezone, for example 2026-09-16T14:01:25Z.",
    },
    "unparseable_as_of": {
        "means": "as_of was present but is not a parseable ISO 8601 timestamp.",
        "fix": "Use ISO 8601 with an explicit timezone, for example "
               "2026-09-16T14:01:25Z. A bare date or a naive local time is "
               "ambiguous in an hourly chain.",
    },
    "as_of_without_timezone": {
        "means": "as_of parsed but carries no timezone, e.g. "
                 "'2026-09-16 14:01:25'.",
        "fix": "Add an explicit offset: 2026-09-16T14:01:25Z. A naive local "
               "time is ambiguous by the offset, so the receipt would be "
               "anchored to a moment that cannot be resolved later.",
    },
    "as_of_in_future": {
        "means": "A newly staged cycle claimed an observation time more than "
                 "15 minutes ahead of the validator clock.",
        "fix": "Refresh current time and evidence, then use the actual "
               "timezone-qualified observation timestamp. Do not copy a "
               "future session time to make a follow-up appear later.",
    },
    "order_submission_declaration_required": {
        "means": "'order_submission_used' was absent.",
        "fix": "State it explicitly as false. Silence is not a denial: an "
               "absent declaration cannot be read as 'no orders were sent'.",
    },
    "live_order_submission_forbidden": {
        "means": "'order_submission_used' was anything other than false.",
        "fix": "This runtime never submits live orders. Propose instructions "
               "for review; do not submit and then declare it.",
    },
    "missing_research": {
        "means": "'research' was absent, empty, or not a list.",
        "fix": "Send a non-empty LIST, one object per research question: "
               "{'question': ..., 'finding': ..., 'tool_calls': [...]}. A "
               "single object with one shared tool_calls array is refused "
               "because it cannot show which evidence produced which finding.",
    },
    "research_entry_not_an_object": {
        "means": "An element of the 'research' list was not an object.",
        "fix": "Each element must be an object with question, finding and "
               "tool_calls.",
    },
    "research_without_question": {
        "means": "A research row did not say what was asked.",
        "fix": "Give each row a non-empty 'question'. A finding with no "
               "question cannot be judged relevant later.",
    },
    "research_without_finding": {
        "means": "A research row did not say what was concluded.",
        "fix": "Give each row a non-empty 'finding' stating what the evidence "
               "showed.",
    },
    "research_without_tool_calls": {
        "means": "A research row carried a conclusion with no evidence.",
        "fix": "Give each row a non-empty 'tool_calls' list recording the calls "
               "that produced the finding. A pass that genuinely used no tools "
               "is not research and should not be reported as research.",
    },
    "tool_call_not_an_object": {
        "means": "An element of a 'tool_calls' list was not an object.",
        "fix": "Each call must be an object: "
               "{'tool': ..., 'call': ..., 'result': ...}.",
    },
    "tool_call_without_tool": {
        "means": "A tool call did not name the tool that was invoked.",
        "fix": "Set a non-empty 'tool', for example 'Interactive Brokers "
               "(IBKR)'. Evidence that cannot be attributed cannot be checked.",
    },
    "tool_call_without_result": {
        "means": "A tool call had no 'result' key.",
        "fix": "Include 'result', even when it is empty. An empty result such "
               "as {'orders': []} is a real answer; an absent one means nothing "
               "came back, and a call that returned nothing is not evidence.",
    },
    "tool_provenance_invalid": {
        "means": "A full-cycle research tool call did not distinguish the "
                 "committed result from its source provenance.",
        "fix": "For schema v4, give every call a stable tool_call_id, kind, "
               "and call object with exact action plus JSON arguments. Add "
               "provenance with result_origin, timezone-qualified observed_at, "
               "source_refs, capture, and web_sources. Connector responses "
               "must be JSON values, never Python-repr strings. Use "
               "host_summary only for host prose and label its capture "
               "host_summary_no_response. Redactions require explicit markers "
               "and cannot target investment-evidence fields. The research "
               "finding remains the separate interpretation.",
    },
    "missing_call": {
        "means": "A persisted tool-provenance index references a source call "
                 "that is absent from the immutable accepted input.",
        "fix": "Treat this as a historical integrity defect. Do not recreate "
               "or renumber the missing call, rewrite the input, or delete "
               "the persisted index. Preserve both artifacts and report the "
               "mismatch for a version-aware recovery.",
    },
    "web_sources_url_refs_mismatch": {
        "means": "URL source references and structured web-source metadata "
                 "did not identify the same sanitized URLs.",
        "fix": "Include one web_sources row for every URL or link source_ref, "
               "and no extra rows. Each row has URL, title, nullable "
               "published_at, and timezone-qualified retrieved_at.",
    },
    "order_instructions_required": {
        "means": "The input did not report the IBKR order instructions "
                 "standing at observation time.",
        "fix": "Fetch the current order instructions from IBKR every cycle, "
               "the same way you fetch positions and balances, and send them "
               "as \"order_instructions\": [...]. An empty list is a real "
               "answer and is accepted; omitting the field is not, because "
               "not looking and finding nothing are different facts.",
    },
    "order_instructions_must_be_a_list": {
        "means": "'order_instructions' was present but was not a list.",
        "fix": "Send a list, one object per standing instruction, or [] when "
               "there are none.",
    },
    "order_instruction_activity_must_be_a_list": {
        "means": "Order-instruction tool evidence was not a list.",
        "fix": "Use order_instruction_activity as a list with one object per "
               "get, create, or delete call.",
    },
    "order_instruction_activity_not_object": {
        "means": "An order-instruction activity row was not an object.",
        "fix": "Each activity row needs operation, tool, request when "
               "applicable, result, and instruction_id when returned.",
    },
    "order_instruction_operation_invalid": {
        "means": "An order-instruction activity used an unknown operation.",
        "fix": "Use get, create, or delete. Live order submission is not an "
               "order-instruction operation and remains forbidden.",
    },
    "order_instruction_tool_required": {
        "means": "An order-instruction activity did not identify its tool.",
        "fix": "Record the exact IBKR connector tool name used for the call.",
    },
    "order_instruction_result_required": {
        "means": "An order-instruction tool call omitted its result.",
        "fix": "Include the raw result key even when the connector returned "
               "an empty list or object.",
    },
    "order_instruction_request_required": {
        "means": "A create or delete activity omitted the submitted request.",
        "fix": "Record the exact request sent to the IBKR instruction tool.",
    },
    "recommended_instruction_required": {
        "means": "A recommended decision did not contain a complete order "
                 "instruction.",
        "fix": "Include decision.instruction with action, symbol, quantity, "
               "order type, price fields, time in force, expected effect, "
               "and invalidation.",
    },
    "recommended_instruction_field_required": {
        "means": "A staged instruction omitted an operator-facing execution "
                 "or justification field.",
        "fix": "The detail names the field. Include action, quantity, "
               "order_type, time_in_force, rationale_one_line, "
               "review_condition, and rollback_condition.",
    },
    "recommended_instruction_identity_required": {
        "means": "A staged instruction did not identify its instrument.",
        "fix": "Include symbol, contract_description, or contract_id_ex so the "
               "operator can unambiguously identify the instrument.",
    },
    "recommended_instruction_quantity_invalid": {
        "means": "A staged instruction quantity was absent or not positive.",
        "fix": "Use the positive quantity actually staged in IBKR.",
    },
    "recommended_instruction_limit_price_invalid": {
        "means": "A limit instruction had no positive numeric limit price.",
        "fix": "Include the exact positive limit_price staged in IBKR.",
    },
    "recommended_instruction_not_staged": {
        "means": "A recommended decision was not verified as staged in IBKR.",
        "fix": "Call create order instruction, then set instruction_staged "
               "true only after a post-create get confirms it exists.",
    },
    "recommended_instruction_id_required": {
        "means": "A staged recommendation did not carry the IBKR instruction "
                 "identifier.",
        "fix": "Copy the identifier returned by IBKR into "
               "decision.ibkr_instruction_id and the create activity row.",
    },
    "recommended_instruction_create_evidence_required": {
        "means": "A recommended decision had no create-tool evidence.",
        "fix": "Record the actual create order instruction call in "
               "order_instruction_activity.",
    },
    "recommended_instruction_post_get_required": {
        "means": "A recommended decision was not re-read from IBKR after "
                 "creation.",
        "fix": "Call get order instructions after creation, record that call, "
               "and use its result as the committed order_instructions state.",
    },
    "recommended_instruction_missing_from_post_state": {
        "means": "The claimed staged instruction id was absent from the "
                 "post-create IBKR state.",
        "fix": "Do not claim staging. Re-run get order instructions and commit "
               "a new cycle only when the returned list contains the exact id.",
    },
    "instruction_created_without_recommendation": {
        "means": "The host created an instruction while its decision was not "
                 "recommended.",
        "fix": "Do not create canary or speculative instructions. Create one "
               "only for an evidence-backed recommended decision.",
    },
    "instruction_lifecycle_updates_must_be_a_list": {
        "means": "Instruction lifecycle updates were not a list.",
        "fix": "Use a list with one explicit submission, execution, "
               "modification, deletion, rejection, expiry, or unknown event "
               "per object.",
    },
    "instruction_lifecycle_update_not_object": {
        "means": "An instruction lifecycle update was not an object.",
        "fix": "Each update needs recommendation_id, event_id, from_state, "
               "to_state, evidence_ids, evidence, and optional metadata.",
    },
    "instruction_lifecycle_update_invalid": {
        "means": "An instruction lifecycle update violates the legal state "
                 "machine.",
        "fix": "The detail names the transition error. A matching account "
               "order moves instruction_created to submitted; a matching "
               "trade can move submitted or instruction_created to executed.",
    },
    "instruction_lifecycle_evidence_required": {
        "means": "A lifecycle update had no operator or IBKR evidence.",
        "fix": "Include at least one evidence row. Use account-order evidence "
               "for submission, trade evidence for execution, delete/operator "
               "evidence for deletion, and explicit operator or connector "
               "status for rejection. Absence alone is unknown.",
    },
    "instruction_lifecycle_evidence_invalid": {
        "means": "A lifecycle evidence row did not identify a tool and result.",
        "fix": "Each evidence row is an object with the exact tool name and "
               "its returned result.",
    },
    "instruction_submission_requires_account_orders": {
        "means": "An instruction was marked approved or submitted without a "
                 "matching account order.",
        "fix": "Call get account orders and include the matching returned "
               "order. Prefer submitted for this directly observable state; "
               "the existence of an instruction proves only staging.",
    },
    "instruction_execution_requires_account_trades": {
        "means": "An instruction was marked executed without matching trade "
                 "evidence.",
        "fix": "Call get account trades and include the matching fill. "
               "Balances, positions, and account summary may verify the "
               "effect but do not replace the trade record.",
    },
    "instruction_deletion_requires_explicit_evidence": {
        "means": "An instruction was marked deleted without a delete action "
                 "or explicit operator confirmation.",
        "fix": "Use delete order instruction evidence when the host deleted "
               "it, or an explicit operator confirmation when the operator "
               "deleted it. Disappearance alone remains unknown.",
    },
    "instruction_deletion_conflicts_with_fresh_connector_state": {
        "means": "The lifecycle claimed deletion while the same cycle's "
                 "fresh connector snapshot still contained that instruction "
                 "ID.",
        "fix": "Keep the lifecycle unknown while connector state and the "
               "operator-visible app disagree. If connector cleanup is "
               "intended, call delete once, immediately list instructions "
               "again, and claim deleted only after the ID is absent.",
    },
    "instruction_reconciliations_require_schema_v3": {
        "means": "Instruction reconciliation was supplied outside the "
                 "structured schema-v3/v4 full-cycle contract.",
        "fix": "Use host_input_schema_version 4 with the complete cognitive "
               "cycle, or omit instruction_reconciliations.",
    },
    "instruction_reconciliations_must_be_a_list": {
        "means": "instruction_reconciliations was not a list.",
        "fix": "Use a list of explicit operator/connector reconciliation "
               "rows, or omit the field when no instruction is reconciled.",
    },
    "instruction_reconciliations_too_many": {
        "means": "The cycle attempted more than eight reconciliations.",
        "fix": "Submit only the bounded instructions actually reconciled from "
               "fresh saved-instruction, account-order and trade evidence.",
    },
    "instruction_reconciliation_invalid": {
        "means": "A reconciliation row had an invalid ID or field set.",
        "fix": "Use exactly reconciliation_id, recommendation_id, "
               "instruction_id, supersedes_reconciliation_id, "
               "operator_observation, account_orders_tool_call_id, "
               "account_trades_tool_call_id, and evidence.",
    },
    "instruction_reconciliation_duplicate_id": {
        "means": "A reconciliation ID was reused.",
        "fix": "Choose a new reconciliation_id. Corrections and later fills "
               "use a new record linked through supersedes_reconciliation_id.",
    },
    "instruction_reconciliation_proposal_missing": {
        "means": "No durable frozen proposal exists for the named "
                 "recommendation and instruction.",
        "fix": "Use recommendation_id and instruction_id from a persisted "
               "order_instruction_event. Do not reconstruct missing proposal "
               "terms from memory.",
    },
    "instruction_reconciliation_duplicate_proposal": {
        "means": "The same frozen proposal was reconciled twice in one cycle.",
        "fix": "Keep one reconciliation row per frozen proposal.",
    },
    "instruction_reconciliation_supersession_required": {
        "means": "A prior active reconciliation exists for this proposal.",
        "fix": "Set supersedes_reconciliation_id to the current active record "
               "when new order, trade, or correction evidence advances it.",
    },
    "instruction_reconciliation_supersession_invalid": {
        "means": "The row referenced an unknown, already superseded, or "
                 "different proposal reconciliation.",
        "fix": "Reference the current active reconciliation for the exact "
               "frozen proposal, or use null for its first reconciliation.",
    },
    "instruction_reconciliation_operator_invalid": {
        "means": "The explicit operator observation was incomplete or "
                 "temporally impossible.",
        "fix": "Provide observed_at, disposition accepted/rejected/deleted/"
               "unknown, app_saved_instruction_visible boolean or null, and "
               "the operator's explicit quote. The observation must follow "
               "instruction creation and not exceed cycle as_of.",
    },
    "instruction_reconciliation_tool_invalid": {
        "means": "Account order/trade evidence did not resolve to the required "
                 "current-cycle connector calls.",
        "fix": "Reference research-scope connector-response tool calls for "
               "get_account_orders and get_account_trades. Do not copy values "
               "into lifecycle evidence as a substitute.",
    },
    "instruction_reconciliation_evidence_invalid": {
        "means": "Reconciliation evidence was empty, malformed, duplicated, "
                 "or absent from the current cycle.",
        "fix": "Provide 1-8 unique current-cycle stage:<stage_id> or "
               "finding:<finding_id> references.",
    },
    "adversarial_disputes_require_schema_v3": {
        "means": "Adversarial disputes were supplied outside schema v3/v4.",
        "fix": "Use host_input_schema_version 4 or omit adversarial_disputes.",
    },
    "adversarial_disputes_must_be_a_list": {
        "means": "adversarial_disputes was not a list.",
        "fix": "Use a list of explicit disputes or omit the field.",
    },
    "adversarial_disputes_too_many": {
        "means": "The cycle supplied more than eight disputes.",
        "fix": "Keep only material disputes from this cycle.",
    },
    "adversarial_dispute_invalid": {
        "means": "A dispute had invalid identity, fields, positions, "
                 "resolution, or decision-change declaration.",
        "fix": "Provide the exact canonical dispute shape with bounded "
               "emerging/adversarial positions, governance resolution, and "
               "boolean final_decision_changed.",
    },
    "adversarial_dispute_duplicate_id": {
        "means": "A dispute ID was reused.",
        "fix": "Choose a new immutable dispute_id.",
    },
    "adversarial_dispute_opportunity_invalid": {
        "means": "The dispute referenced no known opportunity.",
        "fix": "Use a durable or same-cycle opportunity_id, or null.",
    },
    "adversarial_dispute_claims_invalid": {
        "means": "Disputed claims were absent, malformed, duplicated, or "
                 "unbounded.",
        "fix": "Provide 1-8 stable claim rows with claim_id, emerging_claim, "
               "and adversarial_claim.",
    },
    "adversarial_dispute_evidence_invalid": {
        "means": "Dispute evidence did not resolve inside the current cycle.",
        "fix": "Provide 1-8 unique stage: or finding: evidence refs.",
    },
    "tool_manifest_report_not_an_object": {
        "means": "The host capability inventory was not an object.",
        "fix": "Use the tool_manifest_report object shape published in "
               "delivery_probes.contracts.complete_tool_inventory.",
    },
    "tool_manifest_lookalike_key_unsupported": {
        "means": "A tool inventory used a lookalike top-level key, so the "
                 "runtime could not validate or persist it as evidence.",
        "fix": "Commit a NEW cycle file; never edit the committed input. Use "
               "exactly tool_manifest_report with observed_at, "
               "complete_for_current_session, connectors, "
               "manifest_discrepancies, and unreachable_manifest_connectors. "
               "Every connector action needs name, inputs, returns, and mode. "
               "Do not use tool_manifest, tool_inventory, or another alias.",
    },
    "tool_manifest_lookalike_contract_preview": {
        "means": "The lookalike inventory would still fail after a key-only "
                 "rename; the runtime checked its visible structure without "
                 "accepting or persisting it.",
        "fix": "Use tool_manifest_report and correct every item in the detail "
               "in the same new candidate. missing_fields names envelope "
               "fields to add. missing_known_ibkr lists the exact known IBKR "
               "actions absent from the candidate. Then compare against every "
               "other action visible in the current session because the "
               "known minimum is not a ceiling.",
    },
    "tool_manifest_observed_at_invalid": {
        "means": "The capability inventory had no timezone-qualified "
                 "observation time.",
        "fix": "Set observed_at to the time the host inspected its current "
               "tool list, including Z or an explicit UTC offset.",
    },
    "tool_manifest_not_declared_complete": {
        "means": "The host did not attest that it enumerated the entire "
                 "current-session tool list.",
        "fix": "Inspect every exposed connector action, then set "
               "complete_for_current_session to true. Do not infer tools from "
               "the repository manifest.",
    },
    "tool_manifest_connectors_must_be_nonempty_list": {
        "means": "The capability inventory did not contain connector objects.",
        "fix": "Use a non-empty connectors list with one object per connector.",
    },
    "tool_manifest_connector_not_object": {
        "means": "A connector inventory row was not an object.",
        "fix": "Each connector row needs name and a non-empty actions list.",
    },
    "tool_manifest_connector_name_required": {
        "means": "A connector inventory row did not name the connector.",
        "fix": "Use the exact connector name visible to the host.",
    },
    "tool_manifest_actions_must_be_nonempty_list": {
        "means": "A connector was summarized without enumerating its actions.",
        "fix": "List every invokable action currently exposed by that "
               "connector. Connector-level return summaries do not count.",
    },
    "tool_manifest_action_not_object": {
        "means": "A connector action row was not an object.",
        "fix": "Each action needs name, inputs, returns, and mode.",
    },
    "tool_manifest_actions_must_be_objects": {
        "means": "A connector listed action names as strings instead of "
                 "action contract objects.",
        "fix": "Replace every action string with an object such as "
               "{\"name\":\"get account positions\",\"inputs\":[],"
               "\"returns\":\"current account positions\","
               "\"mode\":\"read\"}. Use write_nontransmitting for IBKR "
               "instruction create/delete actions.",
    },
    "tool_manifest_action_name_required": {
        "means": "An action row did not carry the exact action name.",
        "fix": "Copy the action name as exposed by the connector.",
    },
    "tool_manifest_action_inputs_must_be_list": {
        "means": "An action did not list its input names.",
        "fix": "Use inputs as a list, including [] for an action with no "
               "arguments.",
    },
    "tool_manifest_action_returns_required": {
        "means": "An action did not describe what its result contains.",
        "fix": "Describe the returned evidence or mutation result in returns.",
    },
    "tool_manifest_action_mode_invalid": {
        "means": "An action used an unknown read/write classification.",
        "fix": "Use read, write_nontransmitting, write, or unknown. IBKR "
               "instruction create/delete is write_nontransmitting.",
    },
    "tool_manifest_known_action_mode_mismatch": {
        "means": "A known IBKR action was assigned the wrong read/write mode.",
        "fix": "Use the expected mode named in the detail. Account, market, "
               "search, and lookup actions are read; alerts/watchlists/"
               "feedback mutations are write; create/delete order instruction "
               "is write_nontransmitting.",
    },
    "tool_manifest_duplicate_action": {
        "means": "A connector listed the same action more than once.",
        "fix": "List each exact action once per connector.",
    },
    "tool_manifest_missing_known_ibkr_action": {
        "means": "The claimed complete inventory omitted an IBKR action "
                 "already confirmed by the operator.",
        "fix": "Add the action named in the detail, then continue enumerating "
               "all other actions visible in the current session.",
    },
    "tool_manifest_field_must_be_list": {
        "means": "A manifest discrepancy collection was not a list.",
        "fix": "Use lists for manifest_discrepancies and "
               "unreachable_manifest_connectors, including [] when empty.",
    },
    "staged_input_filename_invalid": {
        "means": "The staging filename is not safe for deterministic "
                 "promotion.",
        "fix": "Commit a new JSON file under host_staging/ using only letters, "
               "digits, dots, underscores, and hyphens in a name no longer "
               "than 125 characters.",
    },
    "staged_input_filename_collision": {
        "means": "A canonical host input already uses this filename.",
        "fix": "Commit a new file under host_staging/ with a unique filename. "
               "Never overwrite or replace the existing canonical file.",
    },
    "staged_cycle_id_collision": {
        "means": "A persisted receipt or another staged candidate already "
                 "uses this cycle_id.",
        "fix": "Commit a new staging file with a unique cycle_id. Never reuse "
               "an earlier cycle identity.",
    },
    "host_input_not_promoted": {
        "means": "A cycle was written directly to the canonical executor "
                 "directory without passing staged validation.",
        "fix": "Do not edit the direct file. Commit a new candidate under "
               "host_staging/ and wait for Host Input Validator to promote "
               "the exact bytes into host_input/.",
    },
    "market_sessions_not_an_object": {
        "means": "A full-cycle input omitted the market-session envelope.",
        "fix": "Include market_sessions with observed_at, EU and US market "
               "rows, and the mechanically derived overlap state.",
    },
    "market_sessions_observed_at_invalid": {
        "means": "Market-session evidence had no timezone-qualified timestamp.",
        "fix": "Set market_sessions.observed_at to the UTC observation time.",
    },
    "market_sessions_observed_at_mismatch": {
        "means": "Market-session evidence was not observed near the cycle's "
                 "portfolio snapshot.",
        "fix": "Fetch current session clocks in the same cycle as the IBKR "
               "snapshot; do not copy a prior cycle's market_sessions block.",
    },
    "market_sessions_markets_must_be_nonempty_list": {
        "means": "The market-session envelope had no market rows.",
        "fix": "Include one EU row and one US row.",
    },
    "market_session_not_object": {
        "means": "A market-session row was not an object.",
        "fix": "Each row needs region, venue, timezone, local_time, status, "
               "is_open, next_open, next_close, and evidence.",
    },
    "market_session_region_invalid": {
        "means": "A market-session row used an unknown region.",
        "fix": "Use EU or US. Put the actual exchange in venue.",
    },
    "market_session_region_duplicate": {
        "means": "The session envelope contained the same region twice.",
        "fix": "Include exactly one aggregate EU row and one aggregate US row.",
    },
    "market_session_region_missing": {
        "means": "The session envelope omitted EU or US.",
        "fix": "The detail names the missing region. Include both each cycle.",
    },
    "market_session_venue_required": {
        "means": "A session row did not identify the observed venue.",
        "fix": "Name the actual source venue, such as XETRA or NYSE.",
    },
    "market_session_timezone_invalid": {
        "means": "A session row used an invalid IANA timezone.",
        "fix": "Use an IANA name such as Europe/Berlin or America/New_York.",
    },
    "market_session_local_time_invalid": {
        "means": "A session row had no timezone-qualified local time.",
        "fix": "Include local_time with the venue's current UTC offset.",
    },
    "market_session_local_time_mismatch": {
        "means": "The venue local time did not represent the same instant as "
                 "market_sessions.observed_at.",
        "fix": "Convert observed_at through the declared IANA timezone. Do not "
               "manually copy a fixed offset across DST changes.",
    },
    "market_session_local_time_offset_mismatch": {
        "means": "The local time used an offset that disagreed with its IANA "
                 "timezone at the observed instant.",
        "fix": "Convert observed_at through the IANA timezone so DST selects "
               "the current offset automatically.",
    },
    "market_session_status_invalid": {
        "means": "A session row used an unknown market status.",
        "fix": "Use open, closed, pre_market, post_market, holiday, auction, "
               "or unknown.",
    },
    "market_session_is_open_not_boolean": {
        "means": "A session row did not state open/closed as a boolean.",
        "fix": "Set is_open to true or false from current calendar/clock "
               "evidence.",
    },
    "market_session_open_status_mismatch": {
        "means": "A row said status open while is_open was false.",
        "fix": "Make status and is_open agree with the source result.",
    },
    "market_session_closed_status_mismatch": {
        "means": "A row said closed or holiday while is_open was true.",
        "fix": "Make status and is_open agree with the source result.",
    },
    "market_session_next_open_invalid": {
        "means": "A session row omitted a timezone-qualified next open.",
        "fix": "Use the source calendar's next_open timestamp.",
    },
    "market_session_next_close_invalid": {
        "means": "A session row omitted a timezone-qualified next close.",
        "fix": "Use the source calendar's next_close timestamp.",
    },
    "market_session_next_open_not_future": {
        "means": "The reported next market open was not after observed_at.",
        "fix": "Use the source calendar's next future open, not the current or "
               "a historical session timestamp.",
    },
    "market_session_next_close_not_future": {
        "means": "The reported next market close was not after observed_at.",
        "fix": "Use the source calendar's next future close, not a historical "
               "session timestamp.",
    },
    "market_session_open_sequence_invalid": {
        "means": "An open market's next close did not precede its next open.",
        "fix": "For an open venue, report the current session's future close "
               "and the following session's future open.",
    },
    "market_session_closed_sequence_invalid": {
        "means": "A closed market's next open did not precede its next close.",
        "fix": "For a closed venue, report the upcoming session's open and "
               "that session's later close.",
    },
    "market_session_evidence_required": {
        "means": "A market status was asserted without calendar or clock "
                 "evidence.",
        "fix": "Include the exact tool and result used for the session state.",
    },
    "market_session_evidence_invalid": {
        "means": "A market-session evidence row lacked a tool or result.",
        "fix": "Each evidence row needs the exact source/tool and returned "
               "result.",
    },
    "market_session_overlap_invalid": {
        "means": "The combined EU/US overlap used an unknown value.",
        "fix": "Use both_open, eu_only, us_only, or none_open.",
    },
    "market_session_overlap_mismatch": {
        "means": "The combined overlap disagreed with the two is_open flags.",
        "fix": "Derive overlap mechanically from EU and US is_open values.",
    },
    "mutation_not_an_object": {
        "means": "'mutation' was present but was not an object.",
        "fix": "Send the proposal as an object with the MutationProposal "
               "fields, or omit 'mutation' entirely when proposing none.",
    },
    "mutation_missing_field": {
        "means": "A proposed mutation omitted a required field.",
        "fix": "A proposal needs all of: mutation_id, parent_version, "
               "mutation_type, targets, rationale, failure_ids, patch, "
               "expected_effect, counter_metrics, sample_requirement, "
               "evaluation_window, rollback_condition, created_at. A partial "
               "proposal cannot be evaluated or rolled back.",
    },
    "memory_distillation_not_an_object": {
        "means": "'memory_distillation' was present but was not an object.",
        "fix": "Use the object shape in MEMORY_DISTILLATION_CONTRACT.md, or "
               "omit the field when this cycle performs no distillation.",
    },
    "memory_distillation_not_performed_object": {
        "means": "The cycle used an object to say memory distillation was not "
                 "performed, which makes the runtime validate it as a real "
                 "but incomplete distillation envelope.",
        "fix": "Set memory_distillation to null when this cycle performs no "
               "distillation. Use an object only when every field in "
               "MEMORY_DISTILLATION_CONTRACT.md contains real evidence.",
    },
    "memory_distillation_missing": {
        "means": "A memory-distillation envelope omitted a required field.",
        "fix": "Include every output named by MEMORY_DISTILLATION_CONTRACT.md. "
               "The detail names the missing field.",
    },
    "memory_distillation_invalid": {
        "means": "A memory-distillation field had the wrong container type.",
        "fix": "Use lists for source/object/claim/proposal/retirement/blocker "
               "collections and ex_post_material. Use objects for bounds, "
               "contradiction groups, reconstruction_spec, and "
               "compression_metrics. The detail names the field.",
    },
    "memory_distillation_item_not_object": {
        "means": "A memory object or Active Brain proposal was not an object.",
        "fix": "Each memory_objects and active_brain_proposals list item must "
               "be a complete object using MEMORY_DISTILLATION_CONTRACT.md.",
    },
    "memory_distillation_duplicate_memory_id": {
        "means": "One distillation collection repeated a memory_id, so the "
                 "runtime could not identify one version unambiguously.",
        "fix": "Use each memory_id at most once in memory_objects and at most "
               "once in active_brain_proposals. A later revision belongs in "
               "a later cycle.",
    },
    "memory_retirement_not_object": {
        "means": "A retirement entry was not an object.",
        "fix": "Use an object with memory_id, status, reason, and source_ids "
               "for every retirement.",
    },
    "memory_retirement_missing": {
        "means": "A retirement entry omitted a required field.",
        "fix": "Include memory_id, status, reason, and source_ids. The detail "
               "names the missing field.",
    },
    "memory_retirement_invalid": {
        "means": "A retirement entry was ambiguous, duplicated, unsupported, "
                 "or exceeded the per-distillation safety limit.",
        "fix": "Use one row per known memory_id, status stale or archived, a "
               "non-empty reason, and a non-empty source_ids list.",
    },
    "lessons_must_be_a_list": {
        "means": "The optional lessons field was not a list.",
        "fix": "Use lessons as a list of lesson objects, or [] when this cycle "
               "produced no durable lesson.",
    },
    "lesson_not_an_object": {
        "means": "A lessons entry was not an object.",
        "fix": "Each lesson must be an object with lesson, evidence, and "
               "falsified_if fields.",
    },
    "lesson_missing_lesson": {
        "means": "A lesson row did not state the durable conclusion.",
        "fix": "Add a non-empty lesson field, or remove the row if no durable "
               "lesson was learned.",
    },
    "lesson_missing_evidence": {
        "means": "A lesson row stated a conclusion without naming the evidence "
                 "that supports it.",
        "fix": "Add a non-empty evidence field citing the current-cycle facts "
               "that produced the lesson. Do not use the rationale itself as "
               "evidence.",
    },
    "lesson_missing_falsified_if": {
        "means": "A lesson row did not say what future evidence would overturn "
                 "it.",
        "fix": "Add a non-empty falsified_if condition so the lesson can be "
               "revised instead of becoming permanent dogma.",
    },
    "retry_target_unsatisfied": {
        "means": "A correction retry left a previously refused target in the "
                 "same invalid structural state.",
        "fix": "The detail names the exact JSON pointer and required state. "
               "Correct that target before changing unrelated evidence or "
               "committing another retry.",
    },
    "memory_object_invalid": {
        "means": "A memory object or Active Brain proposal was structurally "
                 "invalid.",
        "fix": "The detail names the collection, memory id, and exact defect. "
               "Every object needs memory_id, layer, status, as_of, claim, "
               "source_ids, evidence_status, confidence, "
               "reconstruction_status, and claim_ids. Use a documented "
               "status such as validated or experimental for "
               "research_memory, and active for Active Brain. "
               "reconstruction_status is separately one of not_run, passed, "
               "failed, or blocked; validated is a lifecycle status, not a "
               "reconstruction result.",
    },
    "memory_contradiction_group_invalid": {
        "means": "A contradiction group did not map to a list of claim ids.",
        "fix": "Use an object such as "
               "{\"group-id\": [\"claim-a\", \"claim-b\"]}.",
    },
    "memory_reconstruction_missing": {
        "means": "reconstruction_spec omitted a required field.",
        "fix": "Include required_claim_ids, distilled_claim_ids, "
               "source_claim_ids, and distilled_contradiction_groups.",
    },
    "memory_reconstruction_invalid": {
        "means": "A reconstruction field had the wrong container type.",
        "fix": "Use lists for the three claim-id fields and an object mapping "
               "group ids to claim-id lists for "
               "distilled_contradiction_groups.",
    },
    "memory_compression_missing": {
        "means": "compression_metrics omitted a count needed to measure the "
                 "distillation.",
        "fix": "Include positive numeric raw_units and distilled_units.",
    },
    "memory_compression_invalid": {
        "means": "A compression count was not a positive number.",
        "fix": "Set raw_units and distilled_units to positive numeric counts "
               "for the source material and resulting distillation.",
    },
    "mechanical_input_invalid": {
        "means": "A deterministic-analysis input used the wrong top-level "
                 "container type.",
        "fix": "Use objects for portfolio_mechanics/historical_backfill and "
               "lists for expressions/covered_call_candidates. The detail "
               "names the field.",
    },
    "invalid_host_input_schema_version": {
        "means": "'host_input_schema_version' was not an integer.",
        "fix": "Use 3 for new staged cycles. Version 2 remains supported only "
               "for replaying historical full-cycle inputs, and the field may "
               "be omitted only for the legacy three-stage shape.",
    },
    "unsupported_host_input_schema_version": {
        "means": "The input named a schema version this executor does not "
                 "implement.",
        "fix": "Use host_input_schema_version 4 for a new staged cycle. "
               "Versions 2 and 3 remain replayable by the executor.",
    },
    "host_input_schema_version_required": {
        "means": "A newly staged cycle omitted the current schema version "
                 "and could bypass the full cognitive contract.",
        "fix": "Set host_input_schema_version to 4 and copy the complete "
               "current structure from the canonical schema example.",
    },
    "staged_host_input_schema_version_required": {
        "means": "A new staged cycle used a replay-compatible historical "
                 "schema rather than the current canonical schema.",
        "fix": "Set host_input_schema_version to 4 and add the complete "
               "learning_stage_dispositions and tool-capture contracts. "
               "Versions 2 and 3 remain historical replay formats.",
    },
    "learning_dispositions_required": {
        "means": "A schema-v3/v4 cycle omitted the required learning-stage "
                 "disposition list.",
        "fix": "Add learning_stage_dispositions with exactly one row for "
               "learning_audit, meta_research, and self_improvement.",
    },
    "learning_disposition_invalid": {
        "means": "A learning disposition had an unknown or duplicate stage, "
                 "an unsupported value, an empty or overlong rationale, or "
                 "an unexpected field.",
        "fix": "Use exactly one row per required learning stage. Set "
               "disposition to artifact or no_change and give a non-empty "
               "rationale of at most 600 characters.",
    },
    "learning_disposition_missing_stage": {
        "means": "The schema-v3/v4 cycle omitted one required learning-stage "
                 "disposition.",
        "fix": "Add the named stage using exactly one learning_audit, one "
               "meta_research, and one self_improvement row.",
    },
    "learning_disposition_evidence_invalid": {
        "means": "A disposition did not cite a valid current-cycle stage or "
                 "finding reference.",
        "fix": "Provide a non-empty bounded evidence list using only "
               "stage:<stage_id> or finding:<finding_id> values that exist "
               "in this submitted cycle.",
    },
    "learning_disposition_artifact_invalid": {
        "means": "An artifact disposition did not identify a declared "
                 "same-cycle durable artifact, or a no-change row supplied "
                 "artifact references.",
        "fix": "For artifact, provide non-empty artifact_refs using lesson:"
               "<lesson_id>, memory-distillation:<distillation_id>, "
               "goal:<goal_id>, goal:<goal_id>:progress, "
               "goal:<goal_id>:closed, or mutation:<mutation_id>. For "
               "no_change, omit artifact_refs entirely.",
    },
    "learning_artifact_id_reused": {
        "means": "A schema-v3/v4 artifact declaration reused an identifier "
                 "already present in the append-only journal.",
        "fix": "Choose a new lesson, memory-distillation, goal, or mutation "
               "identifier. Never silently attach a new disposition to an "
               "artifact produced by an earlier cycle.",
    },
    "research_agenda_invalid": {
        "means": "The research director did not provide a complete data-led "
                 "agenda with fresh drivers, selected work, and a rejected "
                 "alternative.",
        "fix": "Populate research_director.output.research_agenda with "
               "drivers, candidates, one rejected alternative, and a "
               "selection_rationale. Selected candidate IDs must be real "
               "specialist stage IDs.",
    },
    "research_binding_invalid": {
        "means": "A research row was not traceable to a specialist selected "
                 "by the research director's agenda.",
        "fix": "Set research[].specialist_stage_id to the selected agenda "
               "candidate_id and matching cognitive specialist stage_id.",
    },
    "research_allocation_required": {
        "means": "A newly staged cycle omitted the Research Director's "
                 "session-aware allocation plan.",
        "fix": "Add research_agenda.allocation_plan, "
               "allocation_variance, and allocation_factors for every "
               "candidate. The plan must expose ceilings for new opportunity, "
               "existing opportunity, portfolio risk, and follow-up work.",
    },
    "research_allocation_plan_invalid": {
        "means": "The host-authored research allocation plan was incomplete, "
                 "inconsistent with current market sessions, or larger than "
                 "the Scout specialist budget.",
        "fix": "Provide non-negative ceilings for new_opportunity, "
               "existing_opportunity, portfolio_risk, and follow_up. Copy "
               "market_sessions.overlap into market_session_context and "
               "explain why the current session state supports this mix. "
               "The four ceilings may leave capacity unused but must not "
               "exceed budget.specialist_investigations.",
    },
    "research_allocation_candidate_invalid": {
        "means": "An agenda candidate lacked complete allocation reasoning or "
                 "a selected candidate lacked a unique evidence-linked "
                 "accounting reference.",
        "fix": "Give every candidate allocation_factors with novelty, "
               "portfolio_impact, missing_information, and "
               "expected_information_gain. Resolve portfolio_risk_ref to "
               "portfolio:account, portfolio:cash, portfolio:positions, "
               "portfolio:open_orders, portfolio:instructions, or an "
               "observed position:<symbol-or-contract-id>. Resolve "
               "follow_up_ref to instruction:<current-id>, "
               "opportunity:<known-id>, or candidate:<prior-id>. Use "
               "opportunity_id for existing ledger work or "
               "scout_candidate_id for selected new work. Selected "
               "references must be unique. A staged follow-up timestamp must "
               "be later than the durable record it references.",
    },
    "research_allocation_variance_invalid": {
        "means": "Mechanically derived selected work exceeded an allocation "
                 "bucket without an exact variance disclosure, or variance "
                 "was declared when no bucket exceeded its ceiling.",
        "fix": "Compare the selected allocation usage with allocation_plan. "
               "If a bucket exceeded its ceiling, set allocation_variance to "
               "exactly exceeded plus a rationale. Otherwise use null.",
    },
    "market_scout_required": {
        "means": "A newly staged schema-v4 cycle omitted the Market Scout "
                 "core stage.",
        "fix": "Add a completed market_scout stage after portfolio. It must "
               "depend only on portfolio and research_director must depend "
               "only on market_scout.",
    },
    "market_scout_stage_invalid": {
        "means": "The Market Scout stage had the wrong phase, dependency, "
                 "required flag, status, or Research Director edge.",
        "fix": "Use phase discovery, depends_on [portfolio], required true, "
               "and status completed. Set research_director.depends_on to "
               "[market_scout].",
    },
    "market_scout_report_invalid": {
        "means": "Market Scout did not provide the canonical discovery "
                 "report shape.",
        "fix": "Set output.market_scout_report to an object containing exactly "
               "scope, budget, tool_calls, candidates, and budget_variance. "
               "A zero-candidate report is valid when its scan is evidenced.",
    },
    "market_scout_budget_invalid": {
        "means": "The host-authored research budget was incomplete or used "
                 "invalid values.",
        "fix": "Provide non-negative integer budgets for specialist "
               "investigations, external searches, deep dives, and "
               "opportunity updates, plus a non-empty rationale.",
    },
    "market_scout_tool_calls_invalid": {
        "means": "The Market Scout report contained no concrete discovery "
                 "tool call.",
        "fix": "Record at least one connector lookup, external search, or "
               "deep dive with a stable tool_call_id and canonical "
               "provenance.",
    },
    "market_scout_tool_call_invalid": {
        "means": "A Market Scout tool call had an invalid ID, kind, field "
                 "set, tool name, call, or result.",
        "fix": "Use exactly tool_call_id, kind, tool, call, result, and "
               "provenance. Kinds are connector_lookup, external_search, "
               "or deep_dive.",
    },
    "market_scout_tool_provenance_invalid": {
        "means": "A Market Scout result did not satisfy the same provenance "
                 "contract as research tool calls.",
        "fix": "Provide result_origin, timezone-qualified observed_at, stable "
               "source_refs, and the canonical result hash required by the "
               "existing tool-provenance validator.",
    },
    "market_scout_candidates_invalid": {
        "means": "Market Scout candidates was not a list.",
        "fix": "Use candidates as a list. Use [] with an evidenced scope and "
               "rationale when the scan found no worthwhile candidate.",
    },
    "market_scout_candidate_invalid": {
        "means": "A scout candidate had an invalid ID, identity, duplicate "
                 "identity, trigger, rationale, or field set.",
        "fix": "Use a unique stable candidate_id and the exact five-field "
               "opportunity identity: instrument, instrument_type, "
               "strategy_family, direction, and thesis_key.",
    },
    "market_scout_candidate_evidence_invalid": {
        "means": "A scout candidate did not cite concrete Market Scout tool "
                 "calls from the same report.",
        "fix": "Provide 1-12 unique evidence_tool_call_ids that resolve to "
               "tool_call_id values in market_scout_report.tool_calls.",
    },
    "market_scout_candidate_rediscovery_invalid": {
        "means": "A scout candidate's exact identity matches an unpromoted "
                 "candidate from a prior receipted cycle, but the required "
                 "rediscovery_of reference was missing, not applicable to a "
                 "genuinely new identity, or did not resolve to that exact "
                 "prior occurrence.",
        "fix": "Read FEEDBACK.json.candidate_registry. If this exact "
               "identity appears there, add "
               "rediscovery_of: {cycle_id, candidate_id} naming the "
               "last_seen_cycle_id and last_seen_candidate_id shown for it. "
               "Omit rediscovery_of entirely for a candidate that is not in "
               "the registry.",
    },
    "market_scout_budget_variance_invalid": {
        "means": "Mechanically derived research usage exceeded the declared "
                 "budget without an exact variance disclosure, or variance "
                 "was declared when no category exceeded budget.",
        "fix": "Compare the declared budget with FEEDBACK.json.market_scout."
               "usage. If any category exceeded budget, list exactly those "
               "categories and explain why. Otherwise use null.",
    },
    "market_scout_agenda_link_invalid": {
        "means": "A Research Director agenda row referenced an unknown scout "
                 "candidate.",
        "fix": "Omit scout_candidate_id for unrelated work, or set it to a "
               "candidate_id from this cycle's Market Scout report. Keep "
               "specialist candidate_id separate, and keep instrument and "
               "strategy_family consistent with the linked scout identity.",
    },
    "opportunity_updates_require_schema_v3": {
        "means": "Opportunity lifecycle events were supplied outside the "
                 "structured schema-v3/v4 full-cycle contract.",
        "fix": "Use host_input_schema_version 4 with the complete cognitive "
               "stage contract, or omit opportunity_updates.",
    },
    "opportunity_updates_must_be_a_list": {
        "means": "The opportunity update collection was not a list.",
        "fix": "Use opportunity_updates as a list with one object per "
               "append-only lifecycle event, or [] when nothing changed.",
    },
    "opportunity_update_not_object": {
        "means": "An opportunity update was not an object.",
        "fix": "Each update must be an object containing event_id, "
               "opportunity_id, identity, transition, thesis, rationale, "
               "and current-cycle evidence.",
    },
    "opportunity_update_field_required": {
        "means": "An opportunity event omitted a required identity or "
                 "explanation field.",
        "fix": "Populate the field named in the detail with a non-empty value. "
               "Event and opportunity IDs must be stable identifiers.",
    },
    "opportunity_update_identity_invalid": {
        "means": "An opportunity or event identifier was malformed, or the "
                 "immutable identity object was incomplete or unsupported.",
        "fix": "Use safe stable IDs and an identity containing exactly "
               "instrument, instrument_type, strategy_family, direction, "
               "and thesis_key. Direction must be long, short, "
               "relative_value, hedge, or mixed.",
    },
    "opportunity_update_text_too_long": {
        "means": "An opportunity thesis or rationale exceeded the bounded "
                 "ledger contract.",
        "fix": "Condense the named field to at most 600 characters while "
               "preserving the decision-relevant claim.",
    },
    "opportunity_research_state_invalid": {
        "means": "An opportunity update omitted or malformed its durable "
                 "missing-information, uncertainty, trigger, or next-question "
                 "state.",
        "fix": "Provide research_state with bounded stable-ID rows for "
               "missing_information, uncertainties, and review_triggers. "
               "Keep resolved or retired rows, preserve their defining text, "
               "and point next_question_id to an open missing-information "
               "row whenever the opportunity still requires research.",
    },
    "opportunity_revisit_invalid": {
        "means": "An existing opportunity was researched without a valid "
                 "pre-committed target, trigger, expected information gain, "
                 "or evidence-backed result.",
        "fix": "Target the prior ledger next_question_id and an active prior "
               "review trigger. If fresh evidence genuinely retargets the "
               "work, explain retarget_reason. Use the one-time legacy "
               "initialization flag only when the prior event predates "
               "research_state.",
    },
    "opportunity_revisit_evidence_invalid": {
        "means": "A revisit result did not cite valid current-cycle evidence.",
        "fix": "Provide 1-8 unique stage:<stage_id> or finding:<finding_id> "
               "references from this cycle that support the revisit result.",
    },
    "opportunity_revisit_required": {
        "means": "A same-state opportunity event was submitted without a "
                 "research revisit record.",
        "fix": "Add the revisit object with the prior committed question, "
               "activating trigger, expected information gain, result, and "
               "current-cycle evidence, or omit the unchanged event.",
    },
    "opportunity_revisit_repeated_no_information": {
        "means": "The host repeated the same trigger and missing-information "
                 "target immediately after that pair produced no new "
                 "information.",
        "fix": "Do not spend another specialist pass on the unchanged pair. "
               "Advance next_question_id, wait for a different active trigger, "
               "or select another opportunity.",
    },
    "opportunity_agenda_link_invalid": {
        "means": "Selected Research Director work ambiguously rediscovered or "
                 "misidentified an existing durable opportunity.",
        "fix": "Set opportunity_id when revisiting the ledger item. If a Scout "
               "candidate is genuinely distinct from same-instrument prior "
               "work, list every matching ID in "
               "distinct_from_opportunity_ids and explain "
               "distinctness_reason. For a linked ledger item, copy "
               "instrument and strategy_family from its identity.",
    },
    "opportunity_agenda_revisit_required": {
        "means": "A selected existing opportunity consumed specialist work "
                 "without a corresponding append-only revisit event.",
        "fix": "Add one opportunity_updates event for the named opportunity "
               "with research_state and a valid revisit result, or do not "
               "select that opportunity in this cycle.",
    },
    "forecast_registrations_require_schema_v3": {
        "means": "Forecast registrations were supplied outside the canonical "
                 "schema-v3/v4 full-cycle contract.",
        "fix": "Use host_input_schema_version 4 with the complete cognitive "
               "cycle, or omit forecast_registrations.",
    },
    "forecast_registrations_must_be_a_list": {
        "means": "forecast_registrations was not a list.",
        "fix": "Use a list of immutable forecast objects, or omit the field "
               "when no forecast is registered this cycle.",
    },
    "forecast_registrations_too_many": {
        "means": "The cycle attempted to register more than eight forecasts.",
        "fix": "Keep only the bounded forecasts actually selected by the host "
               "for this cycle. Do not register predictions for volume.",
    },
    "forecast_registration_invalid": {
        "means": "A forecast row had an invalid ID, field set, or required "
                 "bounded text.",
        "fix": "Use the canonical forecast shape exactly. Keep forecast_id "
               "unique and provide thesis, portfolio_context, "
               "invalidation_condition, metric, horizon, expectation, "
               "confidence, optional benchmark/entry context, risk "
               "assumptions, and current-cycle evidence.",
    },
    "forecast_registration_duplicate_id": {
        "means": "A forecast ID was reused in this cycle or already exists in "
                 "the append-only journal.",
        "fix": "Choose a new forecast_id. Never rewrite an earlier forecast; "
               "use supersedes_forecast_id for a visible revision.",
    },
    "forecast_registration_duplicate_key": {
        "means": "The cycle registered the same opportunity, metric source, "
                 "and target horizon more than once.",
        "fix": "Keep one forecast for that measurable event in this cycle.",
    },
    "forecast_opportunity_invalid": {
        "means": "The forecast did not link to a known durable or same-cycle "
                 "opportunity.",
        "fix": "Set opportunity_id to an opportunity in "
               "FEEDBACK.json.opportunity_ledger or one created by this "
               "cycle's opportunity_updates.",
    },
    "forecast_source_invalid": {
        "means": "A metric, benchmark, or entry source was incomplete.",
        "fix": "Provide tool, field, instrument_ref, and stable_ref so the "
               "later outcome must observe the same numeric source and field.",
    },
    "forecast_metric_invalid": {
        "means": "A numeric metric or benchmark had an invalid name, unit, "
                 "baseline, source, or observation timestamp.",
        "fix": "Use a finite baseline observed no later than this cycle. Units "
               "are currency, percent, ratio, count, or basis_points.",
    },
    "forecast_expectation_invalid": {
        "means": "The forecast expectation was not deterministically "
                 "resolvable.",
        "fix": "For direction use up or down with null bounds; ties resolve "
               "false. Express a flat view as a range. For range use ordered "
               "inclusive numeric bounds and direction null.",
    },
    "forecast_horizon_invalid": {
        "means": "The forecast horizon was malformed or did not end after "
                 "registration.",
        "fix": "Provide a bounded label and timezone-qualified target_at later "
               "than this cycle's effective snapshot time.",
    },
    "forecast_confidence_invalid": {
        "means": "confidence_probability was not a finite number from 0 to 1.",
        "fix": "Provide the host's ex-ante probability from 0 through 1. It is "
               "stored for later calibration, not treated as precision proof.",
    },
    "forecast_entry_context_invalid": {
        "means": "Optional discovery, recommendation, or hypothetical entry "
                 "context was incomplete, future-dated, or non-positive.",
        "fix": "Use null or provide kind, bounded expression, positive price, "
               "timezone-qualified observed_at, and a complete source. This "
               "context never creates an order.",
    },
    "forecast_risk_assumptions_invalid": {
        "means": "Risk assumptions were absent, malformed, or unbounded.",
        "fix": "Provide 1-8 concise assumptions that frame the forecast but do "
               "not change its deterministic resolution rule.",
    },
    "forecast_evidence_invalid": {
        "means": "Forecast evidence was empty, malformed, duplicated, or did "
                 "not resolve inside the current cycle.",
        "fix": "Provide 1-8 unique current-cycle stage:<stage_id> or "
               "finding:<finding_id> references.",
    },
    "forecast_supersession_required": {
        "means": "An open forecast already exists for the same opportunity, "
                 "metric source, and target horizon.",
        "fix": "Do not silently re-forecast. Either omit the new row or set "
               "supersedes_forecast_id to the exact currently open forecast.",
    },
    "forecast_supersession_invalid": {
        "means": "The forecast tried to supersede an unknown, already "
                 "superseded, or different measurable event.",
        "fix": "Reference the currently open forecast with the same "
               "opportunity, metric source, and target_at.",
    },
    "forecast_outcome_lookalike_key_unsupported": {
        "means": "A forecast-outcome-like top-level key would be ignored.",
        "fix": "Use exactly forecast_outcomes as a list. Singular or renamed "
               "keys are refused rather than silently treated as success.",
    },
    "forecast_outcomes_require_schema_v3": {
        "means": "Forecast outcomes were supplied outside the canonical "
                 "schema-v3/v4 full-cycle contract.",
        "fix": "Use host_input_schema_version 4 with the complete cognitive "
               "cycle, or omit forecast_outcomes.",
    },
    "forecast_outcomes_must_be_a_list": {
        "means": "forecast_outcomes was not a list.",
        "fix": "Use a list of forecast outcome observations, or omit the field "
               "when no matured forecast is measured this cycle.",
    },
    "forecast_outcomes_too_many": {
        "means": "The cycle attempted to measure more than eight forecasts.",
        "fix": "Submit only the bounded matured forecasts actually observed "
               "from their frozen sources in this cycle.",
    },
    "forecast_outcome_invalid": {
        "means": "A forecast outcome row had an invalid field set or "
                 "invalidation reason.",
        "fix": "Use exactly forecast_id, tool_call_id, invalidation_reason, "
               "and evidence. The runtime extracts the value and time; do not "
               "supply a grade or observed_value.",
    },
    "forecast_outcome_unknown_forecast": {
        "means": "The outcome referenced no persisted immutable forecast.",
        "fix": "Use a forecast_id from FEEDBACK.json.forecast_ledger.",
    },
    "forecast_outcome_duplicate_forecast": {
        "means": "The same forecast appeared more than once in one outcome "
                 "submission.",
        "fix": "Keep exactly one row per forecast_id.",
    },
    "forecast_outcome_already_measured": {
        "means": "A terminal outcome already exists for this forecast.",
        "fix": "Do not submit another outcome. Terminal measurement is "
               "append-only and keyed by forecast_id.",
    },
    "forecast_outcome_tool_call_invalid": {
        "means": "The outcome did not bind to a current-cycle connector call "
                 "matching the forecast's frozen tool and stable reference.",
        "fix": "Use the current research tool_call_id whose provenance has "
               "result_origin connector_response, the same tool, and the "
               "forecast metric stable_ref.",
    },
    "forecast_outcome_time_invalid": {
        "means": "The connector observation was before target_at, after the "
                 "frozen observation window, or after the cycle snapshot.",
        "fix": "Observe the frozen source only inside target_at through "
               "target_at plus observation_window_seconds. Do not wait for a "
               "later favorable value.",
    },
    "forecast_outcome_value_invalid": {
        "means": "The frozen metric field could not be extracted as a finite "
                 "number from the connector result.",
        "fix": "Use a connector call whose raw result contains the exact "
               "metric.source.field frozen in the forecast.",
    },
    "forecast_outcome_evidence_invalid": {
        "means": "Outcome evidence was empty, malformed, duplicated, or not "
                 "present in the current cycle.",
        "fix": "Provide 1-8 unique current-cycle stage:<stage_id> or "
               "finding:<finding_id> references.",
    },
    "opportunity_event_id_duplicate": {
        "means": "The candidate reused an opportunity event ID within the "
                 "same input.",
        "fix": "Give each lifecycle event a unique event_id. Multiple ordered "
               "events for one opportunity may share opportunity_id, not "
               "event_id.",
    },
    "opportunity_event_id_reused": {
        "means": "The event ID already exists in the immutable journal.",
        "fix": "Create a new event_id for the new transition. Never rewrite "
               "or reuse an already persisted opportunity event.",
    },
    "opportunity_evidence_invalid": {
        "means": "Opportunity evidence was empty, malformed, duplicated, or "
                 "not present in the current cycle.",
        "fix": "Provide 1-8 unique current-cycle references using "
               "stage:<stage_id> or finding:<finding_id> that exactly match "
               "this candidate.",
    },
    "opportunity_initial_state_invalid": {
        "means": "A new opportunity did not begin with the canonical initial "
                 "transition.",
        "fix": "For a previously unseen opportunity_id, set from_state to null "
               "and to_state to new.",
    },
    "opportunity_transition_invalid": {
        "means": "The requested opportunity lifecycle transition is not legal.",
        "fix": "Use the current state from FEEDBACK.json.opportunity_ledger "
               "and follow the documented transition graph. Reopening a "
               "terminal opportunity returns it to new with explicit lineage.",
    },
    "opportunity_duplicate_identity": {
        "means": "A different opportunity_id already owns the same normalized "
                 "instrument, type, family, direction, and thesis key.",
        "fix": "Update the existing opportunity_id instead of creating a "
               "duplicate. Add new evidence through another immutable event.",
    },
    "opportunity_state_mismatch": {
        "means": "The submitted from_state disagrees with the journal's latest "
                 "state for this opportunity.",
        "fix": "Read FEEDBACK.json.opportunity_ledger and use its current state "
               "as from_state before choosing the next transition.",
    },
    "opportunity_identity_changed": {
        "means": "An update attempted to change the immutable identity of an "
                 "existing opportunity.",
        "fix": "Repeat the existing normalized identity exactly. If the thesis "
               "is genuinely different, create a new opportunity and let the "
               "soft-collision feedback expose the relationship.",
    },
    "opportunity_reopen_invalid": {
        "means": "A terminal opportunity was reopened without exact prior "
                 "event lineage, or reopen metadata appeared on a normal "
                 "transition.",
        "fix": "Only rejected or invalidated opportunities may reopen to new. "
               "Set reopens_event_id to the latest full opportunity-event "
               "record ID shown in feedback.",
    },
    "opportunity_update_reconciliation_failed": {
        "means": "An opportunity event no longer matched journal state when "
                 "the executor attempted to persist it.",
        "fix": "Treat this as a concurrency or recovery conflict. Read the "
               "latest opportunity ledger, preserve the candidate, and submit "
               "a new event from the actual current state.",
    },
    "opportunity_event_payload_mismatch": {
        "means": "A retry found the same opportunity event ID with different "
                 "content or causal parents.",
        "fix": "Do not rewrite an event. Restore the exact accepted bytes for "
               "recovery or use a new event_id for a genuinely new update.",
    },
    "opportunity_backfill_chain_invalid": {
        "means": "Opportunity recovery could not safely order the audit chain.",
        "fix": "Preserve the journal and repair its integrity before "
               "backfilling opportunity events.",
    },
    "goal_mode_invalid": {
        "means": "A goal observation could not be routed to a supported "
                 "journal-backed lifecycle operation.",
        "fix": "Send each goal_observations entry as an object with mode "
               "create, progress, or close. Do not omit mode, send a blank "
               "mode, use legacy grade, or invent another mode.",
    },
    "goal_creation_invalid": {
        "means": "A requested open goal was not a unique, current-cycle, "
                 "machine-gradable controllable goal.",
        "fix": "Send at most one goal_observations entry with mode create. "
               "Use finite numeric baseline, partial_target, and "
               "success_target in strict directional order, created_at equal "
               "to this cycle's as_of, a future deadline, and caused_by "
               "evidence from this cycle. Never reuse a prior goal_id.",
    },
    "goal_progress_invalid": {
        "means": "A progress observation did not describe the current open "
                 "goal with finite, current-cycle evidence.",
        "fix": "Send at most one progress row per open goal. Supply goal_id, "
               "observed_at equal to this cycle's as_of, a finite numeric "
               "observed_value, assessment, evidence linked to this cycle, "
               "and caused_by. Do not send a goal snapshot or any terminal "
               "status, grade, result, outcome, verdict, score, or final.",
    },
    "goal_close_invalid": {
        "means": "A terminal observation could not safely close the current "
                 "journal-backed goal.",
        "fix": "Use mode close once for the open goal. For measurement, wait "
               "until its deadline and provide a finite observed_value. For "
               "invalidation, omit observed_value and explain why the premise "
               "became ungradable. Include current-cycle evidence and complete "
               "causal analysis. Never send a goal snapshot or terminal "
               "status; the runtime computes it.",
    },
    "cognitive_stages_must_be_nonempty_list": {
        "means": "A v2 full-cycle input did not include cognitive_stages.",
        "fix": "Send the complete stage list described in "
               "prompts/host-standing-schedule.md. memory_distillation is an "
               "additional top-level field in that same v2 cycle, not a "
               "replacement for cognitive_stages.",
    },
    "cognitive_stage_not_an_object": {
        "means": "A cognitive stage row was not an object.",
        "fix": "Each stage needs stage_id, phase, depends_on, required, "
               "status, tools_used, and output.",
    },
    "cognitive_stage_missing": {
        "means": "A cognitive stage omitted a required field.",
        "fix": "The detail names the stage/field. Include stage_id, phase, "
               "depends_on, required, status, tools_used, and output.",
    },
    "cognitive_stage_output_not_object": {
        "means": "A cognitive stage output was not an object.",
        "fix": "Store the actual structured output for that stage.",
    },
    "cognitive_stage_output_missing": {
        "means": "A completed cognitive stage supplied a placeholder rather "
                 "than the evidence needed to audit it.",
        "fix": "Every stage output includes observations, evidence_status, "
               "blockers, confidence, and next_actions. The detail names the "
               "stage and all missing fields.",
    },
    "cognitive_stage_output_not_list": {
        "means": "A stage output collection was not a list.",
        "fix": "observations, blockers, and next_actions are lists, including "
               "[] when genuinely empty.",
    },
    "cognitive_stage_confidence_invalid": {
        "means": "Stage confidence was not a number from 0 through 1.",
        "fix": "Use a numeric confidence in [0,1], or null when the stage "
               "cannot support one.",
    },
    "cognitive_stage_evidence_status_invalid": {
        "means": "A stage used an evidence-quality label outside the v2 "
                 "contract.",
        "fix": "Use verified, cross_checked, partial, unknown, or "
               "not_applicable. Evidence quality is separate from whether "
               "the stage itself completed.",
    },
    "cognitive_stage_blockers_empty": {
        "means": "A blocked or failed stage did not say what blocked it.",
        "fix": "Keep the stage in the plan and put the exact reasons in "
               "output.blockers.",
    },
    "cognitive_stage_tools_not_list": {
        "means": "A cognitive stage tools_used value was not a list.",
        "fix": "Use the actual list of tools the stage called, including [] "
               "when it used none.",
    },
    "cognitive_stage_dependencies_not_list": {
        "means": "A cognitive stage depends_on value was not a list.",
        "fix": "List the stage ids whose committed outputs this stage used.",
    },
    "cognitive_stage_status_invalid": {
        "means": "A cognitive stage used an unknown status.",
        "fix": "Use completed, blocked, skipped, or failed.",
    },
    "duplicate_cognitive_stage_id": {
        "means": "Two cognitive stage rows used the same stage_id.",
        "fix": "Every stage id must be unique in one cycle.",
    },
    "full_cycle_stage_missing": {
        "means": "A v2 cycle omitted a core cognitive stage.",
        "fix": "The detail names it. Full cycles include portfolio, research "
               "director, memory retrieval, arbitration, portfolio fit, "
               "counterfactual, adversarial, governance, decision, learning, "
               "meta-research, and self-improvement.",
    },
    "full_cycle_stage_not_required": {
        "means": "A core full-cycle stage was marked optional.",
        "fix": "Core stages are required. If one cannot run, include it with "
               "status blocked and the exact blocker.",
    },
    "full_cycle_specialist_not_required": {
        "means": "A specialist selected by the Research Director was marked "
                 "optional in a full cycle.",
        "fix": "Mark every selected specialist required. Select only passes "
               "that fit the current host wake and record a real blocker if "
               "a selected pass cannot execute.",
    },
    "full_cycle_specialist_not_isolated": {
        "means": "A selected specialist depended on another specialist or "
                 "on the wrong stage.",
        "fix": "Make every selected specialist an isolated sibling with "
               "depends_on exactly [\"memory_retrieval\"]. Do not chain "
               "specialists; combine their conclusions only in "
               "evidence_arbitration.",
    },
    "evidence_arbitration_dependencies_mismatch": {
        "means": "Evidence arbitration did not depend on every selected "
                 "specialist, or depended on the wrong stages.",
        "fix": "Set evidence_arbitration.depends_on to the complete list of "
               "selected specialist stage ids. If there are no specialists, "
               "depend on memory_retrieval.",
    },
    "cognitive_stage_completed_after_noncompleted_dependency": {
        "means": "A stage claimed completion even though one of its declared "
                 "dependencies was blocked, failed, or skipped.",
        "fix": "Correct the dependency graph or the status. If the upstream "
               "pass ran but found incomplete evidence, mark that pass "
               "completed with evidence_status partial and list the evidence "
               "gaps in output.blockers. Use blocked only when the pass "
               "itself could not execute.",
    },
    "cognitive_plan": {
        "means": "The committed stage dependency graph is invalid.",
        "fix": "The detail names a missing, duplicate, self, or cyclic "
               "dependency.",
    },
    "decision_stage_disagrees_with_decision": {
        "means": "The decision stage output and top-level decision name "
                 "different statuses.",
        "fix": "Make them identical. The executor cannot choose which claim "
               "is the real one.",
    },
    "decision_stage_missing_rationale": {
        "means": "The decision stage did not carry its own rationale.",
        "fix": "Repeat the top-level decision rationale in the committed "
               "decision-stage output.",
    },
    "missing_decision": {
        "means": "'decision' was absent, not an object, or had no status.",
        "fix": "Send {'status': ..., 'rationale': ..., 'rests_on': [...]}. Note "
               "the field is 'status', not 'decision_status'.",
    },
    "decision_status_not_allowed": {
        "means": "The decision status was not one the runtime recognises.",
        "fix": "Use one of: blocked, wait, researching, experiment, "
               "recommended. An unrecognised status is silently recorded as "
               "'blocked', which would misreport what you decided.",
    },
    "decision_without_rationale": {
        "means": "The decision gave no reasoning.",
        "fix": "Set a non-empty 'rationale'. A decision with no stated reason "
               "cannot be reviewed or learned from.",
    },
    "experiment_contract_required": {
        "means": "The decision used status experiment without a falsifiable "
               "experiment contract.",
        "fix": "Set decision.experiment to an object with hypothesis, "
               "mechanism, measurement, counter_metric, evaluation_window, "
               "and rollback_condition.",
    },
    "experiment_field_required": {
        "means": "The experiment contract omitted a required field.",
        "fix": "The detail names the missing field. Supply a concrete "
               "hypothesis, mechanism, measurement, counter_metric, "
               "evaluation_window, and rollback_condition.",
    },
    "cannot_verify_input_unchanged": {
        "means": "This cycle_id already has a receipt persisted from content "
                 "that cannot be compared to this file.",
        "fix": "Commit a NEW file with its own unique name. Never rewrite an "
               "input that has already been executed.",
    },
    "input_changed_after_persist": {
        "means": "This cycle_id already has a receipt, and this file differs "
                 "from the content that produced it.",
        "fix": "Commit a NEW file with its own unique name rather than "
               "overwriting a cycle that already ran.",
    },
    "invalid_host_input": {
        "means": "The input failed validation; the specific codes follow it.",
        "fix": "Correct the listed codes and commit a new file.",
    },
}

for _code in (
    "known_instruction_recovery_source_missing",
    "known_instruction_recovery_source_invalid",
    "known_instruction_recovery_source_hash_mismatch",
):
    REFUSAL_GUIDANCE[_code] = {
        "means": "The immutable r27 recovery authority is unavailable or no "
                 "longer matches its pinned SHA-256.",
        "fix": "This is a repository integrity defect, not permission to "
               "guess or recreate the instruction. Preserve the candidate "
               "and restore the exact canonical r27 source bytes.",
    }

for _code in (
    "known_instruction_recovery_required",
    "known_instruction_recovery_requires_schema_version",
    "known_instruction_recovery_source_cycle_mismatch",
    "known_instruction_recovery_source_hash_required",
    "known_instruction_recovery_decision_status_mismatch",
):
    REFUSAL_GUIDANCE[_code] = {
        "means": "HH-13 is still missing and the candidate did not declare "
                 "the pinned r27 recovery lineage exactly.",
        "fix": "Add staged_order_instruction_recovery to the schema-v4 cycle. "
               "Copy source_cycle_id, source_sha256, and the original source "
               "decision status from the recovery contract without changing "
               "them.",
    }

for _code in (
    "known_instruction_recovery_create_missing",
    "known_instruction_recovery_create_mismatch",
):
    REFUSAL_GUIDANCE[_code] = {
        "means": "The recovery envelope omitted or changed the genuine r27 "
                 "create activity.",
        "fix": "Copy the exact JSON value at the contract's "
               "create_activity_path into "
               "staged_order_instruction_recovery.create_activity. Never "
               "call create order instruction again.",
    }

for _code in (
    "known_instruction_recovery_fresh_get_required",
    "known_instruction_recovery_fresh_get_operation",
    "known_instruction_recovery_fresh_get_tool",
    "known_instruction_recovery_fresh_get_time_invalid",
    "known_instruction_recovery_fresh_get_not_current_cycle",
    "known_instruction_recovery_get_state_mismatch",
):
    REFUSAL_GUIDANCE[_code] = {
        "means": "The recovery envelope did not contain a current-cycle get "
                 "whose exact result matches the committed instruction state.",
        "fix": "Call get order instructions now. Record operation, exact tool, "
               "request, result.order_instructions, and observed_at equal to "
               "this cycle's as_of. Copy the same returned list into the "
               "top-level order_instructions field.",
    }

for _code in (
    "known_instruction_recovery_status_invalid",
    "known_instruction_recovery_present_id_missing",
    "known_instruction_recovery_absent_id_present",
):
    REFUSAL_GUIDANCE[_code] = {
        "means": "The declared recovery status disagreed with the fresh "
                 "instruction state.",
        "fix": "Use status present only when the fresh get and top-level state "
               "both contain the pinned instruction id. Use absent only when "
               "both omit it; absence remains genuine evidence but does not "
               "complete HH-13.",
    }

for _code in (
    "known_instruction_recovery_instruction_required",
    "known_instruction_recovery_instruction_field",
    "known_instruction_recovery_instruction_identity",
    "known_instruction_recovery_instruction_mismatch",
):
    REFUSAL_GUIDANCE[_code] = {
        "means": "A present recovered instruction lacked the normalized "
                 "operator-facing contract or disagreed with r27.",
        "fix": "Provide action, quantity, order_type, limit_price, "
               "time_in_force, canonical instrument identity, "
               "rationale_one_line, review_condition, and rollback_condition. "
               "Trading fields must match the immutable r27 decision and "
               "create request.",
    }

for _code in (
    "learning_disposition_artifact_unresolved",
    "learning_disposition_artifacts_missing",
    "learning_disposition_evidence_missing",
    "learning_disposition_evidence_unresolved",
    "learning_disposition_no_change_has_artifacts",
    "learning_disposition_payload_invalid",
    "learning_disposition_rationale_invalid",
    "learning_disposition_receipt_cause_missing",
    "learning_disposition_record_count",
    "learning_disposition_record_type",
    "learning_disposition_schema_version",
    "learning_disposition_stage_mismatch",
    "learning_disposition_value_invalid",
):
    REFUSAL_GUIDANCE[_code] = {
        "means": "A persisted schema-v3/v4 learning disposition no longer "
               "reconciles with its receipt, evidence, or same-cycle "
               "artifact lineage.",
        "fix": "Treat this as a runtime integrity defect. Preserve the "
               "journal, inspect the named disposition and receipt records, "
               "and do not rewrite or silently skip the mismatch.",
    }

CANONICAL_EXAMPLE, _CANONICAL_SCHEMA_IMPORT_VIOLATIONS = inspect_example()


def _canonical_schema_feedback() -> tuple[dict[str, Any], dict[str, Any]]:
    return canonical_schema_status()


def explain(code: str) -> dict[str, str]:
    """What one refusal code means and what to do about it."""
    # Codes are emitted as "name" or "name:detail"; guidance is keyed by name.
    name = code.split(":", 1)[0]
    detail = code.split(":", 1)[1] if ":" in code else ""
    guidance = REFUSAL_GUIDANCE.get(name)
    if guidance is None:
        return {"code": code, "means": "Unrecognised refusal code.",
                "fix": "This is a runtime defect: the code has no guidance "
                       "entry. Report it rather than guessing."}
    entry = {"code": name, "means": guidance["means"], "fix": guidance["fix"]}
    if detail:
        entry["detail"] = detail
    return entry


def _split_validation_codes(body: str) -> list[str]:
    """Split validator codes without splitting commas inside details."""
    keys = sorted(REFUSAL_GUIDANCE, key=len, reverse=True)
    if not keys:
        return [body] if body else []
    pattern = re.compile(
        r",(?=(?:" + "|".join(re.escape(key) for key in keys)
        + r")(?=:|,|$))"
    )
    return [part for part in pattern.split(body) if part]


def parse_reason(reason: str) -> list[dict[str, str]]:
    """Split a refusal message into its individual explained codes."""
    # A refusal that is not a validation code is an exception string, e.g.
    # "JSONDecodeError: Expecting property name ... line 116 column 9". It
    # used to fall through to the unknown-code branch and tell the host its
    # own malformed file was a runtime defect to report, which is both wrong
    # and unactionable on the single most likely thing to go wrong.
    if reason.startswith("JSONDecodeError"):
        entry = explain("malformed_json")
        entry["detail"] = reason.split(": ", 1)[-1]
        return [entry]
    body = reason.split(": ", 1)[-1]
    if body.startswith("invalid_host_input:"):
        # invalid_host_input:<file>:<code>,<code>...
        parts = body.split(":", 2)
        body = parts[2] if len(parts) > 2 else ""
        return [explain(code) for code in _split_validation_codes(body)]
    return [explain(body.split(":", 1)[0] + (":" + body.split(":", 2)[2]
                                             if body.count(":") >= 2 else ""))]


def _full_refusal_code(entry: Mapping[str, Any]) -> str:
    code = str(entry.get("code", ""))
    detail = str(entry.get("detail", ""))
    return f"{code}:{detail}" if detail else code


def _explained_refusal(row: Mapping[str, Any]) -> list[dict[str, str]]:
    targets = {
        str(target.get("code", "")): target
        for target in row.get("correction_targets", ())
        if isinstance(target, Mapping) and target.get("code")
    }
    explained = []
    for entry in parse_reason(str(row.get("reason", ""))):
        enriched = dict(entry)
        target = targets.get(_full_refusal_code(entry))
        if target is not None:
            for field in ("json_pointer", "required_state"):
                if target.get(field):
                    enriched[field] = str(target[field])
        explained.append(enriched)
    return explained


def _retry_contract(
    refusals: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    targets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for refusal in refusals:
        for target in refusal.get("correction_targets", ()):
            if not isinstance(target, Mapping):
                continue
            pointer = str(target.get("json_pointer", ""))
            required_state = str(target.get("required_state", ""))
            if not pointer or not required_state:
                continue
            identity = (pointer, required_state)
            if identity in seen:
                continue
            seen.add(identity)
            targets.append({
                "code": str(target.get("code", "")),
                "json_pointer": pointer,
                "required_state": required_state,
            })
    if not targets:
        return None
    return {
        "refused_input": str(refusals[-1].get("input", "")),
        "must_change_paths": [target["json_pointer"] for target in targets],
        "targets": targets,
        "instruction": (
            "Before committing a retry, satisfy every target's required_state. "
            "Do not rewrite unrelated evidence as a substitute for changing "
            "these exact paths."
        ),
    }


def refusal_recurrence_from_history(
    history: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Count immutable byte-distinct candidates from the rejection ledger."""
    events_by_code: dict[str, dict[str, dict[str, str]]] = {}
    for event in history:
        candidate_id = str(event.get("candidate_id", ""))
        if not candidate_id:
            continue
        raw_codes = event.get("codes", ())
        if not isinstance(raw_codes, Sequence) or isinstance(
                raw_codes, (str, bytes)):
            continue
        for raw_code in set(str(code) for code in raw_codes):
            code = explain(raw_code)["code"]
            events_by_code.setdefault(code, {})[candidate_id] = {
                "candidate_id": candidate_id,
                "input": str(event.get("input", "")),
                "refused_at": str(event.get("refused_at", "")),
            }
    counts = {
        code: len(events)
        for code, events in events_by_code.items()
    }
    ordered = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    latest_by_code: dict[str, dict[str, str]] = {}
    for code, events in events_by_code.items():
        latest_by_code[code] = max(
            events.values(),
            key=lambda event: (
                event["refused_at"],
                event["candidate_id"],
            ),
        )
    return {
        "candidate_ids_by_code": {
            code: [
                event["candidate_id"]
                for event in sorted(
                    events_by_code[code].values(),
                    key=lambda event: (
                        event["refused_at"],
                        event["candidate_id"],
                    ),
                )[-3:]
            ]
            for code in ordered
        },
        "inputs_by_code": {
            code: sorted({
                event["input"]
                for event in events_by_code[code].values()
                if event["input"]
            })
            for code in ordered
        },
        "counts_by_code": ordered,
        "repeated": [
            {
                "code": code,
                "occurrences": count,
                "latest_input": latest_by_code[code]["input"],
                "latest_candidate_id": latest_by_code[code]["candidate_id"],
            }
            for code, count in ordered.items()
            if count > 1
        ],
    }


def refusal_pattern_summary(
    refusals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Count each refusal code once per candidate, not once per field."""
    counts: dict[str, int] = {}
    latest_input: dict[str, str] = {}
    current_cycles = 0
    for refusal in refusals:
        if refusal.get("first_seen_this_pass") is not False:
            current_cycles += 1
        input_name = str(refusal.get("input", ""))
        codes = {
            item["code"]
            for item in parse_reason(str(refusal.get("reason", "")))
        }
        for code in codes:
            counts[code] = counts.get(code, 0) + 1
            latest_input[code] = input_name
    ordered = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return {
        "cycles_analyzed": len(refusals),
        "current_cycles": current_cycles,
        "historical_cycles": len(refusals) - current_cycles,
        "counts_by_code": ordered,
        "repeated": [
            {
                "code": code,
                "cycles": count,
                "latest_input": latest_input[code],
            }
            for code, count in ordered.items()
            if count > 1
        ],
        "instruction": (
            "This is historical and current diagnostic context. Only rows in "
            "the top-level refused list are current actionable failures. A "
            "repeated current code requires a refusal postmortem and durable "
            "prompt, schema, or validator improvement before new research."
        ),
    }


def _updated_refusal_recurrence(
    existing: Mapping[str, Any],
    refusals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    previous = existing.get("refusal_recurrence", {})
    raw_inputs = (
        previous.get("inputs_by_code", {})
        if isinstance(previous, Mapping)
        else {}
    )
    previous_repeated = (
        previous.get("repeated", [])
        if isinstance(previous, Mapping)
        else []
    )
    inputs_by_code = {
        str(code): {
            str(input_name)
            for input_name in input_names
            if str(input_name)
        }
        for code, input_names in raw_inputs.items()
        if isinstance(input_names, list)
    }
    # Migrate the earlier count-only shape conservatively. A count without
    # candidate identities cannot be trusted for deduplication, but the latest
    # named input is still a real identity and can seed the new representation.
    for row in previous_repeated:
        if (
            isinstance(row, Mapping)
            and row.get("code")
            and row.get("latest_input")
        ):
            inputs_by_code.setdefault(str(row["code"]), set()).add(
                str(row["latest_input"]))
    for refusal in refusals:
        input_name = str(refusal.get("input", ""))
        codes = {
            item["code"]
            for item in parse_reason(str(refusal.get("reason", "")))
        }
        for code in codes:
            if input_name:
                inputs_by_code.setdefault(code, set()).add(input_name)
    counts = {
        code: len(input_names)
        for code, input_names in inputs_by_code.items()
    }
    ordered = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return {
        "inputs_by_code": {
            code: sorted(inputs_by_code[code])
            for code in ordered
        },
        "counts_by_code": ordered,
        "repeated": [
            {
                "code": code,
                "occurrences": count,
                "latest_input": sorted(inputs_by_code[code])[-1],
            }
            for code, count in ordered.items()
            if count > 1
        ],
    }


def write_feedback(input_dir: Path, *, accepted: Sequence[Mapping[str, Any]],
                   refusals: Sequence[Mapping[str, Any]],
                   skipped: Sequence[str],
                   open_recommendations: Mapping[str, Any] | None = None,
                   strategy_coverage: Mapping[str, Any] | None = None,
                   source_coverage: Mapping[str, Any] | None = None,
                   recent_reasoning: Mapping[str, Any] | None = None,
                   research_agenda: Mapping[str, Any] | None = None,
                   market_scout: Mapping[str, Any] | None = None,
                   candidate_registry: Mapping[str, Any] | None = None,
                   opportunity_ledger: Mapping[str, Any] | None = None,
                   forecast_ledger: Mapping[str, Any] | None = None,
                   forecast_outcomes: Mapping[str, Any] | None = None,
                   instruction_reconciliation:
                   Mapping[str, Any] | None = None,
                   empirical_calibration: Mapping[str, Any] | None = None,
                   research_value_census: Mapping[str, Any] | None = None,
                   learning_dispositions: Mapping[str, Any] | None = None,
                   reliability: Mapping[str, Any] | None = None,
                   goals: Mapping[str, Any] | None = None,
                   goal_attribution: Mapping[str, Any] | None = None,
                   recent_input_selection: Mapping[str, Any] | None = None,
                   open_experiments: Mapping[str, Any] | None = None,
                   theses: Mapping[str, Any] | None = None,
                   decision_outcomes: Mapping[str, Any] | None = None,
                   lessons: Mapping[str, Any] | None = None,
                   research_candidates: Mapping[str, Any] | None = None,
                   active_memory: Mapping[str, Any] | None = None,
                   research_memory: Mapping[str, Any] | None = None,
                   memory_distillation: Mapping[str, Any] | None = None,
                   tool_inventory: Mapping[str, Any] | None = None,
                   tool_provenance: Mapping[str, Any] | None = None,
                   market_sessions: Mapping[str, Any] | None = None,
                   mechanical_analysis: Mapping[str, Any] | None = None,
                   delivery_probes: Mapping[str, Any] | None = None) -> Path:
    """Write the message the host reads at the start of its next cycle."""
    path = Path(input_dir) / FEEDBACK_FILENAME
    active_refusals = [
        row for row in refusals
        if row.get("first_seen_this_pass") is not False
    ]
    expected_input_shape, canonical_schema = _canonical_schema_feedback()
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "runtime.run_host_cycle",
        "read_this_first": (
            "This is the runtime's reply to your last commit. If 'refused' is "
            "non-empty your input did not execute and no receipt exists for "
            "it. Fix the listed points and commit a NEW candidate under "
            "host_staging/ with a unique name. Never rewrite a file that was "
            "already accepted."
        ),
        "last_pass": {"accepted": len(accepted),
                      "refused": len(active_refusals),
                      "already_persisted": len(skipped)},
        "accepted": [dict(row) for row in accepted],
        # Only refusals the host has not already moved past. Six stale ones
        # were being re-sent every cycle, costing more to read than the
        # instructions themselves, and the host had already corrected all of
        # them. A refusal it has superseded is history, not feedback.
        "refused": [
            {"input": row["input"], "reason": row["reason"],
             "what_to_fix": parse_reason(str(row["reason"]))}
            for row in active_refusals[-3:]
        ],
        "refusal_patterns": refusal_pattern_summary(refusals),
        "older_refusals_not_shown": max(0, len(active_refusals) - 3),
        "already_persisted": list(skipped),
        # What the host has already proposed and nobody has resolved. Without
        # this it cannot know it recommended the same trade an hour ago, and
        # three cycles in thirty minutes each recommended the same trim at
        # a different price with no mention of the others.
        "open_recommendations": open_recommendations or {"count": 0, "open": []},
        # Which strategy families have been drawn on. Ten cycles used none of
        # the seventeen, because nothing ever asked: "the highest-conviction
        # risk-reducing action" is answered by trimming the largest position
        # every time a portfolio has a largest position.
        "strategy_coverage": strategy_coverage or {},
        # Installed sources that have never been called. Four of six had zero
        # calls while IBKR had forty-three.
        "source_coverage": source_coverage or {},
        # The host had no memory across cycles and re-derived everything on
        # each run. ACTIVE_BRAIN.md was designed as this surface and nothing
        # ever wrote to it.
        "recent_reasoning": recent_reasoning or {},
        "research_agenda": research_agenda or {
            "cycles_examined": 0,
            "recent": [],
            "selection_counts": {},
        },
        "market_scout": market_scout or {
            "available": False,
            "what_this_means": (
                "No durable Market Scout stage exists yet."
            ),
        },
        "candidate_registry": candidate_registry or {
            "available": False,
            "unpromoted_total": 0,
            "items": [],
            "not_shown": 0,
            "what_this_means": (
                "No unpromoted Market Scout candidates exist yet."
            ),
        },
        "opportunity_ledger": opportunity_ledger or {
            "total": 0,
            "counts_by_state": {},
            "items": [],
            "not_shown": 0,
            "soft_identity_collisions": [],
            "what_this_means": (
                "No durable opportunity events exist in the supplied records."
            ),
        },
        "forecast_ledger": forecast_ledger or {
            "total": 0,
            "open_count": 0,
            "measured_count": 0,
            "overdue_count": 0,
            "superseded_count": 0,
            "items": [],
            "not_shown": 0,
            "what_this_means": (
                "No immutable ex-ante forecasts exist in the supplied "
                "records."
            ),
        },
        "forecast_outcomes": forecast_outcomes or {
            "measured_count": 0,
            "overdue_count": 0,
            "open_count": 0,
            "matured_count": 0,
            "measurement_coverage": None,
            "invalidated_count": 0,
            "outcome_counts": {
                "resolved_true": 0,
                "resolved_false": 0,
            },
            "calibration": {
                "source": "persisted_forecast_outcomes",
                "n": 0,
                "brier": None,
                "log_loss": None,
                "hit_rate": None,
                "reliability": [],
                "status": "insufficient_data",
            },
            "recent": [],
            "what_this_means": (
                "No matured forecast outcomes exist in the supplied records."
            ),
        },
        "instruction_reconciliation": instruction_reconciliation or {
            "total_records": 0,
            "active_count": 0,
            "counts_by_status": {},
            "items": [],
            "not_shown": 0,
            "what_this_means": (
                "No explicit operator instruction reconciliation records "
                "exist in the supplied journal."
            ),
        },
        "empirical_calibration": empirical_calibration or {
            "forecast_rows": 0,
            "exact_forecast_reconciliation_matches": 0,
            "forecast_outcome_calibration": {
                "source": "persisted_forecast_outcomes",
                "n": 0,
                "brier": None,
                "log_loss": None,
                "hit_rate": None,
                "reliability": [],
                "status": "insufficient_data",
            },
            "directional_decision_metrics": {},
            "timing_groups": [],
            "implementation": {
                "reconciliation_count": 0,
                "counts_by_status": {},
                "modification_field_counts": {},
                "submitted_count": 0,
                "executed_count": 0,
                "accepted_without_fill_count": 0,
                "unknown_or_disputed_count": 0,
            },
            "unresolved": {
                "open_forecasts": 0,
                "overdue_forecasts": 0,
                "measured_invalidated_forecasts": 0,
                "forecast_reconciliation_conflicts": 0,
                "unmatched_forecasts": 0,
                "unmatched_reconciliations": 0,
            },
            "unmatched_forecasts": [],
            "unmatched_reconciliations": [],
            "recent_rows": [],
            "what_this_means": (
                "No empirical forecast/reconciliation rows exist yet."
            ),
        },
        "research_value_census": research_value_census or {
            "cycles_examined": 0,
            "research_rows": 0,
            "questions": {
                "unique": 0,
                "repeated": 0,
                "top_repeated": [],
            },
            "source_tool_use": {
                "call_count": 0,
                "tools": {},
                "origins": {},
            },
            "result_novelty": {
                "observations": 0,
                "unique_result_hashes": 0,
                "repeated_result_observations": 0,
                "unique_rate": None,
            },
            "adversarial_disputes": {
                "count": 0,
                "decision_changed_count": 0,
                "decision_change_rate": None,
                "sampling_independence": "single_host_role_execution",
                "recent": [],
            },
            "dimensions": {},
            "what_this_means": (
                "No research-value records exist in the supplied journal."
            ),
        },
        "learning_dispositions": learning_dispositions or {
            "count": 0,
            "recent": [],
            "not_shown": 0,
            "what_this_means": (
                "No schema-v3/v4 learning dispositions exist in the supplied "
                "records."
            ),
        },
        "reliability": reliability or {
            "scope": {"kind": "no_journal_records"},
            "candidate_attempts": {
                "total": 0,
                "accepted_receipts": 0,
                "cycle_candidate_refusals": 0,
                "excluded_non_cycle_refusals": 0,
                "attempt_acceptance_rate": None,
            },
        },
        "goals": goals or {
            "open_count": 0,
            "expired_count": 0,
            "open": [],
            "closed_count": 0,
            "excluded_closed_count": 0,
            "invalidated_count": 0,
            "recent_closed": [],
        },
        "goal_attribution": goal_attribution or {
            "sample_count": 0,
            "outcome_counts": {
                "met": 0,
                "partially_met": 0,
                "missed": 0,
                "invalidated": 0,
            },
            "closures_excluded": {"count": 0, "reasons": {}},
            "coverage": {
                "closures_with_goal_pattern": 0,
                "closures_with_origin_causes": 0,
                "closures_with_evidence_sources": 0,
                "closures_with_next_change": 0,
            },
            "goal_patterns": {
                "distinct_count": 0, "not_shown": 0, "rows": [],
            },
            "origin_causes": {
                "distinct_count": 0, "not_shown": 0, "rows": [],
            },
            "closure_evidence_sources": {
                "distinct_count": 0, "not_shown": 0, "rows": [],
            },
            "what_this_means": (
                "No closed goals exist in the records supplied to this "
                "summary, so no quality attribution claim is available."
            ),
        },
        "recent_input_selection": recent_input_selection or {},
        "open_experiments": open_experiments or {"count": 0, "open": []},
        # Theses already held. Four cycles concluded "no new actionable
        # thesis" because each started from nothing; a thesis needing a week
        # of evidence could never form in one hour of research.
        "theses": theses or {},
        # Past decisions against what followed. Every cycle reported
        # "insufficient attributable outcomes" while the snapshots needed to
        # compute them were already in the repository.
        "decision_outcomes": decision_outcomes or {},
        # What the host has concluded about deciding. evolution.py has
        # derived lessons since it was written; the journal held zero.
        "lessons": lessons or {},
        # Enumerated by discovery.py, which has produced these since it was
        # written and was never called.
        "research_candidates": research_candidates or {},
        "active_memory": active_memory or {"count": 0, "items": []},
        "research_memory": research_memory or {
            "count": 0,
            "items": [],
            "retrieval_order": [
                "active_memory",
                "research_memory",
                "raw_archive",
            ],
        },
        "memory_distillation": memory_distillation or {},
        "tool_inventory": tool_inventory or {},
        "tool_provenance": tool_provenance or {
            "cycle_id": None,
            "call_count": 0,
            "connector_response_count": 0,
            "host_summary_count": 0,
            "rows": [],
            "not_shown": 0,
            "what_this_means": (
                "No Market Scout or research tool provenance index exists "
                "in the supplied records."
            ),
        },
        "market_sessions": market_sessions or {},
        "mechanical_analysis": mechanical_analysis or {},
        "delivery_probes": delivery_probes or {},
        "canonical_schema": canonical_schema,
        # Only when it is needed. Sending the schema every cycle to a host
        # that has been committing valid input for hours is pure cost.
        "expected_input_shape": (
            expected_input_shape if active_refusals else None
        ),
    }
    # Rewriting an identical message with a fresh timestamp made every idle
    # pass dirty the tree, so the scheduler committed and pushed a one-line
    # change to generated_at each time it ran and found nothing to do. At
    # hourly that is 24 junk commits a day; at any polling frequency worth
    # having it is far worse, and it buries the real receipts.
    #
    # generated_at is excluded from the comparison because it is the one
    # field guaranteed to differ. Everything the host actually reads is
    # compared, so a genuine change still lands immediately.
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            # Compare at the JSON boundary. Runtime contracts may contain
            # tuples, but JSON reloads them as lists. Comparing the live
            # Python objects made identical serialized feedback look changed
            # on every idle poll, producing a commit that changed only the
            # timestamp and dictionary key order.
            existing_body = {
                k: v for k, v in existing.items() if k != "generated_at"
            }
            payload_body = {
                k: v for k, v in payload.items() if k != "generated_at"
            }
            normalized_payload = json.loads(json.dumps(payload_body))
            if existing_body == normalized_payload:
                return path
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n",
                    encoding="utf-8")
    return path


def write_validation_feedback(
    input_dir: Path,
    *,
    checked: Sequence[str],
    refusals: Sequence[Mapping[str, Any]],
    refusal_history: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """Update only validation fields, preserving executor-owned feedback."""
    path = Path(input_dir) / FEEDBACK_FILENAME
    expected_input_shape, canonical_schema = _canonical_schema_feedback()
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        if isinstance(value, dict):
            existing = value
    payload = dict(existing)
    payload.update({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "runtime.host_input_validator",
        "read_this_first": VALIDATION_READ_THIS_FIRST,
        "last_validation": {
            "checked": list(checked),
            "refused": len(refusals),
        },
        "refused": [
            {
                "input": row["input"],
                "reason": row["reason"],
                "candidate_id": row.get("candidate_id"),
                "archive": row.get("archive"),
                "what_to_fix": _explained_refusal(row),
            }
            for row in refusals[-3:]
        ],
        "refusal_recurrence": (
            refusal_recurrence_from_history(refusal_history)
            if refusal_history is not None
            else _updated_refusal_recurrence(existing, refusals)
        ),
        "retry_contract": _retry_contract(refusals),
        "older_refusals_not_shown": max(0, len(refusals) - 3),
        "canonical_schema": canonical_schema,
        "expected_input_shape": expected_input_shape if refusals else None,
    })
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def refresh_validation_feedback(
    input_dir: Path | str,
    *,
    refusal_history: Sequence[Mapping[str, Any]],
) -> Path:
    """Refresh derived guidance without replaying or rewriting validation."""
    path = Path(input_dir) / FEEDBACK_FILENAME
    if not path.exists():
        return path
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"validation_feedback_unreadable:{path}:{type(error).__name__}:"
            f"{error}"
        ) from error
    if not isinstance(existing, Mapping):
        raise ValueError(f"validation_feedback_root_must_be_object:{path}")
    refused = existing.get("refused", [])
    if not isinstance(refused, list):
        raise ValueError(f"validation_feedback_refused_must_be_list:{path}")

    history_by_candidate = {
        str(event.get("candidate_id")): event
        for event in refusal_history
        if isinstance(event, Mapping) and event.get("candidate_id")
    }
    refreshed_refusals = []
    for index, row in enumerate(refused):
        if not isinstance(row, Mapping):
            raise ValueError(
                f"validation_feedback_refusal_must_be_object:{path}:{index}"
            )
        refreshed = dict(row)
        event = history_by_candidate.get(str(row.get("candidate_id") or ""))
        if event is not None:
            enriched = dict(row)
            enriched["correction_targets"] = event.get(
                "correction_targets", [])
            refreshed["what_to_fix"] = _explained_refusal(enriched)
        refreshed_refusals.append(refreshed)

    expected_input_shape, canonical_schema = _canonical_schema_feedback()
    payload = dict(existing)
    payload.update({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "runtime.host_input_validator",
        "read_this_first": VALIDATION_READ_THIS_FIRST,
        "refused": refreshed_refusals,
        "canonical_schema": canonical_schema,
        "expected_input_shape": (
            expected_input_shape
            if existing.get("expected_input_shape") is not None
            or refreshed_refusals
            else None
        ),
    })
    existing_body = {
        key: value for key, value in existing.items()
        if key != "generated_at"
    }
    payload_body = {
        key: value for key, value in payload.items()
        if key != "generated_at"
    }
    if existing_body == json.loads(json.dumps(payload_body)):
        return path
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path
