import copy
import pathlib
import unittest

from .cycle_receipt import (
    ALLOWED_MODES,
    _ancestry_errors,
    build_receipt,
    latest_receipt_record,
    previous_receipt_status,
    receipt_hash,
    validate_audit_receipt_record,
    validate_receipt,
)
from .engine import hash_record, make_record


TS1 = "2026-09-16T06:00:00Z"
TS2 = "2026-09-16T06:05:00Z"


def sample_receipt():
    return build_receipt(
        cycle_id="cycle-20260916-0600",
        run_id="run-123",
        started_at=TS1,
        completed_at=TS2,
        mode="production",
        required_stages=["portfolio", "decision"],
        snapshot_id="ibkr-20260916-060001",
        stages=[
            {
                "stage_id": "portfolio",
                "agent_id": "portfolio-agent",
                "status": "completed",
                "execution_order": 1,
                "started_at": TS1,
                "completed_at": "2026-09-16T06:01:00Z",
                "tools_used": ["Interactive Brokers"],
            },
            {
                "stage_id": "decision",
                "agent_id": "decision-agent",
                "status": "blocked",
                "execution_order": 2,
                "started_at": "2026-09-16T06:01:00Z",
                "completed_at": TS2,
                "tools_used": ["GitHub"],
            },
        ],
        tools_used=["Interactive Brokers", "GitHub"],
        status="blocked",
        decision_status="blocked",
        self_improvement={"status": "locked", "mutation_ids": [], "gates": {"real_outcome": False}},
        host={"cognitive_execution_claim": "host-recorded actual stage execution"},
        blockers=["fresh account state unavailable"],
    )


def receipt_record(receipt):
    return make_record(
        f"cycle-receipt:{receipt['cycle_id']}",
        "cycle_receipt",
        "sovereign-host",
        receipt,
    )


