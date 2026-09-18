import json
import tempfile
import unittest
from pathlib import Path

from .audit_store import AuditJournal
from .cycle_receipt import validate_audit_receipt_record, verify_receipt_hash
from .orchestrator import AgentJob
from .production_host import ProductionHostExecutor, make_portfolio_handler, make_tool_stage_handler


SELF_IMPROVEMENT = {
    "status": "not_ready",
    "mutation_ids": [],
    "gates": {},
}


def _success_handler(name):
    def handler(job, context, dependencies):
        return {
            "status": "completed",
            "stage": job.agent_id,
            "context_cycle": context["cycle_id"],
            "dependencies": sorted(dependencies),
            "tools_used": [name],
        }
    return handler


class ProductionHostExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.journal = AuditJournal(Path(self.tempdir.name) / "audit.jsonl")
        self.executor = ProductionHostExecutor(self.journal)
        self.jobs = [
            AgentJob("portfolio", "observe"),
            AgentJob("research_director", "direct", ("portfolio",)),
            AgentJob("decision", "decision", ("research_director",)),
        ]

    def tearDown(self):
        self.tempdir.cleanup()

    def handlers(self, calls):
        def portfolio(job, context, dependencies):
            calls.append(job.agent_id)
            return {"status": "completed", "stage": job.agent_id, "tools_used": ["IBKR"]}

        def director(job, context, dependencies):
            calls.append(job.agent_id)
            self.assertIsInstance(job, AgentJob)
            self.assertEqual(sorted(dependencies), ["portfolio"])
            return {"status": "completed", "stage": job.agent_id, "tools_used": ["web"]}

        def decision(job, context, dependencies):
            calls.append(job.agent_id)
            self.assertEqual(sorted(dependencies), ["research_director"])
            return {"status": "completed", "decision_status": "wait", "tools_used": []}

        return {"portfolio": portfolio, "research_director": director, "decision": decision}

    def run_cycle(self, calls, cycle_id="cycle-test", run_id="run-test"):
        return self.executor.run(
            jobs=self.jobs,
            handlers=self.handlers(calls),
            context={"snapshot_id": "snapshot-test"},
            cycle_id=cycle_id,
            run_id=run_id,
            started_at="2026-01-01T00:00:00+00:00",
            host_claim="Executed the three stages through real host handlers and persisted each checkpoint.",
            self_improvement=SELF_IMPROVEMENT,
        )

    def test_initial_run_persists_each_stage_and_one_receipt(self):
        calls = []
        result, receipt, state = self.run_cycle(calls)
        self.assertEqual(calls, ["portfolio", "research_director", "decision"])
        self.assertEqual(state.new_stage_ids, tuple(calls))
        self.assertEqual(state.reused_stage_ids, ())
        self.assertEqual(result.blocked, ())
        records = self.journal.read()
        self.assertEqual(sum(r["record_type"] == "cycle_stage" for r in records), 3)
        receipts = [r for r in records if r["record_type"] == "cycle_receipt"]
        self.assertEqual(len(receipts), 1)
        self.assertTrue(validate_audit_receipt_record(receipts[0]) == [])
        self.assertTrue(verify_receipt_hash(receipt))

    def test_restart_reuses_all_stage_records_without_rerunning_handlers(self):
        first_calls = []
        self.run_cycle(first_calls)
        second_calls = []
        result, receipt, state = self.run_cycle(second_calls)
        self.assertEqual(second_calls, [])
        self.assertEqual(state.reused_stage_ids, tuple(j.agent_id for j in self.jobs))
        self.assertEqual(state.new_stage_ids, ())
        self.assertEqual(len([r for r in self.journal.read() if r["record_type"] == "cycle_receipt"]), 1)
        self.assertEqual(result.completed, tuple(j.agent_id for j in self.jobs))
        self.assertEqual(receipt["status"], "completed")

    def test_invalid_predecessor_blocks_start(self):
        self.run_cycle([])
        records = self.journal.read()
        receipt = next(r for r in records if r["record_type"] == "cycle_receipt")
        receipt["payload"]["host"]["cognitive_execution_claim"] = "tampered"
        self.journal.path.write_text("\n".join(
            __import__("json").dumps(r, sort_keys=True, separators=(",", ":"))
            for r in records
        ) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "invalid_previous_receipt"):
            self.run_cycle([], cycle_id="cycle-next", run_id="run-next")

    def test_portfolio_factory_requires_authoritative_ibkr_as_of_and_source(self):
        handler = make_portfolio_handler(lambda: {
            "source": "ibkr",
            "as_of": "2026-09-16T10:00:00Z",
            "net_liquidation": 1.0,
            "order_submission_used": False,
        })
        result = handler(AgentJob("portfolio", "observe"), {}, {})
        self.assertEqual(result["ibkr_as_of"], "2026-09-16T10:00:00Z")
        self.assertEqual(result["snapshot"]["source"], "ibkr")

    def test_portfolio_factory_rejects_missing_as_of(self):
        handler = make_portfolio_handler(lambda: {"source": "ibkr"})
        with self.assertRaisesRegex(ValueError, "ibkr_portfolio_as_of_required"):
            handler(AgentJob("portfolio", "observe"), {}, {})

    def test_portfolio_factory_rejects_order_submission(self):
        handler = make_portfolio_handler(lambda: {
            "source": "ibkr",
            "as_of": "2026-09-16T10:00:00Z",
            "order_submission_used": True,
        })
        with self.assertRaisesRegex(ValueError, "live_order_submission_forbidden"):
            handler(AgentJob("portfolio", "observe"), {}, {})

    def test_portfolio_factory_refuses_a_missing_order_declaration(self):
        handler = make_portfolio_handler(lambda: {
            "source": "ibkr",
            "as_of": "2026-09-16T10:00:00Z",
        })
        with self.assertRaisesRegex(ValueError, "order_submission_declaration_required"):
            handler(AgentJob("portfolio", "observe"), {}, {})

    def test_portfolio_factory_refuses_a_non_false_declaration(self):
        for value in ("no", None, 0.0, "false"):
            handler = make_portfolio_handler(lambda v=value: {
                "source": "ibkr",
                "as_of": "2026-09-16T10:00:00Z",
                "order_submission_used": v,
            })
            with self.assertRaises(ValueError):
                handler(AgentJob("portfolio", "observe"), {}, {})

    def test_tool_factory_requires_complete_tool_call_trace(self):
        handler = make_tool_stage_handler(lambda *_: {"observations": []})
        with self.assertRaisesRegex(ValueError, "tool_calls_required:research"):
            handler(AgentJob("research", "specialist"), {}, {})

    def test_tool_factory_preserves_tool_calls_and_derives_tools_used(self):
        handler = make_tool_stage_handler(lambda *_: {
            "tool_calls": [
                {"tool": "Longbridge", "result": {"symbol": "ABC"}},
                {"tool": "Web", "result": {"url": "example"}},
            ],
            "observations": [{"symbol": "ABC"}],
        })
        result = handler(AgentJob("research", "specialist"), {}, {})
        self.assertEqual(result["tools_used"], ["Longbridge", "Web"])
        self.assertEqual(len(result["tool_calls"]), 2)


