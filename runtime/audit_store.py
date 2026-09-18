"""Durable append-only audit journal built on the runtime hash-chain format.

The journal is deliberately file-backed so a host can persist it in GitHub,
object storage, or another append-only medium. This module never submits orders.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - platform without POSIX advisory locks
    fcntl = None  # type: ignore[assignment]

from .cycle_receipt import validate_receipt
from .engine import canonical_json, make_record, verify_chain, dangling_causes


@contextmanager
def _exclusive_journal_lock(path: Path) -> Iterator[None]:
    """Serialize the read-tip / check-duplicate / append sequence.

    Appending was read-then-write with nothing in between: two writers both
    read the same last record, both computed the same prev_hash, and both
    appended. The chain forks, and because the journal is append-only the
    fork cannot be repaired by retrying. Verified before fixing -- two
    AuditJournal instances over one file produced duplicate prev_hash
    values.

    The lock is BLOCKING, unlike a scheduling guard where skipping a tick
    is correct backpressure. Here the second writer's record must still be
    written; it just has to be written after the first and chained onto it.

    Fails CLOSED when POSIX advisory locks are unavailable. Appending
    without serialization risks a fork that cannot be undone, so refusing
    to write is the safer failure. A read-only consumer is unaffected;
    only append takes the lock.
    """
    if fcntl is None:  # pragma: no cover - exercised only off POSIX
        raise RuntimeError(
            "journal_locking_unavailable: appending without an exclusive lock "
            "can fork an append-only hash chain irreversibly"
        )
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class AuditJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def append(self, *, record_id: str, record_type: str, agent: str,
               payload: dict[str, Any], caused_by: Sequence[str] = ()) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Everything from reading the tip to writing the new line happens
        # under one exclusive lock. Splitting them is what let two writers
        # pick the same parent and fork the chain.
        with _exclusive_journal_lock(self.path):
            records = self.read()
            if any(r.get("record_id") == record_id for r in records):
                raise ValueError(f"duplicate_record_id:{record_id}")
            prior = records[-1]["record_hash"] if records else None
            record = make_record(record_id, record_type, agent, payload, caused_by, prior)
            # One JSON object per line gives atomic append semantics for the
            # journal boundary; callers should use filesystem/object-store
            # durability as needed.
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def append_cycle_receipt(self, receipt: dict[str, Any], *,
                             agent: str = "sovereign-host",
                             caused_by: Sequence[str] = ()) -> dict[str, Any]:
        """Validate and append one host execution receipt to the causal chain."""
        errors = validate_receipt(receipt)
        # validate_receipt deliberately exempts a receipt with no declared
        # plan, because every receipt already in the journal predates the
        # field and the predecessor is validated before each cycle. That
        # exemption is for READING old records. Accepting a NEW one is a
        # different act, and a fresh receipt can declare its plan, so the
        # write path enforces what read-validation cannot.
        from .cycle_receipt import REQUIRED_STAGE_MODES
        if (str(receipt.get("mode", "")) in REQUIRED_STAGE_MODES
                and receipt.get("required_stages") is None):
            errors.append(
                f"required_stages_not_declared:{receipt.get('mode')}")
        if errors:
            raise ValueError("invalid_cycle_receipt:" + ",".join(errors))
        cycle_id = str(receipt["cycle_id"])
        return self.append(
            record_id=f"cycle-receipt:{cycle_id}",
            record_type="cycle_receipt",
            agent=agent,
            payload=dict(receipt),
            caused_by=caused_by,
        )

    def validate(self) -> dict[str, Any]:
        records = self.read()
        chain_errors = verify_chain(records)
        dangling = dangling_causes(records)
        return {
            "records": len(records),
            "chain_errors": chain_errors,
            "dangling_causes": dangling,
            "valid": not chain_errors and not dangling,
        }

    def export(self) -> list[dict[str, Any]]:
        return self.read()