class CycleReceiptTests(unittest.TestCase):
    def test_builds_hashed_receipt(self):
        receipt = sample_receipt()
        self.assertEqual(validate_receipt(receipt), [])
        self.assertEqual(receipt["receipt_hash"], receipt_hash(receipt))

    def test_schema_version_is_optional_and_hash_covered_when_present(self):
        legacy = sample_receipt()
        self.assertNotIn("host_input_schema_version", legacy)

        versioned = build_receipt(
            **{
                key: value
                for key, value in legacy.items()
                if key not in {"receipt_hash", "host_input_schema_version"}
            },
            host_input_schema_version=3,
        )
        self.assertEqual(versioned["host_input_schema_version"], 3)
        self.assertEqual(validate_receipt(versioned), [])
        versioned["host_input_schema_version"] = 2
        self.assertIn("receipt_hash_mismatch", validate_receipt(versioned))

    def test_retry_lineage_is_optional_and_hash_covered_when_present(self):
        legacy = sample_receipt()
        self.assertNotIn("corrects_candidate_id", legacy)

        first_attempt = build_receipt(
            **{
                key: value
                for key, value in legacy.items()
                if key not in {"receipt_hash", "corrects_candidate_id"}
            },
            corrects_candidate_id_declared=True,
        )
        self.assertIsNone(first_attempt["corrects_candidate_id"])

        corrected = build_receipt(
            **{
                key: value
                for key, value in legacy.items()
                if key not in {"receipt_hash", "corrects_candidate_id"}
            },
            corrects_candidate_id=(
                "prior.semantic.json@sha256:" + "a" * 64
            ),
        )
        self.assertEqual(validate_receipt(corrected), [])
        corrected["corrects_candidate_id"] = (
            "other.semantic.json@sha256:" + "b" * 64
        )
        self.assertIn("receipt_hash_mismatch", validate_receipt(corrected))

    def test_timezone_less_cycle_timestamps_are_refused(self):
        receipt = sample_receipt()
        receipt["started_at"] = "2026-09-16T06:00:00"
        receipt["completed_at"] = "2026-09-16T06:05:00"
        receipt.pop("receipt_hash", None)
        self.assertIn(
            "invalid_cycle_timestamps",
            validate_receipt(receipt),
        )

    def test_timezone_less_stage_timestamps_are_refused(self):
        receipt = sample_receipt()
        receipt["stages"][0]["started_at"] = "2026-09-16T06:00:00"
        receipt.pop("receipt_hash", None)
        self.assertIn(
            "invalid_stage_timestamps:portfolio",
            validate_receipt(receipt),
        )

    def test_tampering_is_rejected(self):
        receipt = sample_receipt()
        receipt["status"] = "completed"
        errors = validate_receipt(receipt)
        self.assertIn("receipt_hash_mismatch", errors)

    def test_missing_stage_is_rejected(self):
        receipt = sample_receipt()
        receipt["stages"][0].pop("tools_used")
        self.assertIn("stage_missing:tools_used", validate_receipt(receipt))

    def test_reordered_execution_is_rejected(self):
        receipt = sample_receipt()
        receipt["stages"][1]["execution_order"] = 3
        receipt.pop("receipt_hash", None)
        self.assertIn("execution_order_not_contiguous", validate_receipt(receipt))

    def test_failed_or_blocked_stages_are_recordable_without_claiming_success(self):
        receipt = sample_receipt()
        receipt["stages"][1]["status"] = "failed"
        receipt.pop("receipt_hash", None)
        receipt["receipt_hash"] = receipt_hash(receipt)
        self.assertEqual(validate_receipt(receipt), [])
        self.assertEqual(receipt["decision_status"], "blocked")

    def test_hash_changes_with_content(self):
        receipt = sample_receipt()
        original = receipt_hash(receipt)
        changed = copy.deepcopy(receipt)
        changed["blockers"].append("new blocker")
        self.assertNotEqual(original, receipt_hash(changed))

    def test_host_claim_is_required(self):
        receipt = sample_receipt()
        receipt["host"] = {}
        receipt.pop("receipt_hash", None)
        self.assertIn("missing_cognitive_execution_claim", validate_receipt(receipt))

    def test_audit_record_contract_requires_matching_record_id(self):
        receipt = sample_receipt()
        record = receipt_record(receipt)
        self.assertEqual(validate_audit_receipt_record(record), [])
        record["record_id"] = "cycle-receipt:wrong"
        self.assertIn("record_id_mismatch", validate_audit_receipt_record(record))

    def test_previous_receipt_missing_is_explicit_initial_state(self):
        state = previous_receipt_status([])
        self.assertTrue(state["ok"])
        self.assertEqual(state["status"], "missing_initial")

    def test_previous_receipt_must_verify_before_next_cycle(self):
        receipt = sample_receipt()
        record = receipt_record(receipt)
        state = previous_receipt_status([record])
        self.assertTrue(state["ok"])
        self.assertEqual(state["status"], "verified")
        receipt["decision_status"] = "wait"
        state = previous_receipt_status([record])
        self.assertFalse(state["ok"])
        self.assertEqual(state["status"], "invalid")
        self.assertIn("receipt_hash_mismatch", state["errors"])

    def test_previous_receipt_rejects_tampered_audit_record_id(self):
        receipt = sample_receipt()
        record = receipt_record(receipt)
        record["record_id"] = "cycle-receipt:wrong"
        state = previous_receipt_status([record])
        self.assertFalse(state["ok"])
        self.assertIn("record_id_mismatch", state["errors"])

    def test_previous_receipt_rejects_tampered_audit_record_hash(self):
        receipt = sample_receipt()
        record = receipt_record(receipt)
        record["record_hash"] = "tampered"
        state = previous_receipt_status([record])
        self.assertFalse(state["ok"])
        self.assertIn("audit_record_hash_mismatch", state["errors"])


if __name__ == "__main__":
    unittest.main()


