"""The system proposes. It never submits.

That is the central safety promise, and until now it was enforced in exactly
one place: FORBIDDEN_MUTATION_TOKENS stops a self-improvement patch from
ADDING order-submission code. Everywhere else the promise was prose --
LLM_HOST_CONTRACT.md, AGENT_ORCHESTRATOR.md, E2E_TEST_PROTOCOL.md all assert
it, and nothing checked it.

Two things are asserted here.

Behaviourally: a cycle carrying the strongest possible decision, a live
"recommended", produces a receipt and nothing else. No order, no instruction
queue, no file that a downstream reader could mistake for one.

Structurally: the runtime holds no capability to place an order at all. A
behavioural test only covers the paths it walks, and "no such capability
exists" is a claim about every path, so it has to be asserted over the source.
This is deliberately a structural assertion rather than a behavioural one; the
alternative is trusting that the paths nobody tested also happen not to
submit.
"""

import json
import tempfile
import unittest
from pathlib import Path

from .audit_store import AuditJournal
from .orchestrator import AgentJob
from .production_host import ProductionHostExecutor
from .self_improvement import FORBIDDEN_MUTATION_TOKENS

RUNTIME_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUNTIME_DIR.parent

# Client libraries that can reach a broker. Importing one does not prove an
# order was placed, but it does mean the capability is one call away, and this
# runtime has no legitimate reason to hold it: the host observes IBKR in its
# own environment and commits what it saw.
BROKER_CLIENT_MODULES = (
    "ib_insync", "ibapi", "ib_async", "ibkr", "interactive_brokers",
    "alpaca", "ccxt", "tda", "schwab",
)

# Method names that submit, modify or cancel an order.
ORDER_WRITE_CALLS = (
    "placeOrder", "place_order", "submitOrder", "submit_order",
    "cancelOrder", "cancel_order", "modifyOrder", "modify_order",
    "bracketOrder", "market_order", "limit_order",
)


def production_sources():
    """Every production runtime module, tests excluded."""
    return [p for p in sorted(RUNTIME_DIR.glob("*.py"))
            if not p.name.startswith("test_")]


class TheRuntimeHoldsNoOrderCapabilityTests(unittest.TestCase):
    """You cannot accidentally submit through an API you never imported."""

    def test_no_production_module_imports_a_broker_client(self):
        offenders = []
        for path in production_sources():
            source = path.read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if not (stripped.startswith("import ")
                        or stripped.startswith("from ")):
                    continue
                # Relative imports are this package's own modules. One is
                # NAMED ibkr_backfill because it normalises IBKR data the
                # host already fetched; it is not a broker client.
                if stripped.startswith("from ."):
                    continue
                for module in BROKER_CLIENT_MODULES:
                    if module in stripped.lower():
                        offenders.append(f"{path.name}: {stripped}")
        self.assertEqual(offenders, [], f"broker client imported: {offenders}")

    def test_no_production_module_calls_an_order_write_api(self):
        offenders = []
        for path in production_sources():
            source = path.read_text(encoding="utf-8")
            for number, line in enumerate(source.splitlines(), start=1):
                # The token lists themselves are declarations of what is
                # forbidden, not uses of it. Refusing them would make naming
                # the danger impossible.
                if path.name == "self_improvement.py" or "FORBIDDEN" in line:
                    continue
                for call in ORDER_WRITE_CALLS:
                    if f"{call}(" in line:
                        offenders.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], f"order write call: {offenders}")

    def test_the_runtime_still_has_no_third_party_dependencies(self):
        """The strongest form of this guarantee.

        A runtime that imports nothing outside the standard library cannot
        reach a broker, whatever else changes about it.
        """
        requirements = REPO_ROOT / "requirements.txt"
        if requirements.exists():
            declared = [line.strip() for line in
                        requirements.read_text(encoding="utf-8").splitlines()
                        if line.strip() and not line.strip().startswith("#")]
            self.assertEqual(declared, [], f"third-party deps: {declared}")

    def test_the_forbidden_tokens_still_name_order_submission(self):
        """Mutations must remain unable to add what this test forbids.

        If the token list were quietly emptied, a self-improvement patch could
        introduce the capability that every other check here assumes absent.
        """
        for token in ("place_order", "submit_order", "live_order",
                      "order_submission", "broker_execution"):
            self.assertIn(token, FORBIDDEN_MUTATION_TOKENS)