class MidRunRestartTests(unittest.TestCase):
    def test_kill_after_first_persisted_stage_then_resume(self):
        with tempfile.TemporaryDirectory() as tempdir:
            journal = AuditJournal(Path(tempdir) / "audit.jsonl")
            executor = ProductionHostExecutor(journal)
            jobs = [
                AgentJob("one", "test"),
                AgentJob("two", "test", ("one",)),
                AgentJob("decision", "decision", ("two",)),
            ]
            calls = []

            def one(job, context, dependencies):
                calls.append(job.agent_id)
                return {"status": "completed", "tools_used": ["fake-test"]}

            def two(job, context, dependencies):
                calls.append(job.agent_id)
                raise SystemExit("simulated host kill")

            handlers = {
                "one": one,
                "two": two,
                "decision": lambda job, context, dependencies: {
                    "status": "completed", "decision_status": "wait", "tools_used": []
                },
            }
            common = dict(
                jobs=jobs,
                handlers=handlers,
                context={"snapshot_id": "snap"},
                cycle_id="cycle-kill",
                run_id="run-kill",
                started_at="2026-01-01T00:00:00+00:00",
                host_claim="Resumed a killed host after the first durable stage checkpoint.",
                self_improvement=SELF_IMPROVEMENT,
            )
            with self.assertRaises(SystemExit):
                executor.run(**common)
            self.assertEqual(calls, ["one", "two"])
            self.assertEqual(len([r for r in journal.read() if r["record_type"] == "cycle_stage"]), 1)
            calls.clear()

            def two_after_restart(job, context, dependencies):
                calls.append(job.agent_id)
                return {"status": "completed", "tools_used": ["fake-test"]}

            executor.run(**{**common, "handlers": {
                "one": one,
                "two": two_after_restart,
                "decision": handlers["decision"],
            }})
            self.assertEqual(calls, ["two"])
            stages = [r for r in journal.read() if r["record_type"] == "cycle_stage"]
            self.assertEqual(len(stages), 3)
            receipts = [r for r in journal.read() if r["record_type"] == "cycle_receipt"]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["payload"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()


class AResumedCycleKeepsItsSnapshotTests(unittest.TestCase):
    """A resumed cycle keeps its cycle_id and run_id, so neither noticed the
    snapshot underneath had changed.

    Reused stages carried observations of one portfolio into a decision the
    receipt then attributed to another: the receipt named the new snapshot
    while the evidence came from the old one. That is the precise shape of
    claim this runtime exists to make impossible, produced by the runner
    rather than by the host.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.executor = ProductionHostExecutor(
            AuditJournal(Path(self.tempdir.name) / "audit.jsonl"))
        self.jobs = [AgentJob("portfolio", "observe"),
                     AgentJob("decision", "decision", ("portfolio",))]

    def handlers(self, *, interrupt):
        def portfolio(job, context, dependencies):
            return {"status": "completed", "observed_under": context["snapshot_id"],
                    "tools_used": ["IBKR"]}

        def decision(job, context, dependencies):
            if interrupt:
                raise RuntimeError("interrupted")
            return {"status": "completed", "decision_status": "wait", "tools_used": []}

        return {"portfolio": portfolio, "decision": decision}

    def run_cycle(self, snapshot_id, *, interrupt=False):
        return self.executor.run(
            jobs=self.jobs, handlers=self.handlers(interrupt=interrupt),
            context={"snapshot_id": snapshot_id}, cycle_id="c1", run_id="r1",
            started_at="2026-01-01T00:00:00+00:00",
            host_claim="Ran the stages through real handlers and persisted each checkpoint.",
            self_improvement=SELF_IMPROVEMENT)

    def interrupted_first_pass(self, snapshot_id="SNAP-A"):
        """Leaves portfolio persisted and decision unrun, as a real interrupt does."""
        self.run_cycle(snapshot_id, interrupt=True)
        stages = [r["payload"]["stage_id"] for r in self.executor._records()
                  if "stage" in str(r.get("record_type"))]
        self.assertEqual(stages, ["portfolio"])

    def test_resuming_onto_a_different_snapshot_is_refused(self):
        self.interrupted_first_pass()
        with self.assertRaises(RuntimeError) as ctx:
            self.run_cycle("SNAP-B")
        self.assertIn("persisted_stage_snapshot_mismatch", str(ctx.exception))

    def test_resuming_onto_the_same_snapshot_still_works(self):
        self.interrupted_first_pass()
        _, receipt, state = self.run_cycle("SNAP-A")
        self.assertIn("portfolio", state.reused_stage_ids)
        self.assertEqual(receipt["snapshot_id"], "SNAP-A")

    def test_a_stage_records_the_snapshot_it_ran_under(self):
        self.interrupted_first_pass()
        stages = [r for r in self.executor._records()
                  if "stage" in str(r.get("record_type"))]
        self.assertTrue(stages)
        for record in stages:
            self.assertEqual(record["payload"]["snapshot_id"], "SNAP-A")

    def test_a_stage_with_no_recorded_snapshot_is_not_assumed_to_match(self):
        """Written before stages carried a snapshot.

        What it ran against is genuinely unknown, and unknown is not "the
        same". Reusing it would be a guess the receipt then states as fact.
        Appended through the journal so the chain stays valid and this tests
        the missing field rather than a broken hash.
        """
        journal = AuditJournal(Path(self.tempdir.name) / "audit.jsonl")
        journal.append(
            record_id="cycle-stage:c1:portfolio", record_type="cycle_stage",
            agent="sovereign-host", caused_by=(),
            payload={"cycle_id": "c1", "run_id": "r1", "stage_id": "portfolio",
                     "agent_id": "portfolio", "status": "completed",
                     "execution_order": 1, "started_at": "2026-01-01T00:00:00+00:00",
                     "completed_at": "2026-01-01T00:00:01+00:00",
                     "tools_used": ["IBKR"], "output": {"status": "completed"}})
        with self.assertRaises(RuntimeError) as ctx:
            self.run_cycle("SNAP-A")
        self.assertIn("persisted_stage_snapshot_unknown", str(ctx.exception))


class ABlockedCycleCitesOnlyRecordsThatExistTests(unittest.TestCase):
    """The receipt named a cause that was never written.

    A blocked stage still belongs in the receipt's stage list, which is
    honest reporting. It gets a synthetic stage_meta entry and no journal
    record, and caused_by was built from stage_meta, so the causal graph
    pointed at stage records nobody had written. Reporting a stage as
    blocked and citing it as a persisted cause are different claims.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.journal = AuditJournal(Path(self.tempdir.name) / "audit.jsonl")

    def run_with(self, decision_handler):
        executor = ProductionHostExecutor(self.journal)

        def portfolio(job, context, dependencies):
            return {"status": "completed", "tools_used": ["IBKR"]}

        return executor.run(
            jobs=[AgentJob("portfolio", "observe"),
                  AgentJob("decision", "decision", ("portfolio",))],
            handlers={"portfolio": portfolio, "decision": decision_handler},
            context={"snapshot_id": "S"}, cycle_id="c1", run_id="r1",
            started_at="2026-01-01T00:00:00+00:00",
            host_claim="Ran the stages through real handlers and persisted each checkpoint.",
            self_improvement=SELF_IMPROVEMENT)

    def receipt_record(self):
        return next(r for r in self.journal.read()
                    if r["record_type"] == "cycle_receipt")

    def test_a_blocked_stage_is_not_cited_as_a_persisted_cause(self):
        def blows_up(job, context, dependencies):
            raise RuntimeError("stage blew up")

        self.run_with(blows_up)
        record = self.receipt_record()
        written = {r["record_id"] for r in self.journal.read()}
        dangling = [c for c in record.get("caused_by", []) if c not in written]
        self.assertEqual(dangling, [])
        self.assertIn("cycle-stage:c1:portfolio", record["caused_by"])

    def test_the_blocked_stage_is_still_reported_in_the_receipt(self):
        """Narrowing the citations must not hide the failure."""
        def blows_up(job, context, dependencies):
            raise RuntimeError("stage blew up")

        _, receipt, _ = self.run_with(blows_up)
        statuses = {s["stage_id"]: s["status"] for s in receipt["stages"]}
        self.assertEqual(statuses["decision"], "blocked")
        self.assertEqual(receipt["status"], "blocked")

    def test_a_completed_cycle_still_cites_every_stage(self):
        def decides(job, context, dependencies):
            return {"status": "completed", "decision_status": "wait", "tools_used": []}

        self.run_with(decides)
        caused_by = self.receipt_record()["caused_by"]
        self.assertIn("cycle-stage:c1:portfolio", caused_by)
        self.assertIn("cycle-stage:c1:decision", caused_by)

    def test_a_resumed_cycle_still_cites_the_stage_it_reused(self):
        """A reused stage is persisted evidence too.

        Citing only newly run stages would drop it from the causal graph, so
        a resumed cycle's receipt would silently lose the provenance of work
        that genuinely happened.
        """
        def blows_up(job, context, dependencies):
            raise RuntimeError("stage blew up")

        def decides(job, context, dependencies):
            return {"status": "completed", "decision_status": "wait", "tools_used": []}

        self.run_with(blows_up)
        journal_path = Path(self.tempdir.name) / "audit.jsonl"
        kept = [line for line in journal_path.read_text().splitlines()
                if '"cycle_receipt"' not in line]
        journal_path.write_text("\n".join(kept) + "\n")
        self.journal = AuditJournal(journal_path)

        _, _, state = self.run_with(decides)
        self.assertEqual(state.reused_stage_ids, ("portfolio",))
        caused_by = self.receipt_record()["caused_by"]
        self.assertIn("cycle-stage:c1:portfolio", caused_by)
        self.assertIn("cycle-stage:c1:decision", caused_by)


class ResumeIsBoundToContentNotALabelTests(unittest.TestCase):
    """snapshot_id is a label, and reuse was bound to it.

    A caller that keeps the label while changing what is underneath silently
    reused stale stage output: same "SNAP-A", price 1 replaced by 999, and
    the decision still consumed 1. run_host_cycle happens to build a
    content-addressed snapshot_id, so the real path was safe by that caller's
    convention rather than by anything the executor enforced.

    What this cannot see is data a handler CLOSES OVER rather than receiving
    through context. That is outside the executor's view, so callers must
    pass the inputs their stages depend on instead of capturing them.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.executor = ProductionHostExecutor(
            AuditJournal(Path(self.tempdir.name) / "audit.jsonl"))

    def run_cycle(self, context, *, interrupt, jobs=None):
        def portfolio(job, ctx, dependencies):
            return {"status": "completed", "seen": ctx.get("price"),
                    "tools_used": ["IBKR"]}

        def decision(job, ctx, dependencies):
            if interrupt:
                raise RuntimeError("interrupted")
            return {"status": "completed", "decision_status": "wait",
                    "tools_used": []}

        return self.executor.run(
            jobs=jobs or [AgentJob("portfolio", "observe"),
                          AgentJob("decision", "decision", ("portfolio",))],
            handlers={"portfolio": portfolio, "decision": decision},
            context=context, cycle_id="c1", run_id="r1", snapshot_id="SNAP-A",
            started_at="2026-01-01T00:00:00+00:00",
            host_claim="Ran the stages through real handlers and persisted each checkpoint.",
            self_improvement=SELF_IMPROVEMENT)

    def interrupted_first_pass(self, price=1):
        self.run_cycle({"snapshot_id": "SNAP-A", "price": price}, interrupt=True)

    def test_changed_input_behind_an_unchanged_label_is_refused(self):
        self.interrupted_first_pass(price=1)
        with self.assertRaises(RuntimeError) as ctx:
            self.run_cycle({"snapshot_id": "SNAP-A", "price": 999},
                           interrupt=False)
        self.assertIn("persisted_stage_plan_mismatch", str(ctx.exception))

    def test_an_unchanged_input_still_resumes(self):
        self.interrupted_first_pass(price=1)
        _, _, state = self.run_cycle({"snapshot_id": "SNAP-A", "price": 1},
                                     interrupt=False)
        self.assertIn("portfolio", state.reused_stage_ids)

    def test_a_changed_plan_is_refused_too(self):
        """Same inputs, different dependency graph, is a different cycle."""
        self.interrupted_first_pass(price=1)
        with self.assertRaises(RuntimeError) as ctx:
            self.run_cycle(
                {"snapshot_id": "SNAP-A", "price": 1}, interrupt=False,
                jobs=[AgentJob("portfolio", "observe"),
                      AgentJob("decision", "decision", ())])
        self.assertIn("persisted_stage_plan_mismatch", str(ctx.exception))

    def test_a_stage_without_a_recorded_plan_is_not_assumed_to_match(self):
        """Written before stages recorded their plan.

        Appended through the journal so the chain stays valid and this tests
        the missing field rather than a broken hash.
        """
        journal = AuditJournal(Path(self.tempdir.name) / "audit.jsonl")
        journal.append(
            record_id="cycle-stage:c1:portfolio", record_type="cycle_stage",
            agent="sovereign-host", caused_by=(),
            payload={"cycle_id": "c1", "run_id": "r1", "stage_id": "portfolio",
                     "agent_id": "portfolio", "status": "completed",
                     "snapshot_id": "SNAP-A", "execution_order": 1,
                     "started_at": "2026-01-01T00:00:00+00:00",
                     "completed_at": "2026-01-01T00:00:01+00:00",
                     "tools_used": ["IBKR"], "output": {"status": "completed"}})
        with self.assertRaises(RuntimeError) as ctx:
            self.run_cycle({"snapshot_id": "SNAP-A", "price": 1}, interrupt=False)
        self.assertIn("persisted_stage_plan_unknown", str(ctx.exception))

    def test_the_growing_journal_does_not_invalidate_a_resume(self):
        """previous_receipt changes as records accumulate.

        Including it in the fingerprint would make every resume look like a
        changed cycle, which would brick resumption entirely.
        """
        self.interrupted_first_pass()
        _, _, state = self.run_cycle({"snapshot_id": "SNAP-A", "price": 1},
                                     interrupt=False)
        self.assertEqual(state.reused_stage_ids, ("portfolio",))


class ARerunReportsThePersistedReceiptTests(unittest.TestCase):
    """A rerun rebuilt the receipt with a fresh completed_at.

    The value returned to the caller then differed from the one in the
    journal: same cycle, two receipts, and the caller acting on the one that
    is not the record. The persisted receipt IS the receipt.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "audit.jsonl"
        self.executor = ProductionHostExecutor(AuditJournal(self.path))

    def run_cycle(self):
        def portfolio(job, context, dependencies):
            return {"status": "completed", "tools_used": ["IBKR"]}

        def decision(job, context, dependencies):
            return {"status": "completed", "decision_status": "wait",
                    "tools_used": []}

        return self.executor.run(
            jobs=[AgentJob("portfolio", "observe"),
                  AgentJob("decision", "decision", ("portfolio",))],
            handlers={"portfolio": portfolio, "decision": decision},
            context={"snapshot_id": "S"}, cycle_id="c1", run_id="r1",
            snapshot_id="S", started_at="2026-01-01T00:00:00+00:00",
            host_claim="Ran the stages through real handlers and persisted each checkpoint.",
            self_improvement=SELF_IMPROVEMENT)

    def stored(self):
        return next(json.loads(line) for line
                    in self.path.read_text(encoding="utf-8").splitlines()
                    if '"cycle_receipt"' in line)["payload"]

    def test_a_rerun_returns_the_receipt_that_was_persisted(self):
        self.run_cycle()
        _, second, _ = self.run_cycle()
        self.assertEqual(second["receipt_hash"], self.stored()["receipt_hash"])

    def test_a_rerun_does_not_mint_a_second_receipt(self):
        self.run_cycle()
        self.run_cycle()
        receipts = [line for line in
                    self.path.read_text(encoding="utf-8").splitlines()
                    if '"cycle_receipt"' in line]
        self.assertEqual(len(receipts), 1)

    def test_the_first_run_still_returns_what_it_wrote(self):
        _, first, _ = self.run_cycle()
        self.assertEqual(first["receipt_hash"], self.stored()["receipt_hash"])