class ReceiptAnchoringTests(unittest.TestCase):
    """A receipt must be anchored in the chain it claims to belong to.

    A record can be internally perfect -- valid receipt, correct
    receipt_hash, correct record_hash -- and still name a parent that does
    not exist. Verification checked only the record itself, so such a
    receipt was reported "verified" with ok=True and the next cycle
    proceeded on an execution record attached to no history.

    Not hypothetical: a malformed prev_hash orphaned six records in this
    journal while every one of them self-validated. Self-consistency is
    not ancestry.
    """

    def receipt(self, cycle_id):
        import inspect
        base = sample_receipt()
        kw = {k: base[k] for k in inspect.signature(build_receipt).parameters if k in base}
        kw["cycle_id"] = cycle_id
        return build_receipt(**kw)

    def record(self, cycle_id, prev_hash, created_at):
        return make_record(f"cycle-receipt:{cycle_id}", "cycle_receipt", "host",
                           self.receipt(cycle_id), [], prev_hash, created_at=created_at)

    def test_a_root_receipt_verifies(self):
        root = self.record("c1", None, "2026-01-01T00:00:00Z")
        self.assertEqual(previous_receipt_status([root])["status"], "verified")

    def test_a_correctly_chained_receipt_verifies(self):
        root = self.record("c1", None, "2026-01-01T00:00:00Z")
        child = self.record("c2", root["record_hash"], "2026-01-02T00:00:00Z")
        status = previous_receipt_status([root, child])
        self.assertEqual(status["status"], "verified")
        self.assertTrue(status["ok"])

    def test_a_receipt_naming_a_nonexistent_parent_is_refused(self):
        # The load-bearing case. This record self-validates completely.
        root = self.record("c1", None, "2026-01-01T00:00:00Z")
        dangling = self.record("c2", "f" * 64, "2026-01-02T00:00:00Z")
        self.assertEqual(validate_audit_receipt_record(dangling), [],
                         "fixture assumption: the record is internally valid")
        status = previous_receipt_status([root, dangling])
        self.assertEqual(status["status"], "invalid")
        self.assertFalse(status["ok"])
        # The wording moved with the mechanism. The dangling receipt is no
        # longer SELECTED as predecessor -- selection now prefers a receipt
        # that is on the chain -- so it is reported as skipped-and-unanchored
        # instead of failing its own ancestry walk. What must not change is
        # that it is refused and named, which is what this asserts.
        self.assertTrue(
            any("prev_hash_not_in_journal" in e or "unanchored_receipt_skipped" in e
                for e in status["errors"]), status["errors"])
        self.assertTrue(any("c2" in e for e in status["errors"]), status["errors"])

    def test_a_self_referential_parent_is_refused(self):
        # Two independent checks cover this: the ancestry check excludes the
        # record itself from the known-parent set, and any forged hash trips
        # audit_record_hash_mismatch. A LEGITIMATE self-parent would require
        # a sha256 fixed point, since prev_hash is part of the hashed content.
        # Mutation testing shows removing the identity guard breaks no test
        # for exactly that reason: it is defence in depth, not a gap.
        root = self.record("c1", None, "2026-01-01T00:00:00Z")
        forged = dict(root)
        forged["prev_hash"] = forged["record_hash"]
        status = previous_receipt_status([forged])
        self.assertEqual(status["status"], "invalid")
        self.assertIn("audit_record_hash_mismatch", status["errors"])

    def test_no_receipt_at_all_is_still_the_explicit_first_run_state(self):
        self.assertEqual(previous_receipt_status([])["status"], "missing_initial")


class PersistedReceiptHashTests(unittest.TestCase):
    """A persisted receipt must carry its own hash.

    build_receipt validates BEFORE stamping receipt_hash, so a receipt
    under construction legitimately has none. A receipt being persisted
    with none has no self-check at all, and a missing hash read exactly
    like a valid receipt.
    """

    def persisted(self, drop_hash=False):
        import inspect
        base = sample_receipt()
        kw = {k: base[k] for k in inspect.signature(build_receipt).parameters if k in base}
        payload = build_receipt(**kw)
        if drop_hash:
            payload = {k: v for k, v in payload.items() if k != "receipt_hash"}
        return make_record(f"cycle-receipt:{payload['cycle_id']}", "cycle_receipt",
                           "host", payload, [], None, created_at="2026-01-01T00:00:00Z")

    def test_a_persisted_receipt_with_its_hash_is_accepted(self):
        self.assertEqual(validate_audit_receipt_record(self.persisted()), [])

    def test_a_persisted_receipt_missing_its_hash_is_refused(self):
        self.assertIn("missing_receipt_hash",
                      validate_audit_receipt_record(self.persisted(drop_hash=True)))

    def test_a_blank_receipt_hash_is_refused(self):
        record = self.persisted()
        payload = dict(record["payload"]); payload["receipt_hash"] = "   "
        rebuilt = make_record(record["record_id"], "cycle_receipt", "host", payload, [],
                              None, created_at="2026-01-01T00:00:00Z")
        self.assertIn("missing_receipt_hash", validate_audit_receipt_record(rebuilt))

    def test_construction_time_validation_still_tolerates_the_absent_hash(self):
        # build_receipt must keep working: it validates, then stamps.
        import inspect
        base = sample_receipt()
        kw = {k: base[k] for k in inspect.signature(build_receipt).parameters if k in base}
        self.assertIn("receipt_hash", build_receipt(**kw))