class ARecommendationProducesOnlyAReceiptTests(unittest.TestCase):
    """The strongest decision the system can reach is "recommended".

    It is the case where a careless implementation would act. Running it end
    to end and inspecting everything written is the behavioural half of the
    promise: no order, no instruction queue, no artifact a downstream reader
    could mistake for one.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.journal_path = self.root / "audit.jsonl"
        self.executor = ProductionHostExecutor(AuditJournal(self.journal_path))

    def run_recommending_cycle(self):
        def portfolio(job, context, dependencies):
            return {"status": "completed", "tools_used": ["Interactive Brokers (IBKR)"]}

        def research(job, context, dependencies):
            return {"status": "completed", "tools_used": ["web"]}

        def decision(job, context, dependencies):
            return {"status": "completed", "decision_status": "recommended",
                    "rationale": "strongest possible decision, deliberately",
                    "instruction": "BUY 100 XYZ limit 10.00",
                    "tools_used": []}

        return self.executor.run(
            jobs=[AgentJob("portfolio", "observe"),
                  AgentJob("research", "research", ("portfolio",)),
                  AgentJob("decision", "decision", ("portfolio", "research"))],
            handlers={"portfolio": portfolio, "research": research,
                      "decision": decision},
            context={"snapshot_id": "snap-1"}, cycle_id="c-rec", run_id="r-rec",
            started_at="2026-01-01T00:00:00+00:00",
            host_claim="Ran the stages through real handlers and persisted each checkpoint.",
            self_improvement={"status": "none", "mutation_ids": [], "gates": {}})

    def test_the_recommendation_is_recorded_as_a_recommendation(self):
        _, receipt, _ = self.run_recommending_cycle()
        self.assertEqual(receipt["decision_status"], "recommended")

    def test_nothing_but_the_journal_is_written(self):
        """An order queue would have to live somewhere."""
        self.run_recommending_cycle()
        written = {p.name for p in self.root.rglob("*") if p.is_file()}
        # The lock guards concurrent appends to the journal; it carries no
        # content and is not an artifact a reader could act on.
        written -= {"audit.jsonl.lock"}
        self.assertEqual(written, {"audit.jsonl"},
                         f"unexpected artifacts alongside the journal: {written}")

    def test_no_record_claims_an_order_was_placed(self):
        self.run_recommending_cycle()
        records = [json.loads(line) for line in
                   self.journal_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(records)
        for record in records:
            body = json.dumps(record).lower()
            for token in ("placeorder", "submit_order", "order_submitted",
                          "order_id", "orderid", "filled", "execution_id"):
                self.assertNotIn(token, body,
                                 f"{token} appears in a {record['record_type']}")

    def test_the_instruction_is_preserved_as_text_not_acted_on(self):
        """The host's proposed instruction is evidence, not an order.

        It must survive into the record so a human can review it, while
        remaining inert.
        """
        self.run_recommending_cycle()
        stages = [json.loads(line) for line in
                  self.journal_path.read_text(encoding="utf-8").splitlines()
                  if '"cycle_stage"' in line]
        decisions = [s for s in stages
                     if s["payload"].get("stage_id") == "decision"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["payload"]["output"]["instruction"],
                         "BUY 100 XYZ limit 10.00")

    def test_the_executor_reports_no_tools_beyond_observation(self):
        """A submission would show up as a tool the decision stage used."""
        _, receipt, _ = self.run_recommending_cycle()
        decision_stage = next(s for s in receipt["stages"]
                              if s["stage_id"] == "decision")
        self.assertEqual(list(decision_stage["tools_used"]), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
