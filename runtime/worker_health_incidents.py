"""Durable worker health incident tracking via audit journal.

Detects and persistently records worker failures and stale records without
hardcoded thresholds. Uses explicit worker status codes and record expiry
times to determine incident state transitions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

INCIDENT_SCHEMA_VERSION = 1
FAILURE_STATUSES = frozenset({
    "quota_exhausted",
    "auth_error",
    "model_error",
    "configuration_error",
})


def safe_error_code(record: Mapping[str, Any]) -> str | None:
    """Extract safe error code from record, filtering sensitive details."""
    if record.get("status") not in FAILURE_STATUSES:
        return None
    error = record.get("error")
    if isinstance(error, Mapping):
        code = str(error.get("code", "")).strip()
        if code and code in FAILURE_STATUSES:
            return code
    return None


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