class ReceiptMustNotContradictItselfTests(unittest.TestCase):
    """A receipt may not claim more than its own stages support.

    The receipt is the artifact everything downstream trusts, and it accepted
    a cycle reporting `completed` while one of its stages was blocked, a
    decision of `recommended` resting on a blocked decision stage, stages
    belonging to a different cycle, and stages whose work fell outside the
    cycle window entirely.
    """

    def stage(self, sid, agent, order, status="completed", **over):
        row = {"stage_id": sid, "agent_id": agent, "status": status,
               "execution_order": order,
               "started_at": f"2026-09-16T0{order}:00:00+00:00",
               "completed_at": f"2026-09-16T0{order}:30:00+00:00",
               "tools_used": ["ibkr"]}
        row.update(over)
        return row

    def receipt(self, **over):
        base = dict(cycle_id="c1", run_id="r1", mode="production", snapshot_id="s1",
                    required_stages=["st1", "st2"],
                    status="completed", decision_status="wait", tools_used=["ibkr"],
                    started_at="2026-09-16T01:00:00+00:00",
                    completed_at="2026-09-16T02:30:00+00:00",
                    self_improvement={"status": "none", "mutation_ids": [], "gates": {}},
                    host={"model": "x", "cognitive_execution_claim": "ran"},
                    stages=[self.stage("st1", "portfolio", 1),
                            self.stage("st2", "decision", 2)])
        base.update(over)
        return base

    def test_the_baseline_receipt_is_valid(self):
        self.assertEqual(validate_receipt(self.receipt()), [])

    def test_completed_cycle_cannot_contain_a_blocked_stage(self):
        receipt = self.receipt(stages=[self.stage("st1", "portfolio", 1),
                                       self.stage("st2", "decision", 2, "blocked")])
        self.assertIn("completed_receipt_has_unfinished_stages:st2=blocked",
                      validate_receipt(receipt))

    def test_completed_cycle_cannot_contain_a_failed_stage(self):
        receipt = self.receipt(stages=[self.stage("st1", "portfolio", 1),
                                       self.stage("st2", "decision", 2, "failed")])
        self.assertTrue(any(e.startswith("completed_receipt_has_unfinished_stages")
                            for e in validate_receipt(receipt)))

    def test_recommendation_cannot_rest_on_a_blocked_stage(self):
        receipt = self.receipt(status="blocked", decision_status="recommended",
                               stages=[self.stage("st1", "portfolio", 1),
                                       self.stage("st2", "decision", 2, "blocked")])
        self.assertIn("recommendation_rests_on_unfinished_stages:st2=blocked",
                      validate_receipt(receipt))

    def test_stage_from_another_cycle_is_refused(self):
        receipt = self.receipt(stages=[self.stage("st1", "portfolio", 1),
                                       self.stage("st2", "decision", 2, cycle_id="OTHER")])
        self.assertIn("stage_belongs_to_other_cycle:st2:OTHER", validate_receipt(receipt))

    def test_stage_finishing_after_the_cycle_is_refused(self):
        receipt = self.receipt(stages=[
            self.stage("st1", "portfolio", 1),
            self.stage("st2", "decision", 2, completed_at="2026-09-16T23:00:00+00:00")])
        self.assertIn("stage_completed_after_cycle:st2", validate_receipt(receipt))

    def test_stage_starting_before_the_cycle_is_refused(self):
        receipt = self.receipt(stages=[
            self.stage("st1", "portfolio", 1, started_at="2026-09-15T00:00:00+00:00"),
            self.stage("st2", "decision", 2)])
        self.assertIn("stage_started_before_cycle:st1", validate_receipt(receipt))

    def test_a_skipped_optional_pass_does_not_invalidate_a_completed_cycle(self):
        """AGENT_ORCHESTRATOR.md requires the host to RECORD skipped passes.

        Refusing them here would push the host toward omitting the stage
        instead of declaring it, which is the opposite of what the receipt
        exists to capture. So a skipped stage the plan did not require is
        fine, and st2 is deliberately not in required_stages below.
        """
        receipt = self.receipt(required_stages=["st1"],
                               stages=[self.stage("st1", "portfolio", 1),
                                       self.stage("st2", "decision", 2, "skipped")])
        self.assertEqual(validate_receipt(receipt), [])

    def test_a_skipped_REQUIRED_pass_is_not_a_completed_cycle(self):
        """Presence was all that was checked.

        A required decision stage marked "skipped" satisfied the plan while
        the receipt still claimed completed. A stage that did not run has not
        run, whatever word the producer chose for it. Recording the skip stays
        honest; calling the cycle complete afterwards does not.
        """
        receipt = self.receipt(required_stages=["st1", "st2"],
                               stages=[self.stage("st1", "portfolio", 1),
                                       self.stage("st2", "decision", 2, "skipped")])
        self.assertIn("required_stage_not_completed:st2:skipped",
                      validate_receipt(receipt))


