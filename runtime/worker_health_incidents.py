"""Durable worker health incident tracking via audit journal.

Detects and persistently records worker failures and stale records without
hardcoded thresholds. Uses explicit worker status codes and record expiry
times to determine incident state transitions.

Replay capability: processes all historical worker records chronologically
to capture transient failures even if already recovered by one host cycle.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping

from .timestamps import parse_iso_timestamp

INCIDENT_SCHEMA_VERSION = 1
FAILURE_STATUSES = frozenset({
    "quota_exhausted",
    "auth_error",
    "model_error",
    "configuration_error",
})
RECOVERY_STATUSES = frozenset({
    "completed",
    "no_work",
})


def safe_error_code(record: Mapping[str, Any]) -> str | None:
    """Extract safe error code from record, filtering sensitive details."""
    if record.get("status") not in FAILURE_STATUSES:
        return None
    error = record.get("error")
    if isinstance(error, Mapping):
        if (
            record.get("status") == "configuration_error"
            and error.get("detail_code") == "opportunity_ledger_truncated"
        ):
            return "opportunity_ledger_truncated"
        code = str(error.get("code", "")).strip()
        if code and code in FAILURE_STATUSES:
            return code
    return None


def get_known_incident_ids(
    journal_records: list[dict[str, Any]],
) -> set[str]:
    """Load all known worker health incident IDs from audit journal.

    Returns set of all incident_ids, both active and resolved. Used for
    replay idempotency: if an incident was already opened/resolved in
    prior cycles, don't recreate it when replaying same records.
    """
    known_ids = set()
    for record in journal_records:
        if record.get("record_type") != "worker_health_incident":
            continue
        payload = record.get("payload", {})
        incident_id = payload.get("incident_id", "")
        if incident_id:
            known_ids.add(incident_id)
    return known_ids


def get_active_incidents(
    journal_records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Load active worker health incidents from audit journal.

    Returns dict mapping incident_id to incident record. Incident is active
    if its most recent event (by journal append order) is an 'open' type.

    Tracks per incident_id, not per worker_id, so multiple incidents per
    worker are tracked independently. Append ordering cannot erase an active
    incident of a different type (e.g., auth_error open followed by
    model_error open remain both active even if auth_error resolve follows).
    """
    active = {}
    incident_to_latest: dict[str, tuple[int, dict[str, Any]]] = {}

    for index, record in enumerate(journal_records):
        if record.get("record_type") != "worker_health_incident":
            continue
        payload = record.get("payload", {})
        incident_id = payload.get("incident_id", "")
        if not incident_id:
            continue

        entry = (index, record)
        if incident_id not in incident_to_latest:
            incident_to_latest[incident_id] = entry
        else:
            existing_idx, _ = incident_to_latest[incident_id]
            if index > existing_idx:
                incident_to_latest[incident_id] = entry

    for incident_id_key, (_, record) in incident_to_latest.items():
        payload = record.get("payload", {})
        if payload.get("event_type") == "open":
            active[incident_id_key] = payload

    return active


