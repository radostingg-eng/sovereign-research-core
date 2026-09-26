"""Standalone host-side preflight for Sovereign Research semantic candidates.
Runs in the host sandbox before it commits host_staging/<cycle>.semantic.json:
re-implements the self-contained slice of the real intake validators
(decidable from the candidate JSON plus FEEDBACK.json). See NOTES at EOF
for what was dropped/narrowed relative to the runtime validators.

Usage: exec(preflight_source); print(json.dumps(preflight(candidate_text, feedback_text), indent=1))
CLI: python3 host_tools/preflight.py candidate.json [FEEDBACK.json]
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone


_ISO_TIMESTAMP = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})" r"(?:(?P<separator>[Tt ])" r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})" r"(?P<fraction>[.,]\d+)?" r"(?P<offset>Z|z|[+-]\d{2}(?::?\d{2})?)?" r")?$")

def _normalize_iso_timestamp(value):
    if not isinstance(value, str) or not value.strip(): return None
    match = _ISO_TIMESTAMP.fullmatch(value.strip())
    if match is None: return None
    if match.group("separator") is None: return match.group("date")
    fraction = match.group("fraction")
    normalized_fraction = ""
    if fraction:
        digits = fraction[1:]
        normalized_fraction = "." + (digits + "000000")[:6]
    offset = match.group("offset") or ""
    if offset in {"Z", "z"}: offset = "+00:00"
    elif offset and ":" not in offset:
        offset = (f"{offset}:00" if len(offset) == 3 else f"{offset[:3]}:{offset[3:]}")
    return (f"{match.group('date')}T{match.group('hour')}:" f"{match.group('minute')}:{match.group('second')}" f"{normalized_fraction}{offset}")

def _parse_iso_timestamp(value):
    normalized = _normalize_iso_timestamp(value)
    if normalized is None: return None
    try: return datetime.fromisoformat(normalized)
    except ValueError: return None

def _aware(value):
    parsed = _parse_iso_timestamp(value)
    if parsed is None or parsed.tzinfo is None: return None
    return parsed.astimezone(timezone.utc)

def _effective_as_of(data):
    snapshot = data.get("snapshot")
    if isinstance(snapshot, dict) and "as_of" in snapshot: return snapshot.get("as_of")
    return data.get("as_of")


REDACTION_SENTINEL = "__SOVEREIGN_REDACTED__"

def _canonical_json_bytes(value):
    return (json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")

def _canonical_equal(left, right):
    try: return _canonical_json_bytes(left) == _canonical_json_bytes(right)
    except (TypeError, ValueError): return False

def _pointer_tokens(path):
    if not path.startswith("/"): return None
    tokens = []
    for token in path[1:].split("/"):
        if re.search(r"~(?![01])", token): return None
        tokens.append(token.replace("~1", "/").replace("~0", "~"))
    return tokens

def _pointer_value(value, tokens):
    current = value
    for token in tokens:
        if isinstance(current, dict):
            if token not in current: raise KeyError(token)
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current): raise KeyError(token)
            current = current[index]
        else: raise KeyError(token)
    return current

def _json_pointer_value(value, path):
    tokens = _pointer_tokens(path)
    if tokens is None: raise ValueError("json_pointer_invalid")
    return _pointer_value(value, tokens)

def _pointer_exists(value, path):
    try: _json_pointer_value(value, path)
    except (KeyError, ValueError): return False
    return True

def _contains_redaction(value):
    if value == REDACTION_SENTINEL: return True
    if isinstance(value, dict): return any(_contains_redaction(v) for v in value.values())
    if isinstance(value, list): return any(_contains_redaction(v) for v in value)
    return False

def _matching_source_paths(value, target, prefix=""):
    matches = []
    if _canonical_equal(value, target): matches.append(prefix or "/")
    if isinstance(value, dict):
        for key, child in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            matches.extend(_matching_source_paths(child, target, f"{prefix}/{token}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            matches.extend(_matching_source_paths(child, target, f"{prefix}/{index}"))
    return matches


def _ts(value):
    if not isinstance(value, str): return None
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError: return None
    if parsed.tzinfo is None or parsed.utcoffset() is None: return None
    return parsed

_DATE_ONLY_PUBLISHED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
def _normalized_published_at_ok(value):
    return (isinstance(value, str) and _DATE_ONLY_PUBLISHED_AT.match(value) is not None) or _ts(value) is not None

def _validate_web_sources(value, *, refs, validation_now=None):
    if not isinstance(value, list): return ["web_sources_not_list"]
    errors = []
    url_refs = { str(ref.get("value", "")).strip() for ref in refs if isinstance(ref, dict) and str(ref.get("kind", "")).strip().casefold() in {"url", "link"} }
    urls = set()
    for index, row in enumerate(value):
        if not isinstance(row, dict): continue
        url = row.get("url")
        if isinstance(url, str) and url.strip(): urls.add(url.strip())
        published_value = row.get("published_at")
        if published_value is not None and not _normalized_published_at_ok(published_value): errors.append(f"web_source_{index}_published_at")
    if url_refs - urls: errors.append("web_sources_url_refs_mismatch")
    return sorted(set(errors))

def _validate_tool_call_provenance(compact, *, validation_now=None):
    refs = compact.get("source_refs")
    refs = refs if isinstance(refs, list) else []
    return _validate_web_sources(compact.get("web_sources"), refs=refs, validation_now=validation_now)


CAPTURE_ORIGIN_ALIASES = {k: "host_summary" for k in ("web_source", "web_search_result", "external_search", "web_search", "search_result", "direct_web_search", "direct_web_response", "direct_web_result", "web_result_summary")}
CAPTURE_ORIGIN_ALIASES["direct_file_analysis"] = "host_transcribed_response"
DERIVABLE_EVIDENCE_PRODUCERS = frozenset({"portfolio", "saved_instructions", "account_orders", "account_trades", "market_sessions"})
CORE_STAGE_IDS = frozenset({"portfolio", "market_scout", "research_director", "memory_retrieval", "evidence_arbitration", "portfolio_fit", "counterfactual", "adversarial", "governance_review", "decision", "learning_audit", "meta_research", "self_improvement"})

def _text(value):
    return value.strip() if isinstance(value, str) else ""

def _text_any(value):
    return str(value).strip() if value is not None else ""

def _nested_call_source(value):
    if not isinstance(value.get("provenance"), dict): return None
    if isinstance(value.get("call"), dict): return "call"
    action = value.get("action")
    if isinstance(action, dict) and ("action" in action or "arguments" in action): return "action"
    return None

def _compact_call_values(value):
    nested = _nested_call_source(value)
    nested_call = value.get(nested) if nested else None
    provenance = value.get("provenance")
    if isinstance(nested_call, dict) and isinstance(provenance, dict):
        result = {"tool_call_id": value.get("tool_call_id"), "kind": value.get("kind"), "tool": value.get("tool"), "action": nested_call.get("action"), "arguments": nested_call.get("arguments"), "result": value.get("result")}
    else: result = dict(value)
    if isinstance(provenance, dict):
        capture = provenance.get("capture")
        capture = capture if isinstance(capture, dict) else {}
        provenance_fields = {"capture_origin": capture.get("capture_origin"), "result_origin": provenance.get("result_origin"), "observed_at": provenance.get("observed_at"), "source_refs": provenance.get("source_refs"), "web_sources": provenance.get("web_sources"), "redactions": capture.get("redactions"), "request_redactions": capture.get("request_redactions")}
        for field, item in provenance_fields.items():
            if field not in result and item is not None: result[field] = item
    action = result.get("action")
    if (isinstance(action, dict) and isinstance(action.get("name"), str) and action["name"].strip() and "arguments" not in result):
        result["action"] = action["name"].strip()
        result["arguments"] = { k: v for k, v in action.items() if k != "name"}
    return result

def _capture_origin(value):
    explicit = value.get("capture_origin")
    if explicit is not None: return CAPTURE_ORIGIN_ALIASES.get(_text(explicit), _text(explicit))
    if _text(value.get("action")) and isinstance(value.get("result"), (dict, list)): return "direct_connector_response"
    return "host_summary"

def _evidence_call_input(wrapper, *, producer):
    value = wrapper.get("call")
    if value is None:
        value = { k: v for k, v in wrapper.items() if k not in {"producer", "projection", "market_region"} }
    if not isinstance(value, dict): return value
    result = dict(value)
    result.setdefault("kind", "connector_lookup")
    default_tool = {"portfolio": "IBKR", "saved_instructions": "IBKR", "account_orders": "IBKR", "account_trades": "IBKR", "market_sessions": "market clock", "market_scout": "web.search"}.get(producer)
    if default_tool: result.setdefault("tool", default_tool)
    return result

def _probe_call(value, *, pointer):
    issues = []
    if not isinstance(value, dict): return [("semantic_tool_call_object", pointer, "")]
    compact = _compact_call_values(value)
    for field in ("tool_call_id", "kind", "tool", "action", "arguments", "result", "observed_at"):
        if field not in compact: issues.append(("semantic_tool_call_missing", pointer, field))
    explicit_origin = compact.get("capture_origin")
    if explicit_origin is not None:
        origin = CAPTURE_ORIGIN_ALIASES.get(_text(explicit_origin), _text(explicit_origin))
        if origin not in {"direct_connector_response", "host_transcribed_response", "host_summary"}:
            issues.append(("semantic_capture_origin", pointer, origin))
    return issues

def _probe_call_id_reuse(value, *, pointer, observed):
    if not isinstance(value, dict): return []
    compact = _compact_call_values(value)
    call_id = _text(compact.get("tool_call_id"))
    if not call_id: return []
    previous = observed.get(call_id)
    if previous is None:
        observed[call_id] = (compact, pointer)
        return []
    prior_call, prior_pointer = previous
    identity_fields = ("action", "arguments", "result", "observed_at")
    if not all(f in prior_call for f in identity_fields):
        if all(f in compact for f in identity_fields): observed[call_id] = (compact, pointer)
        return []
    if not all(f in compact for f in identity_fields): return []
    if any(f in compact and f in prior_call and not _canonical_equal(compact[f], prior_call[f]) for f in (*identity_fields, "tool", "kind")):
        return [("semantic_tool_call_id_conflict", pointer, prior_pointer)]
    return []

def _rewrite_stage_id_refs(data, *, old_id, new_id):
    old_ref, new_ref = f"stage:{old_id}", f"stage:{new_id}"
    def _rewrite(container, key):
        rows = container.get(key) if isinstance(container, dict) else None
        if not isinstance(rows, list): return
        for i, item in enumerate(rows):
            if item == old_ref: rows[i] = new_ref
    decision = data.get("decision")
    if isinstance(decision, dict): _rewrite(decision.get("repetition_review"), "evidence_delta")
    for row in data.get("learning_stage_dispositions") or ():
        _rewrite(row, "evidence")

def _normalize_stale_specialist_stage_ids(data):
    agenda = data.get("research_agenda")
    stage_outputs = data.get("stage_outputs")
    research = data.get("research")
    if (not isinstance(agenda, dict) or not isinstance(stage_outputs, dict) or not isinstance(research, list)):
        return
    selected_ids = [ _text(c.get("candidate_id")) for c in agenda.get("candidates") or () if isinstance(c, dict) and c.get("selected") is True and _text(c.get("candidate_id")) ]
    if len(selected_ids) != 1: return
    candidate_id = selected_ids[0]
    stage_keys = set(stage_outputs)
    renamed_from = None
    if candidate_id not in stage_keys:
        non_core = [k for k in stage_keys if k not in CORE_STAGE_IDS]
        if len(non_core) != 1: return
        renamed_from = non_core[0]
        stage_keys = (stage_keys - {renamed_from}) | {candidate_id}
    rows_to_fix = []
    for row in research:
        if not isinstance(row, dict): continue
        stage_id = _text(row.get("specialist_stage_id"))
        if not stage_id or stage_id == candidate_id: continue
        if stage_id in stage_keys: return
        rows_to_fix.append(row)
    if renamed_from is not None:
        stage_outputs[candidate_id] = stage_outputs.pop(renamed_from)
    for row in rows_to_fix: row["specialist_stage_id"] = candidate_id
    if renamed_from is not None:
        _rewrite_stage_id_refs(data, old_id=renamed_from, new_id=candidate_id)

def _probe_structural(data):
    issues = []
    observed_call_ids = {}

    for r_index, row in enumerate(data.get("research") or ()):
        if not isinstance(row, dict): continue
        for c_index, call in enumerate(row.get("tool_calls") or ()):
            pointer = f"/research/{r_index}/tool_calls/{c_index}"
            issues.extend(_probe_call(call, pointer=pointer))
            issues.extend(_probe_call_id_reuse(call, pointer=pointer, observed=observed_call_ids))

    scout = data.get("market_scout_report")
    if isinstance(scout, dict):
        for c_index, call in enumerate(scout.get("tool_calls") or ()):
            pointer = f"/market_scout_report/tool_calls/{c_index}"
            issues.extend(_probe_call(call, pointer=pointer))
            issues.extend(_probe_call_id_reuse(call, pointer=pointer, observed=observed_call_ids))

    evidence_calls = data.get("evidence_calls")
    if isinstance(evidence_calls, list):
        for index, wrapper in enumerate(evidence_calls):
            pointer = f"/evidence_calls/{index}"
            if not isinstance(wrapper, dict):
                issues.append(("semantic_evidence_call_object", pointer, ""))
                continue
            producer = _text(wrapper.get("producer"))
            call_input = _evidence_call_input(wrapper, producer=producer)
            call_pointer = f"{pointer}/call" if "call" in wrapper else pointer
            issues.extend(_probe_call(call_input, pointer=call_pointer))
            issues.extend(_probe_call_id_reuse(call_input, pointer=call_pointer, observed=observed_call_ids))
            compact = (_compact_call_values(call_input) if isinstance(call_input, dict) else {})
            origin = _capture_origin(compact)
            if ("projection" not in wrapper and origin != "host_summary" and producer not in DERIVABLE_EVIDENCE_PRODUCERS):
                issues.append(("semantic_evidence_target_missing", pointer, producer))

    agenda = data.get("research_agenda")
    selected_rows = []
    if isinstance(agenda, dict):
        for c_index, candidate in enumerate(agenda.get("candidates") or ()):
            if (isinstance(candidate, dict) and candidate.get("selected") is True and _text(candidate.get("candidate_id"))):
                selected_rows.append((c_index, _text(candidate.get("candidate_id"))))
    research_stage_ids = { _text(row.get("specialist_stage_id")) for row in data.get("research") or () if isinstance(row, dict) and _text(row.get("specialist_stage_id")) }
    for c_index, candidate_id in selected_rows:
        if candidate_id in research_stage_ids: continue
        issues.append(("semantic_selected_specialist_mismatch", f"/research_agenda/candidates/{c_index}/candidate_id", f"candidate_id={candidate_id};specialist_stage_ids=" + "|".join(sorted(research_stage_ids))))
    return issues

def _current_evidence_refs(data):
    selected = { _text(c.get("candidate_id")) for c in ((data.get("research_agenda") or {}).get("candidates") or ()) if isinstance(c, dict) and c.get("selected") is True and _text(c.get("candidate_id")) }
    stage_outputs = data.get("stage_outputs")
    stage_keys = set(stage_outputs) if isinstance(stage_outputs, dict) else set()
    stage_ids = CORE_STAGE_IDS | (selected & stage_keys) | (selected & CORE_STAGE_IDS)
    refs = {f"stage:{s}" for s in stage_ids}
    refs.update(f"finding:{fid}" for row in data.get("findings") or () if isinstance(row, dict) and (fid := _text(row.get("id"))))
    return refs


EVIDENCE_PRODUCERS = frozenset({"portfolio", "saved_instructions", "account_orders", "account_trades", "market_sessions"})
PROJECTION_FIELDS = frozenset({"extractor", "bindings"})
BINDING_FIELDS = frozenset({"source_path", "target_path"})
MAX_BINDINGS = 20

def _present_order_instruction_paths(data):
    paths = []
    if "order_instructions" in data: paths.append("/order_instructions")
    snapshot = data.get("snapshot")
    if isinstance(snapshot, dict):
        if "order_instructions" in snapshot: paths.append("/snapshot/order_instructions")
        if "saved_order_instructions" in snapshot: paths.append("/snapshot/saved_order_instructions")
    return paths

def _required_producers(data):
    required = {"portfolio", "saved_instructions", "market_sessions"}
    snapshot = data.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    if "open_orders" in snapshot: required.add("account_orders")
    if "trades" in snapshot: required.add("account_trades")
    return required

def _required_targets(data):
    return {
        "portfolio": {"/snapshot/net_liquidation_value", "/snapshot/cash", "/snapshot/positions"},
        "saved_instructions": set(_present_order_instruction_paths(data)),
        "account_orders": {"/snapshot/open_orders"},
        "account_trades": {"/snapshot/trades"},
        "market_sessions": set(),
    }

def _derivable_target_paths(producer, data):
    if producer == "portfolio":
        candidates = ("/snapshot/net_liquidation_value", "/snapshot/cash", "/snapshot/positions")
    elif producer == "saved_instructions": candidates = tuple(_present_order_instruction_paths(data))
    elif producer == "account_orders": candidates = ("/snapshot/open_orders",)
    elif producer == "account_trades": candidates = ("/snapshot/trades",)
    else: return ()
    return tuple(path for path in candidates if _pointer_exists(data, path))

def _market_session_target_path(wrapper, compact, data):
    region = _text(wrapper.get("market_region"))
    if not region:
        args = compact.get("arguments")
        if isinstance(args, dict): region = _text_any(args.get("region"))
    region = region.upper()
    if not region: return ()
    sessions = data.get("market_sessions")
    markets = sessions.get("markets") if isinstance(sessions, dict) else None
    if not isinstance(markets, list): return ()
    index = next((i for i, m in enumerate(markets) if isinstance(m, dict) and _text(m.get("region")).upper() == region), None)
    if index is None: return ()
    return (f"/market_sessions/markets/{index}/is_open",)

def _check_evidence_coverage(data, *, validation_now):
    calls = data.get("evidence_calls")
    problems = []
    if not isinstance(calls, list): return problems

    captured, targets, market_ids = {}, {}, set()
    sessions = data.get("market_sessions")
    markets = sessions.get("markets") if isinstance(sessions, dict) else None
    region_names = set()
    if isinstance(markets, list):
        for market in markets:
            if not isinstance(market, dict): continue
            region_names.add(_text(market.get("region")).upper())
            for value in market.get("evidence_tool_call_ids") or ():
                if isinstance(value, str) and value.strip(): market_ids.add(value.strip())
    for wrapper in calls:
        if not isinstance(wrapper, dict) or _text(wrapper.get("producer")) != "market_sessions": continue
        call_input = _evidence_call_input(wrapper, producer="market_sessions")
        compact = _compact_call_values(call_input) if isinstance(call_input, dict) else {}
        call_id = _text(compact.get("tool_call_id"))
        region = _text(wrapper.get("market_region")).upper()
        if not region:
            args = compact.get("arguments")
            if isinstance(args, dict): region = _text_any(args.get("region")).upper()
        if region and region in region_names and call_id: market_ids.add(call_id)

    for index, wrapper in enumerate(calls):
        prefix = f"evidence_call_invalid:{index}"
        pointer = f"/evidence_calls/{index}"

        def add(suffix, ptr=None):
            problems.append((f"{prefix}:{suffix}", ptr or pointer))

        if not isinstance(wrapper, dict):
            add("not_object")
            continue
        producer = _text(wrapper.get("producer"))
        if producer not in EVIDENCE_PRODUCERS: add("producer", f"{pointer}/producer")
        call_input = _evidence_call_input(wrapper, producer=producer)
        if not isinstance(call_input, dict):
            add("call")
            continue
        compact = _compact_call_values(call_input)
        tool = compact.get("tool")
        if not isinstance(tool, str) or not tool.strip(): add("tool")
        call_id = compact.get("tool_call_id")
        call_id = call_id.strip() if isinstance(call_id, str) else ""
        if not call_id: add("tool_call_id")
        else: captured.setdefault(producer, set()).add(call_id)

        for problem in _validate_tool_call_provenance(compact, validation_now=validation_now):
            add(f"provenance:{problem}")

        origin = _capture_origin(compact)
        projection = wrapper.get("projection")
        if origin == "host_summary":
            if projection is not None: add("host_summary_projection")
            continue
        if "projection" not in wrapper:
            # _derive_projection: no target path -> target_missing; a path exists but none match -> None projection.
            if producer == "market_sessions": candidate_paths = _market_session_target_path(wrapper, compact, data)
            else: candidate_paths = _derivable_target_paths(producer, data)
            if not candidate_paths:
                problems.append(("semantic_evidence_target_missing", pointer))
                continue
            bound_any = False
            for path in candidate_paths:
                try: target_value = _json_pointer_value(data, path)
                except (KeyError, ValueError): continue
                if _matching_source_paths(compact.get("result"), target_value):
                    targets.setdefault(producer, set()).add(path)
                    bound_any = True
            if not bound_any: add("projection")
            continue
        if not isinstance(projection, dict):
            add("projection")
            continue
        if set(projection) != PROJECTION_FIELDS: add("projection_fields", f"{pointer}/projection")
        if projection.get("extractor") != "json_pointer_v1": add("extractor", f"{pointer}/projection/extractor")
        bindings = projection.get("bindings")
        if not isinstance(bindings, list) or not bindings:
            add("bindings", f"{pointer}/projection/bindings")
            continue
        if len(bindings) > MAX_BINDINGS: add("bindings_too_many", f"{pointer}/projection/bindings")
        for b_index, binding in enumerate(bindings):
            b_suffix = f"binding:{b_index}"
            b_pointer = f"{pointer}/projection/bindings/{b_index}"
            if not isinstance(binding, dict):
                add(f"{b_suffix}:not_object", b_pointer)
                continue
            if set(binding) != BINDING_FIELDS: add(f"{b_suffix}:fields", b_pointer)
            source_path = binding.get("source_path")
            target_path = binding.get("target_path")
            if not isinstance(source_path, str):
                add(f"{b_suffix}:source_path", b_pointer)
                continue
            if not isinstance(target_path, str):
                add(f"{b_suffix}:target_path", b_pointer)
                continue
            try:
                source = _json_pointer_value(compact.get("result"), source_path); target = _json_pointer_value(data, target_path)
            except (KeyError, ValueError): add(f"{b_suffix}:unresolved", b_pointer); continue
            if _contains_redaction(source) or _contains_redaction(target): add(f"{b_suffix}:redacted", b_pointer)
            elif not _canonical_equal(source, target): add(f"{b_suffix}:mismatch", b_pointer)
            targets.setdefault(producer, set()).add(target_path)

    required_producers = _required_producers(data)
    for producer in sorted(required_producers - set(captured)): problems.append((f"evidence_producer_missing:{producer}", "/evidence_calls"))
    required_targets = _required_targets(data)
    for producer in sorted(required_producers):
        if producer == "market_sessions":
            if not market_ids: problems.append(("market_session_evidence_tool_call_ids_required", "/market_sessions"))
            elif not market_ids.issubset(captured.get(producer, set())): problems.append(("market_session_evidence_tool_call_id_unresolved", "/market_sessions"))
            continue
        missing = required_targets.get(producer, set()) - targets.get(producer, set())
        for path in sorted(missing): problems.append((f"evidence_projection_missing:{producer}:{path}", path))
    return problems


MAX_OBSERVATION_SKEW_SECONDS = 15 * 60

def _check_market_sessions(data):
    value = data.get("market_sessions")
    if not isinstance(value, dict): return []
    observed_at = _ts(value.get("observed_at"))
    expected = _ts(_effective_as_of(data))
    if (observed_at is not None and expected is not None and abs((observed_at - expected).total_seconds()) > MAX_OBSERVATION_SKEW_SECONDS):
        return [("market_sessions_observed_at_mismatch", "/market_sessions/observed_at")]
    return []


MARKET_SCOUT_BUDGET_FIELDS = ("specialist_investigations", "external_searches", "deep_dives", "opportunity_updates")

def _selected_specialist_count(data):
    agenda = data.get("research_agenda")
    candidates = agenda.get("candidates") if isinstance(agenda, dict) else None
    if not isinstance(candidates, list): return 0
    return sum(1 for row in candidates if isinstance(row, dict) and row.get("selected") is True)

def _derived_market_scout_usage(data):
    report = data.get("market_scout_report")
    report = report if isinstance(report, dict) else {}
    calls = report.get("tool_calls")
    calls = calls if isinstance(calls, list) else []
    kinds = {}
    for call in calls:
        if isinstance(call, dict):
            kind = _text_any(call.get("kind"))
            kinds[kind] = kinds.get(kind, 0) + 1
    updates = data.get("opportunity_updates")
    updates = updates if isinstance(updates, list) else []
    opportunity_ids = { _text_any(row.get("opportunity_id")) for row in updates if isinstance(row, dict) and _text_any(row.get("opportunity_id")) }
    return {"specialist_investigations": _selected_specialist_count(data), "external_searches": kinds.get("external_search", 0), "deep_dives": kinds.get("deep_dive", 0), "opportunity_updates": len(opportunity_ids)}

def _check_market_scout(data, *, validation_now):
    report = data.get("market_scout_report")
    if not isinstance(report, dict): return []
    problems = []
    calls = report.get("tool_calls")
    if isinstance(calls, list):
        for index, call in enumerate(calls):
            if not isinstance(call, dict): continue
            compact = _compact_call_values(call)
            for problem in _validate_tool_call_provenance(compact, validation_now=validation_now):
                problems.append((f"market_scout_tool_provenance_invalid:{index}:{problem}", f"/market_scout_report/tool_calls/{index}"))
    budget = report.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    usage = _derived_market_scout_usage(data)
    exceeded = sorted(field for field in MARKET_SCOUT_BUDGET_FIELDS if isinstance(budget.get(field), int) and not isinstance(budget.get(field), bool) and usage[field] > int(budget[field]))
    variance = report.get("budget_variance")
    pointer = "/market_scout_report/budget_variance"
    if not exceeded:
        if variance is not None: problems.append(("market_scout_budget_variance_invalid:unexpected", pointer))
    elif not isinstance(variance, dict): problems.append(("market_scout_budget_variance_invalid:required", pointer))
    else:
        if set(variance) != {"exceeded", "rationale"}: problems.append(("market_scout_budget_variance_invalid:fields", pointer))
        declared = variance.get("exceeded")
        if (not isinstance(declared, list) or sorted(map(_text_any, declared)) != exceeded):
            problems.append(("market_scout_budget_variance_invalid:exceeded", pointer))
        rationale = variance.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip(): problems.append(("market_scout_budget_variance_invalid:rationale", pointer))
    return problems


def _portfolio_risk_references(data):
    snapshot = data.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    references = set()
    if snapshot: references.add("portfolio:account")
    for field in ("cash", "total_cash_value", "balances"):
        if field in snapshot: references.add("portfolio:cash")
    for field, reference in (("positions", "portfolio:positions"), ("open_orders", "portfolio:open_orders"), ("order_instructions", "portfolio:instructions")):
        if field in snapshot: references.add(reference)
    for position in snapshot.get("positions") or ():
        if not isinstance(position, dict): continue
        identifier_fields = ("symbol", "contract_id_ex", "contract_id", "conid", "contractId")
        for field in identifier_fields:
            value = _text_any(position.get(field))
            if value: references.add(f"position:{value}".casefold())
        if _text_any(position.get("asset_class")).upper() == "STK" and not any(_text(position.get(f)) for f in identifier_fields):
            d = _text_any(position.get("contract_description"))
            if d: references.add(f"position:{d}".casefold())
    return {r.casefold() for r in references}

def _check_research_allocation(data):
    agenda = data.get("research_agenda")
    if not isinstance(agenda, dict): return []
    problems = []
    portfolio_references = _portfolio_risk_references(data)
    candidates = agenda.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict): continue
        pointer = f"/research_agenda/candidates/{index}/portfolio_risk_ref"
        ref = _text_any(candidate.get("portfolio_risk_ref"))
        if ref and ref.casefold() not in portfolio_references: problems.append((f"research_allocation_candidate_invalid:{index}:portfolio_risk_ref_unresolved", pointer))

    plan = agenda.get("allocation_plan")
    report = data.get("market_scout_report")
    budget = report.get("budget") if isinstance(report, dict) else None
    if isinstance(plan, dict) and isinstance(budget, dict):
        specialist_budget = budget.get("specialist_investigations")
        categories = ("new_opportunity", "existing_opportunity", "portfolio_risk", "follow_up")
        planned = [plan.get(c) for c in categories]
        if (isinstance(specialist_budget, int) and not isinstance(specialist_budget, bool) and all(isinstance(v, int) and not isinstance(v, bool) for v in planned) and sum(planned) > specialist_budget):
            problems.append(("research_allocation_plan_invalid:exceeds_specialist_investigations", "/research_agenda/allocation_plan"))
    return problems


def _check_decision_repetition_refs(data):
    decision = data.get("decision")
    if not isinstance(decision, dict): return []
    review = decision.get("repetition_review")
    if not isinstance(review, dict) or _text(review.get("disposition")) != "new_evidence": return []
    current_refs = _current_evidence_refs(data)
    evidence_delta = review.get("evidence_delta")
    if not isinstance(evidence_delta, list): return []
    finding_ids = {r[8:] for r in current_refs if r[:8] == "finding:"}
    bare_ids = {r.split(":", 1)[1] for r in current_refs}
    problems = []
    for index, ref in enumerate(evidence_delta):
        text = _text(ref)
        if text in current_refs or text in bare_ids: continue
        if not finding_ids: problems.append((f"decision_repetition_evidence_ref_invalid:{index}", f"/decision/repetition_review/evidence_delta/{index}"))
    return problems

_EVIDENCE_REF = re.compile(r"^(stage|finding):[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_MAX_REF_CHARS = 160

def _check_learning_dispositions(data):
    rows = data.get("learning_stage_dispositions")
    if not isinstance(rows, list): return []
    current_refs = _current_evidence_refs(data)
    problems = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict): continue
        evidence = row.get("evidence")
        if not isinstance(evidence, list): continue
        for ref_index, raw_ref in enumerate(evidence):
            ref = _text(raw_ref)
            if not ref or len(ref) > _MAX_REF_CHARS or _EVIDENCE_REF.fullmatch(ref) is None:
                problems.append((f"learning_disposition_evidence_invalid:{index}:invalid_ref:{ref_index}", f"/learning_stage_dispositions/{index}/evidence/{ref_index}"))
                continue
            if ref not in current_refs:
                problems.append((f"learning_disposition_evidence_invalid:{index}:dangling_ref:{ref}", f"/learning_stage_dispositions/{index}/evidence/{ref_index}"))
    return problems

# schedule_ledger.py
INTERVENTIONS = frozenset({"none", "operator", "automation"})
TRIGGERS = frozenset({"scheduled", "manual", "recovery"})

# Fallback SCHEDULE.json; FEEDBACK.schedule_contract wins if present.
SCHEDULE_CONTRACT_FALLBACK = {"task_id": "Sovereign Research IBKR hourly v2", "anchor_at": "2026-09-20T12:57:00+00:00", "cadence_minutes": 60, "grace_minutes": 15}

def _schedule_contract(feedback):
    if isinstance(feedback, dict):
        contract = feedback.get("schedule_contract")
        if isinstance(contract, dict) and {"anchor_at", "cadence_minutes", "grace_minutes"} <= set(contract): return contract
    return SCHEDULE_CONTRACT_FALLBACK

def _expected_slot_for_started_at(contract, started_at):
    anchor = _aware(contract.get("anchor_at"))
    if anchor is None: return None
    cadence = timedelta(minutes=int(contract["cadence_minutes"]))
    grace = timedelta(minutes=int(contract["grace_minutes"]))
    if started_at < anchor: return anchor if anchor - started_at <= grace else None
    slot_number = int((started_at - anchor).total_seconds() // cadence.total_seconds())
    preceding = anchor + slot_number * cadence
    following = preceding + cadence
    if following - started_at <= grace: return following
    return preceding

def _check_schedule_context(data, *, feedback):
    context = data.get("schedule_context")
    if not isinstance(context, dict): return []
    problems = []
    if context.get("intervention") not in INTERVENTIONS: problems.append(("schedule_context_intervention", "/schedule_context/intervention"))
    if context.get("trigger") not in TRIGGERS: problems.append(("schedule_context_trigger", "/schedule_context/trigger"))
    contract = _schedule_contract(feedback)
    expected = _aware(context.get("expected_slot"))
    started = _aware(context.get("started_at"))
    if expected is not None and started is not None:
        derived = _expected_slot_for_started_at(contract, started)
        if derived is not None and expected != derived: problems.append(("schedule_context_expected_slot_mismatch", "/schedule_context/expected_slot"))
    return problems


def _check_research_tool_provenance(data, *, validation_now):
    problems = []
    for r_index, row in enumerate(data.get("research") or ()):
        if not isinstance(row, dict): continue
        calls = row.get("tool_calls")
        if not isinstance(calls, list): continue
        for c_index, call in enumerate(calls):
            if not isinstance(call, dict): continue
            compact = _compact_call_values(call)
            for problem in _validate_tool_call_provenance(compact, validation_now=validation_now):
                problems.append((f"tool_provenance_invalid:{r_index}:{c_index}:{problem}", f"/research/{r_index}/tool_calls/{c_index}"))
    return problems

def _fix_for(base_code):
    return _FIXES.get(base_code, "See runtime/host_feedback.py.")

_FIXES = {
    "web_sources_url_refs_mismatch": "Make source_refs (url/link) match web_sources.",
    "portfolio_risk_ref_unresolved": "Use portfolio:account/cash/positions/open_orders/instructions or position:<sym>.",
    "semantic_tool_call_missing": "Needs tool_call_id/kind/tool/action/arguments/result/observed_at.",
    "semantic_evidence_target_missing": "Add projection {extractor:'json_pointer_v1', bindings:[...]} or host_summary.",
    "semantic_capture_origin": "capture_origin: direct_connector_response/host_transcribed_response/host_summary.",
    "evidence_call_invalid": "Check producer/tool/tool_call_id and binding source_path vs target_path.",
    "semantic_tool_call_id_conflict": "Two calls reuse one tool_call_id with different bodies; use a fresh id.",
    "schedule_context_intervention": "intervention: none/operator/automation",
    "research_allocation_plan_invalid": "allocation_plan counts must not exceed budget.specialist_investigations.",
    "market_scout_tool_provenance_invalid": "Fix web_sources (published_at<=retrieved_at, url in source_refs).",
    "evidence_projection_missing": "Add a call/projection whose result has this required snapshot target.",
    "semantic_selected_specialist_mismatch": "Selected candidate_id must equal a research[].specialist_stage_id.",
    "market_scout_budget_variance_invalid": "Raise the budget or add budget_variance {exceeded, rationale}.",
    "learning_disposition_evidence_invalid": "Ref must be stage:<id> (core/selected) or finding:<id> here.",
    "market_sessions_observed_at_mismatch": "observed_at must be within 15 minutes of as_of.",
    "decision_repetition_evidence_ref_invalid": "evidence_delta ref must be stage:<id> or finding:<id> here.",
    "schedule_context_expected_slot_mismatch": "expected_slot must match started_at/anchor_at/cadence/grace.",
    "tool_provenance_invalid": "Fix this research[] tool call's provenance (see detail suffix).",
    "schedule_context_trigger": "trigger: scheduled/manual/recovery",
}

def _base_code(code):
    prefix = code.split(":", 1)[0]
    return {"research_allocation_candidate_invalid": "portfolio_risk_ref_unresolved"}.get(prefix, prefix)

def preflight(candidate_text, feedback_text=None):
    try: candidate = json.loads(candidate_text)
    except (TypeError, ValueError) as exc: return {"ok": False, "problems": [{"code": "invalid_json", "where": "/", "fix": f"invalid JSON: {exc}"}]}
    if not isinstance(candidate, dict): return {"ok": False, "problems": [{"code": "invalid_json", "where": "/", "fix": "Top-level must be a JSON object."}]}

    feedback = None
    if feedback_text:
        try: feedback = json.loads(feedback_text)
        except (TypeError, ValueError): feedback = None
        if not isinstance(feedback, dict): feedback = None

    validation_now = datetime.now(timezone.utc)
    _normalize_stale_specialist_stage_ids(candidate)
    raw = [(code, pointer) for code, pointer, _detail in _probe_structural(candidate)]
    raw.extend(_check_evidence_coverage(candidate, validation_now=validation_now))
    raw.extend(_check_market_sessions(candidate))
    raw.extend(_check_market_scout(candidate, validation_now=validation_now))
    raw.extend(_check_research_allocation(candidate))
    raw.extend(_check_decision_repetition_refs(candidate))
    raw.extend(_check_learning_dispositions(candidate))
    raw.extend(_check_schedule_context(candidate, feedback=feedback))
    raw.extend(_check_research_tool_provenance(candidate, validation_now=validation_now))

    seen, problems = set(), []
    for code, pointer in raw:
        key = (code, pointer)
        if key in seen: continue
        seen.add(key)
        problems.append({"code": code, "where": pointer, "fix": _fix_for(_base_code(code))})
    problems.sort(key=lambda p: (p["where"], p["code"]))
    return {"ok": not problems, "problems": problems}

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: preflight.py candidate.json [FEEDBACK.json]", file=sys.stderr)
        return 2
    candidate_text = open(argv[0], encoding="utf-8").read()
    feedback_text = open(argv[1], encoding="utf-8").read() if len(argv) > 1 else None
    print(json.dumps(preflight(candidate_text, feedback_text), indent=1))
    return 0

if __name__ == "__main__": raise SystemExit(main())

# NOTES: research_direction_committed_question_unaddressed and decision_repetition's
# "required" gate need a FEEDBACK.json journal projection this profile never produced;
# validate_capture and market_scout stage/candidate-identity checks yield no requested code.