class AncestryIsVerifiedInDepthTests(unittest.TestCase):
    """Checking the immediate parent proves attachment, not soundness.

    A receipt whose parent exists but whose GRANDPARENT is missing, or whose
    ancestor has been rewritten, was reported verified. That is the live
    situation in this journal, where a receipt validates while its ancestry
    still reaches a damaged historical record -- useful execution evidence,
    but not an integrity proof.
    """

    def record(self, record_id, prev_hash, payload=None):
        row = {"record_id": record_id, "record_type": "note",
               "payload": payload or {"n": record_id}, "prev_hash": prev_hash}
        row["record_hash"] = hash_record(dict(row))
        return row

    def chain(self):
        genesis = self.record("genesis", None)
        parent = self.record("A", genesis["record_hash"])
        child = self.record("B", parent["record_hash"])
        return genesis, parent, child

    def test_a_complete_chain_has_no_ancestry_errors(self):
        genesis, parent, child = self.chain()
        self.assertEqual(_ancestry_errors(child, [genesis, parent, child]), [])

    def test_a_missing_grandparent_is_detected(self):
        _, parent, child = self.chain()
        errors = _ancestry_errors(child, [parent, child])
        self.assertTrue(any(e.startswith("ancestor_missing_at_depth_2") for e in errors),
                        errors)

    def test_a_missing_immediate_parent_is_still_detected(self):
        genesis, _, child = self.chain()
        errors = _ancestry_errors(child, [genesis, child])
        self.assertTrue(any(e.startswith("prev_hash_not_in_journal") for e in errors),
                        errors)

    def test_a_rewritten_ancestor_is_detected(self):
        genesis, parent, child = self.chain()
        rewritten = {**parent, "payload": {"n": "REWRITTEN"}}
        errors = _ancestry_errors(child, [genesis, rewritten, child])
        self.assertTrue(any(e.startswith("ancestor_hash_mismatch") for e in errors),
                        errors)

    def test_a_genesis_record_is_not_an_error(self):
        genesis, _, _ = self.chain()
        self.assertEqual(_ancestry_errors(genesis, [genesis]), [])

    def test_a_looping_journal_terminates(self):
        """Hand-edited journals can contain a loop. Validation must end.

        Which error fires matters less than the walk returning at all.
        """
        first = {"record_id": "A", "prev_hash": "hash-B", "record_hash": "hash-A",
                 "payload": {}}
        second = {"record_id": "B", "prev_hash": "hash-A", "record_hash": "hash-B",
                  "payload": {}}
        self.assertTrue(_ancestry_errors(first, [first, second]))