def compute_incident_transitions(
    worker_alerts: list[dict[str, Any]],
    active_incidents: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute incident open/resolve transitions based on current alerts.

    worker_alerts: list of alert dicts with keys:
      - worker_id, condition (status/stale), source_record_id, observed_at,
        expires_at, error_code (optional)
    active_incidents: dict mapping incident_id to active incident payload

    Returns (incidents_to_open, incidents_to_resolve) where each is a list
    of incident payloads ready for audit journal.
    """
    # Build current alert index by worker_id + condition
    current_alerts = {}
    for alert in worker_alerts:
        worker_id = alert.get("worker_id", "")
        condition = alert.get("condition", "")
        key = (worker_id, condition)
        current_alerts[key] = alert

    # Detect which active incidents should be resolved
    incidents_to_resolve = []
    for incident_id, incident in active_incidents.items():
        worker_id = incident.get("worker_id", "")
        condition = incident.get("condition", "")
        key = (worker_id, condition)
        # If no matching alert exists, resolve the incident
        if key not in current_alerts:
            incidents_to_resolve.append({
                "schema_version": INCIDENT_SCHEMA_VERSION,
                "incident_id": incident_id,
                "worker_id": worker_id,
                "condition": condition,
                "event_type": "resolve",
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            })

    # Detect which current alerts need new incidents opened
    incidents_to_open = []
    for alert in worker_alerts:
        worker_id = alert.get("worker_id", "")
        condition = alert.get("condition", "")
        # Check if an active incident already exists for this worker+condition
        existing = next(
            (
                incident for incident in active_incidents.values()
                if (
                    incident.get("worker_id") == worker_id
                    and incident.get("condition") == condition
                    and incident.get("event_type") == "open"
                )
            ),
            None,
        )
        if existing is None:
            # Generate a deterministic but unique incident_id
            from hashlib import sha256
            incident_seed = f"{worker_id}:{condition}:{alert.get('source_record_id', '')}"
            incident_hash = sha256(incident_seed.encode()).hexdigest()[:12]
            incident_id = f"worker-incident-{incident_hash}"

            payload = {
                "schema_version": INCIDENT_SCHEMA_VERSION,
                "incident_id": incident_id,
                "worker_id": worker_id,
                "condition": condition,
                "event_type": "open",
                "source_record_id": alert.get("source_record_id", ""),
                "observed_at": alert.get("observed_at"),
                "expires_at": alert.get("expires_at"),
            }
            if alert.get("error_code"):
                payload["error_code"] = alert["error_code"]

            incidents_to_open.append(payload)

    return incidents_to_open, incidents_to_resolve


def replay_worker_incidents_from_records(
    worker_records: dict[str, list[dict[str, Any]]],
    known_incident_ids: set[str] | None = None,
    active_incident_ids: set[str] | None = None,
    active_incidents: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Replay historical worker records chronologically to capture transient incidents.

    Reconstructs incident state from scratch by processing all records per worker
    in strict chronological order (oldest first). Does NOT pre-populate state from
    active_incidents; instead builds ephemeral state as we go. This ensures:
    - An old completed record cannot resolve a newer unencountered failure
    - An old record cannot resolve a later stale incident

    Processes all records per worker in order:
    - Explicit failures: open incidents per condition (if unknown)
    - Condition transitions: resolve OTHER active-ephemeral failures, open new
    - Recovery (completed/no_work): resolve only ephemeral-active incidents
    - Each new record resolves active stale if it represents new info

    After chronological replay, reconciles active incidents not found in records
    (pruned by retention) against latest records per worker.

    After replay and reconciliation, stale detection is applied separately to
    latest records only (via apply_stale_detection_latest_only).

    Deterministic incident_id based on worker_id:condition:observed_at:record_id.
    Resolves use recovery record's observed_at (not datetime.now()).
    Idempotent: skips re-opening incidents already in known_incident_ids.

    Events are returned in chronological order by parsed source_record observed_at
    timestamp. If parsing fails, event remains active/unemitted (fail-safe).

    Args:
        worker_records: dict mapping worker_id to list of records
        known_incident_ids: set of all known incident IDs (open + resolved) for
          idempotency. If None, all incidents are treated as new (less robust).
        active_incident_ids: set of currently active incident IDs. When a known
          incident ID is encountered in source record, add to ephemeral state
          so later recovery/transition records can resolve it.

    Returns: list of incident events (open and resolve) in chronological order
    """
    if known_incident_ids is None:
        known_incident_ids = set()
    if active_incident_ids is None:
        active_incident_ids = set()
    if active_incidents is None:
        active_incidents = {}

    events = []  # Single ordered list of all incident events
    active_by_condition = {
        (
            str(incident.get("worker_id", "")),
            str(incident.get("condition", "")),
        ): incident_id
        for incident_id, incident in active_incidents.items()
        if incident.get("event_type") == "open"
    }

    # Ephemeral state during replay: (worker_id, condition) -> incident_id
    # Only contains incidents that have been opened during THIS replay
    # OR pre-existing active incidents that are being re-encountered
    ephemeral_open: dict[tuple[str, str], str] = {}
    # Track which incident_ids are currently active in ephemeral state
    ephemeral_active_incident_ids: set[str] = set()
    # Track which active incidents we've already added to ephemeral state
    # so we don't add them twice
    added_active: set[str] = set()

    # Track all encountered incident IDs and their source records during replay
    # Format: incident_id -> (worker_id, condition, source_record_id, observed_at)
    encountered_incidents: dict[str, tuple[str, str, str, str]] = {}

    # Process each worker's records chronologically
    for worker_id, records in worker_records.items():
        # Sort by observed_at ascending (oldest first)
        sorted_records = sorted(
            records,
            key=lambda r: r.get("observed_at", ""),
        )

        for record in sorted_records:
            status = record.get("status", "")
            observed_at = record.get("observed_at")
            expires_at = record.get("expires_at")
            record_id = record.get("record_id", "")

            # Explicit failure: open incident, resolve other active failures
            if status in FAILURE_STATUSES:
                condition = status
                key = (worker_id, condition)

                # Generate deterministic incident_id for this failure
                incident_seed = f"{worker_id}:{condition}:{observed_at}:{record_id}"
                incident_hash = sha256(incident_seed.encode()).hexdigest()[:12]
                incident_id = f"worker-incident-{incident_hash}"

                # If incident is known AND currently active, add to ephemeral state
                # so recovery/transition records can resolve it
                if incident_id in active_incident_ids and incident_id not in added_active:
                    ephemeral_open[key] = incident_id
                    ephemeral_active_incident_ids.add(incident_id)
                    added_active.add(incident_id)

                # Skip opening if already created/resolved in prior cycles
                if incident_id not in known_incident_ids:
                    existing_active_id = active_by_condition.get(key)
                    if (
                        existing_active_id is not None
                        and existing_active_id not in {
                            event["incident_id"]
                            for event in events
                            if event.get("event_type") == "resolve"
                        }
                    ):
                        ephemeral_open[key] = existing_active_id
                        ephemeral_active_incident_ids.add(
                            existing_active_id
                        )
                        continue
                    # Resolve all OTHER active explicit-failure conditions
                    # (only those in ephemeral state at THIS chronological point)
                    other_keys = [
                        k for k in ephemeral_open
                        if k[0] == worker_id and k[1] != condition and k[1] in FAILURE_STATUSES
                    ]
                    for other_key in other_keys:
                        other_incident_id = ephemeral_open[other_key]
                        other_condition = other_key[1]

                        # Only resolve if not already resolved
                        existing = next(
                            (
                                inc for inc in events
                                if (
                                    inc["incident_id"] == other_incident_id
                                    and inc["event_type"] == "resolve"
                                )
                            ),
                            None,
                        )
                        if existing is None:
                            payload = {
                                "schema_version": INCIDENT_SCHEMA_VERSION,
                                "incident_id": other_incident_id,
                                "worker_id": worker_id,
                                "condition": other_condition,
                                "event_type": "resolve",
                                "source_record_id": record_id,
                                "resolved_at": observed_at,
                            }
                            events.append(payload)
                            ephemeral_active_incident_ids.discard(other_incident_id)
                            del ephemeral_open[other_key]

                    # Resolve any active stale incident (newer record encountered)
                    stale_key = (worker_id, "stale")
                    if stale_key in ephemeral_open:
                        stale_incident_id = ephemeral_open[stale_key]
                        existing = next(
                            (
                                inc for inc in events
                                if (
                                    inc["incident_id"] == stale_incident_id
                                    and inc["event_type"] == "resolve"
                                )
                            ),
                            None,
                        )
                        if existing is None:
                            payload = {
                                "schema_version": INCIDENT_SCHEMA_VERSION,
                                "incident_id": stale_incident_id,
                                "worker_id": worker_id,
                                "condition": "stale",
                                "event_type": "resolve",
                                "source_record_id": record_id,
                                "resolved_at": observed_at,
                            }
                            events.append(payload)
                            ephemeral_active_incident_ids.discard(stale_incident_id)
                            del ephemeral_open[stale_key]

                    # Open new incident (only if not already in ephemeral state)
                    if key not in ephemeral_open:
                        payload = {
                            "schema_version": INCIDENT_SCHEMA_VERSION,
                            "incident_id": incident_id,
                            "worker_id": worker_id,
                            "condition": condition,
                            "event_type": "open",
                            "source_record_id": record_id,
                            "observed_at": observed_at,
                            "expires_at": expires_at,
                        }
                        # Use safe_error_code to extract from nested error.code
                        code = safe_error_code(record)
                        if code:
                            payload["error_code"] = code

                        events.append(payload)
                        ephemeral_open[key] = incident_id
                        ephemeral_active_incident_ids.add(incident_id)
                        known_incident_ids.add(incident_id)

            # Recovery: close explicit-failure incidents for this worker
            elif status in RECOVERY_STATUSES:
                # Close all explicit-failure conditions (only ephemeral-active)
                keys_to_close = [
                    k for k in ephemeral_open
                    if k[0] == worker_id and k[1] in FAILURE_STATUSES
                ]
                for key in keys_to_close:
                    incident_id = ephemeral_open[key]
                    condition = key[1]

                    # Only resolve if not already resolved
                    existing = next(
                        (
                            inc for inc in events
                            if (
                                inc["incident_id"] == incident_id
                                and inc["event_type"] == "resolve"
                            )
                        ),
                        None,
                    )
                    if existing is None:
                        payload = {
                            "schema_version": INCIDENT_SCHEMA_VERSION,
                            "incident_id": incident_id,
                            "worker_id": worker_id,
                            "condition": condition,
                            "event_type": "resolve",
                            "source_record_id": record_id,
                            "resolved_at": observed_at,
                        }
                        events.append(payload)
                        ephemeral_active_incident_ids.discard(incident_id)
                        del ephemeral_open[key]

                # Also resolve any active stale incident
                stale_key = (worker_id, "stale")
                if stale_key in ephemeral_open:
                    stale_incident_id = ephemeral_open[stale_key]
                    existing = next(
                        (
                            inc for inc in events
                            if (
                                inc["incident_id"] == stale_incident_id
                                and inc["event_type"] == "resolve"
                            )
                        ),
                        None,
                    )
                    if existing is None:
                        payload = {
                            "schema_version": INCIDENT_SCHEMA_VERSION,
                            "incident_id": stale_incident_id,
                            "worker_id": worker_id,
                            "condition": "stale",
                            "event_type": "resolve",
                            "source_record_id": record_id,
                            "resolved_at": observed_at,
                        }
                        events.append(payload)
                        ephemeral_active_incident_ids.discard(stale_incident_id)
                        del ephemeral_open[stale_key]

    return _sorted_incident_events(events)


def _sorted_incident_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    valid_events = []
    for event in events:
        timestamp_field = (
            "observed_at"
            if event.get("event_type") == "open"
            else "resolved_at"
        )
        parsed = parse_iso_timestamp(event.get(timestamp_field))
        if parsed is None:
            continue
        valid_events.append((
            parsed,
            0 if event.get("event_type") == "resolve" else 1,
            str(event.get("incident_id", "")),
            event,
        ))
    valid_events.sort(key=lambda row: row[:3])
    return [row[3] for row in valid_events]



def apply_stale_detection_latest_only(
    worker_records: dict[str, list[dict[str, Any]]],
    active_incidents: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Handle stale detection after chronological replay.

    After replay_worker_incidents_from_records processes all historical
    records and builds ephemeral state, this function:
    1. Resolves active stale incidents if a strictly newer record exists
    2. Opens stale incidents only for latest records that are stale and have
       no active explicit failures

    A stale incident is resolved when:
    - A record exists with observed_at strictly later than the stale incident's
      source record's observed_at, AND
    - That newer record is the current latest for the worker

    A stale incident is NOT resolved when:
    - The same source record remains the latest (replay hasn't brought new data)
    - An older record is the latest (impossible if replay is correct)

    Args:
        worker_records: dict mapping worker_id to list of all records
        active_incidents: active incidents dict (from journal state before replay)
        events: combined list of incident events from replay (mutated)

    Returns: updated events list
    """
    # Find latest valid (non-future) record per worker
    latest_valid_record: dict[str, dict[str, Any]] = {}
    for worker_id, records in worker_records.items():
        for record in sorted(records, key=lambda r: r.get("observed_at", ""), reverse=True):
            is_future = record.get("future", False)
            if not is_future:
                latest_valid_record[worker_id] = record
                break

    # Resolve active stale incidents if newer records exist
    for incident_id, incident in active_incidents.items():
        if incident.get("event_type") != "open" or incident.get("condition") != "stale":
            continue

        worker_id = incident.get("worker_id", "")
        incident_source_record_id = incident.get("source_record_id", "")
        incident_observed_at = incident.get("observed_at", "")

        # Skip if already being resolved in this batch
        if any(
            inc.get("incident_id") == incident_id and inc.get("event_type") == "resolve"
            for inc in events
        ):
            continue

        # Check if a strictly newer record exists for this worker
        latest = latest_valid_record.get(worker_id)
        if latest is None:
            continue

        latest_observed_at_str = latest.get("observed_at", "")
        incident_observed_at_str = incident.get("observed_at", "")
        latest_record_id = latest.get("record_id", "")

        # Parse both timestamps for proper comparison (handles Z vs +00:00 etc)
        latest_dt = parse_iso_timestamp(latest_observed_at_str)
        incident_dt = parse_iso_timestamp(incident_observed_at_str)

        # Skip if timestamp parsing failed
        if latest_dt is None or incident_dt is None:
            continue

        # Resolve only if latest has a strictly newer observed_at (as datetime)
        if latest_dt > incident_dt:
            payload = {
                "schema_version": INCIDENT_SCHEMA_VERSION,
                "incident_id": incident_id,
                "worker_id": worker_id,
                "condition": "stale",
                "event_type": "resolve",
                "source_record_id": latest_record_id,
                "resolved_at": latest_observed_at_str,
            }
            events.append(payload)

    # Resolve active explicit incidents whose source records were pruned.
    for incident_id, incident in active_incidents.items():
        condition = str(incident.get("condition", ""))
        if (
            incident.get("event_type") != "open"
            or condition not in FAILURE_STATUSES
            or any(
                event.get("incident_id") == incident_id
                and event.get("event_type") == "resolve"
                for event in events
            )
        ):
            continue
        worker_id = str(incident.get("worker_id", ""))
        latest = latest_valid_record.get(worker_id)
        if latest is None:
            continue
        incident_dt = parse_iso_timestamp(incident.get("observed_at"))
        latest_dt = parse_iso_timestamp(latest.get("observed_at"))
        if (
            incident_dt is None
            or latest_dt is None
            or latest_dt <= incident_dt
        ):
            continue
        latest_status = str(latest.get("status", ""))
        if (
            latest_status in RECOVERY_STATUSES
            or (
                latest_status in FAILURE_STATUSES
                and latest_status != condition
            )
        ):
            events.append({
                "schema_version": INCIDENT_SCHEMA_VERSION,
                "incident_id": incident_id,
                "worker_id": worker_id,
                "condition": condition,
                "event_type": "resolve",
                "source_record_id": latest.get("record_id", ""),
                "resolved_at": latest.get("observed_at"),
            })

    # Open stale incidents only for latest records
    # First, collect incidents being resolved in this batch (to exclude from active state)
    incidents_resolving_ids = set(
        inc["incident_id"] for inc in events
        if inc.get("event_type") == "resolve"
    )

    # Build map of active non-stale incidents per worker AFTER this batch resolves
    active_explicit_per_worker: dict[str, set[str]] = {}
    for incident_id, incident in active_incidents.items():
        if (
            incident.get("event_type") == "open"
            and incident.get("condition") != "stale"
            and incident_id not in incidents_resolving_ids
        ):
            worker_id = incident.get("worker_id", "")
            if worker_id not in active_explicit_per_worker:
                active_explicit_per_worker[worker_id] = set()
            active_explicit_per_worker[worker_id].add(incident_id)

    # Also add incidents being opened in this batch (unless they're also being resolved)
    for incident in events:
        if incident.get("event_type") == "open" and incident.get("condition") != "stale":
            incident_id = incident.get("incident_id")
            # Skip if this incident is also being resolved in this batch (net zero active)
            if incident_id not in incidents_resolving_ids:
                worker_id = incident.get("worker_id", "")
                if worker_id not in active_explicit_per_worker:
                    active_explicit_per_worker[worker_id] = set()
                active_explicit_per_worker[worker_id].add(incident_id)

    # Check each latest record for staleness
    for worker_id, latest in latest_valid_record.items():
        # Skip if active explicit failures exist for this worker
        if active_explicit_per_worker.get(worker_id):
            continue

        # Check if record is stale (comparing expires_at to observed_at as proxy)
        is_stale = latest.get("stale", False)
        if not is_stale:
            continue

        # Check if we're already opening a stale incident for this worker
        if any(
            inc.get("worker_id") == worker_id
            and inc.get("condition") == "stale"
            and inc.get("event_type") == "open"
            for inc in events
        ):
            continue

        # Check if an active stale incident already exists
        if any(
            inc.get("worker_id") == worker_id
            and inc.get("condition") == "stale"
            and inc.get("event_type") == "open"
            and inc.get("incident_id") not in incidents_resolving_ids
            for inc in active_incidents.values()
        ):
            continue

        # Open new stale incident for this latest record
        source_record_id = latest.get("record_id", "")
        observed_at = latest.get("observed_at")
        expires_at = latest.get("expires_at")

        incident_seed = f"{worker_id}:stale:{observed_at}:{source_record_id}"
        incident_hash = sha256(incident_seed.encode()).hexdigest()[:12]
        incident_id = f"worker-incident-{incident_hash}"

        payload = {
            "schema_version": INCIDENT_SCHEMA_VERSION,
            "incident_id": incident_id,
            "worker_id": worker_id,
            "condition": "stale",
            "event_type": "open",
            "source_record_id": source_record_id,
            "observed_at": observed_at,
            "expires_at": expires_at,
        }
        events.append(payload)

    return _sorted_incident_events(events)