class AcknowledgedDamageDoesNotBlockForeverTests(unittest.TestCase):
    """One historical write must not brick the system permanently.

    previous_receipt_status refused to run at all while two malformed
    prev_hash writes stood, so no future cycle could start. Explicit
    supersession is how that gets resolved: damage that has been signed for
    in the journal stops blocking, damage nobody acknowledged still does.
    """

    def record(self, record_id, prev_hash, record_type="note", payload=None):
        row = {"record_id": record_id, "record_type": record_type, "agent": "t",
               "payload": payload if payload is not None else {"n": record_id},
               "prev_hash": prev_hash}
        row["record_hash"] = hash_record(dict(row))
        return row

    def supersession(self, records, target_id, defect, prev_hash):
        target = next(r for r in records if r["record_id"] == target_id)
        return self.record(
            f"sup-{defect}-{target_id}", prev_hash, record_type="supersession",
            payload={"supersedes": target_id, "defect": defect,
                     "observed_hash": target["record_hash"],
                     "reason": "acknowledged historical damage, preserved as evidence"},
        )

    def damaged_ancestor_chain(self):
        """root <- tampered <- receipt. The ancestor no longer hashes true."""
        root = self.record("root", None)
        tampered = self.record("tampered", root["record_hash"])
        receipt = self.record("r1", tampered["record_hash"])
        tampered["payload"] = {"n": "REWRITTEN"}
        return [root, tampered, receipt]

    def test_unacknowledged_ancestor_damage_blocks(self):
        records = self.damaged_ancestor_chain()
        errors = _ancestry_errors(records[-1], records)
        self.assertTrue(any(e.startswith("ancestor_hash_mismatch") for e in errors), errors)

    def test_acknowledged_ancestor_damage_does_not_block(self):
        records = self.damaged_ancestor_chain()
        records.append(self.supersession(records, "tampered", "content_mismatch",
                                         records[-1]["record_hash"]))
        self.assertEqual(_ancestry_errors(records[2], records), [])

    def broken_link_chain(self):
        """A stranded segment, plus a live segment to anchor evidence in.

        A supersession has to be REACHABLE to authorize anything, so it
        cannot be chained onto the stranded records it retires. That is the
        protection working: acknowledging damage requires writing into the
        part of the journal that still verifies.
        """
        root = self.record("root", None)
        live = self.record("live", root["record_hash"])
        broken = self.record("broken", "f" * 64)
        child = self.record("child", broken["record_hash"])
        return [root, live, broken, child]

    def test_unacknowledged_link_damage_blocks(self):
        records = self.broken_link_chain()
        self.assertTrue(_ancestry_errors(records[-1], records))

    def test_acknowledged_link_damage_does_not_block(self):
        records = self.broken_link_chain()
        records.append(self.supersession(records, "broken", "bad_prev_hash",
                                         records[1]["record_hash"]))
        self.assertEqual(_ancestry_errors(records[3], records), [])

    def test_a_supersession_chained_onto_stranded_records_authorizes_nothing(self):
        records = self.broken_link_chain()
        stranded = self.supersession(records, "broken", "bad_prev_hash",
                                     records[3]["record_hash"])
        records.append(stranded)
        self.assertTrue(_ancestry_errors(records[3], records))

    def test_a_retired_receipt_is_not_chosen_as_the_predecessor(self):
        """A retired receipt is not the cycle anything follows."""
        root = self.record("root", None, record_type="cycle_receipt",
                           payload=sample_receipt())
        newer = self.record("cycle-receipt:retired", root["record_hash"],
                            record_type="cycle_receipt", payload=sample_receipt())
        records = [root, newer]
        records.append(self.supersession(records, "cycle-receipt:retired",
                                         "content_mismatch", newer["record_hash"]))
        chosen = latest_receipt_record(records)
        self.assertEqual(chosen["record_id"], "root")

    def test_an_unretired_broken_receipt_is_still_chosen(self):
        """Skipping a NEW broken receipt would hide the damage this check
        exists to catch."""
        root = self.record("root", None, record_type="cycle_receipt",
                           payload=sample_receipt())
        newer = self.record("cycle-receipt:broken", root["record_hash"],
                            record_type="cycle_receipt", payload=sample_receipt())
        chosen = latest_receipt_record([root, newer])
        self.assertEqual(chosen["record_id"], "cycle-receipt:broken")


class TheLatestReceiptIsTheLatestOnTheChainTests(unittest.TestCase):
    """Which receipt a new cycle anchors to decided by line order.

    order_chain already answers "which receipt follows which" from the
    hashes. latest_receipt_record consulted it only to ask whether a receipt
    was reachable, then fell back to the order the records happened to arrive
    in. Two correctly linked receipts read back in the other order therefore
    named the earlier one as the predecessor, and a new cycle would chain
    itself onto history that had already been continued.
    """

    def journal(self, cycle_ids):
        import tempfile
        from .audit_store import AuditJournal
        journal = AuditJournal(
            pathlib.Path(tempfile.mkdtemp(prefix="sovereign-order-")) / "a.jsonl")
        for cycle_id in cycle_ids:
            journal.append(record_id=f"cycle-receipt:{cycle_id}",
                           record_type="cycle_receipt", agent="sovereign-host",
                           payload={"cycle_id": cycle_id}, caused_by=())
        return journal.read()

    def test_the_last_linked_receipt_wins_whatever_the_file_order(self):
        records = self.journal(["older", "newer"])
        self.assertEqual(latest_receipt_record(records)["record_id"],
                         "cycle-receipt:newer")
        self.assertEqual(latest_receipt_record(list(reversed(records)))["record_id"],
                         "cycle-receipt:newer")

    def test_a_longer_chain_is_still_read_by_its_links(self):
        records = self.journal(["a", "b", "c", "d"])
        shuffled = [records[2], records[0], records[3], records[1]]
        self.assertEqual(latest_receipt_record(shuffled)["record_id"],
                         "cycle-receipt:d")


class ModeIsCheckedLikeEveryOtherStatusFieldTests(unittest.TestCase):
    """status, decision_status and every stage status were checked against a
    vocabulary. mode was required and accepted any string, so a typo produced
    a receipt that validated cleanly and then grouped with nothing.

    The allowed list is the set of modes actually present in the journal
    rather than an invented taxonomy. Each was produced by a real code path,
    and rejecting them would invalidate stored receipts describing real
    cycles. The point is that a tenth value cannot appear silently.
    """

    def receipt(self, mode):
        base = sample_receipt()
        base["mode"] = mode
        return base

    def test_an_invented_mode_is_refused(self):
        self.assertIn("mode_not_allowed:intrady",
                      validate_receipt(self.receipt("intrady")))

    def test_an_empty_mode_is_refused(self):
        self.assertTrue([e for e in validate_receipt(self.receipt(""))
                         if "mode" in e])

    def mode_errors(self, mode):
        """Mode errors only.

        Rewriting mode on a built receipt invalidates its hash, which is a
        different check doing its own job correctly.
        """
        return [e for e in validate_receipt(self.receipt(mode))
                if e.startswith("mode")]

    def test_the_live_modes_are_accepted(self):
        for mode in ("production", "host_input_replay", "maintenance"):
            with self.subTest(mode=mode):
                self.assertEqual(self.mode_errors(mode), [])

    def test_every_mode_already_in_the_journal_still_validates(self):
        """Tightening this must not invalidate receipts for real past cycles."""
        for mode in ALLOWED_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(self.mode_errors(mode), [])


class ASubsetOfAPlanCannotPassAsTheWholeTests(unittest.TestCase):
    """A receipt was valid with ANY non-empty stage list.

    A "completed" production cycle carrying only the portfolio stage and a
    decision_status of "recommended" validated cleanly: a trade
    recommendation produced without research and without a decision stage
    ever running. Half a cycle asserting it was a whole one.

    Hardcoding stage names per mode was the first attempt and it was wrong,
    because it assumes one plan and fails any caller running a different one.
    The plan is not the envelope's business; that the receipt DECLARE its plan
    and match it is.
    """

    def stage(self, order, stage_id):
        return {"stage_id": stage_id, "agent_id": stage_id, "status": "completed",
                "execution_order": order,
                "started_at": f"2026-09-16T06:0{order}:00Z",
                "completed_at": f"2026-09-16T06:0{order}:30Z", "tools_used": []}

    def build(self, stages, *, required, mode="production", status="completed",
              decision="recommended"):
        return build_receipt(
            cycle_id="c", run_id="r", started_at="2026-09-16T06:00:00Z",
            completed_at="2026-09-16T06:05:00Z", mode=mode, snapshot_id="s",
            stages=stages, tools_used=["IBKR"], status=status,
            decision_status=decision, required_stages=required,
            self_improvement={"status": "none", "mutation_ids": [], "gates": {}},
            host={"cognitive_execution_claim": "ran it", "host_type": "t"},
            blockers=[])

    def test_a_recommendation_from_one_stage_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.build([self.stage(1, "portfolio")],
                       required=["portfolio", "research", "decision"])
        self.assertIn("required_stage_missing:research", str(ctx.exception))
        self.assertIn("required_stage_missing:decision", str(ctx.exception))

    def test_a_cycle_that_ran_its_whole_plan_is_accepted(self):
        receipt = self.build(
            [self.stage(1, "portfolio"), self.stage(2, "research"),
             self.stage(3, "decision")],
            required=["portfolio", "research", "decision"])
        self.assertEqual(validate_receipt(receipt), [])

    def test_a_portfolio_affecting_mode_must_declare_its_plan(self):
        """Otherwise omitting the declaration is the way around the check."""
        with self.assertRaises(ValueError) as ctx:
            self.build([self.stage(1, "portfolio")], required=None)
        self.assertIn("required_stages_not_declared", str(ctx.exception))

    def test_an_exercise_mode_need_not_declare_one(self):
        """e2e and maintenance modes describe exercises, not cycles."""
        receipt = self.build([self.stage(1, "portfolio")], required=None,
                             mode="e2e-smoke-manual", decision="wait")
        self.assertEqual(validate_receipt(receipt), [])

    def test_a_stored_receipt_without_the_field_still_validates(self):
        """Every receipt in the journal predates this field.

        previous_receipt_status validates the predecessor before each cycle,
        so failing on absence would invalidate real history and refuse to
        start any new cycle at all.
        """
        receipt = self.build(
            [self.stage(1, "portfolio"), self.stage(2, "research"),
             self.stage(3, "decision")],
            required=["portfolio", "research", "decision"])
        legacy = {k: v for k, v in receipt.items() if k != "required_stages"}
        self.assertEqual([e for e in validate_receipt(legacy)
                          if "required_stage" in e], [])


class TheWritePathEnforcesWhatReadingCannotTests(unittest.TestCase):
    """validate_receipt exempts a receipt with no declared plan.

    That exemption exists because every receipt already in the journal
    predates the field, and the predecessor is validated before each cycle,
    so failing on absence would refuse to start anything. But the exemption
    is for READING old records. Accepting a NEW one is a different act, and a
    fresh receipt can declare its plan, so appending one that does not is
    refused even though validating it would not be.
    """

    def stage(self, order, stage_id):
        return {"stage_id": stage_id, "agent_id": stage_id,
                "status": "completed", "execution_order": order,
                "started_at": f"2026-09-16T06:0{order}:00Z",
                "completed_at": f"2026-09-16T06:0{order}:30Z", "tools_used": []}

    def raw_receipt(self, **over):
        receipt = {
            "cycle_id": "c-raw", "run_id": "r", "mode": "production",
            "snapshot_id": "s", "started_at": "2026-09-16T06:00:00Z",
            "completed_at": "2026-09-16T06:05:00Z",
            "stages": [self.stage(1, "portfolio")], "tools_used": ["IBKR"],
            "status": "completed", "decision_status": "recommended",
            "self_improvement": {"status": "none", "mutation_ids": [], "gates": {}},
            "host": {"cognitive_execution_claim": "ran", "host_type": "t"},
            "blockers": []}
        receipt.update(over)
        return receipt

    def journal(self):
        import tempfile
        from .audit_store import AuditJournal
        return AuditJournal(
            pathlib.Path(tempfile.mkdtemp(prefix="sovereign-write-")) / "a.jsonl")

    def test_reading_an_undeclared_receipt_is_still_permitted(self):
        self.assertEqual([e for e in validate_receipt(self.raw_receipt())
                          if "required_stages_not_declared" in e], [])

    def test_appending_an_undeclared_receipt_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.journal().append_cycle_receipt(self.raw_receipt())
        self.assertIn("required_stages_not_declared", str(ctx.exception))

    def test_appending_a_declared_receipt_still_works(self):
        receipt = self.raw_receipt(
            required_stages=["portfolio"],
            decision_status="wait")
        record = self.journal().append_cycle_receipt(receipt)
        self.assertEqual(record["record_type"], "cycle_receipt")
