"""The promotion path was unreachable, and the adoption debt opened it.

Every task originally counted defects already at zero, while the evidence
gate requires a strictly positive improvement. Correcting the adoption gate
exposed real nonzero debt, and an independently implemented protected task
now measures the same outcome without trusting the gate under mutation.
See #62 and #63.
"""

import unittest

from .eval_tasks import transitive_unreachable_modules


class TheAdoptionHoldoutReachedItsTargetTests(unittest.TestCase):
    """The holdout enabled a real promotion, then returned to zero."""

    def test_the_protected_task_remains_in_the_corpus(self):
        from .measurement import default_tasks

        tasks = default_tasks()
        protected = {task.task_id for task in tasks if task.protected}
        self.assertIn("transitive_unreachable_modules", protected)

    def test_every_runtime_module_is_now_reachable(self):
        self.assertEqual(transitive_unreachable_modules(), 0)

    def test_the_gate_requires_strictly_positive_improvement(self):
        """The promotion that closed the debt had to lower it, not hold flat."""
        import inspect
        from .self_improvement import _evaluate_mutation

        signature = inspect.signature(_evaluate_mutation)
        self.assertEqual(signature.parameters["min_primary_delta"].default, 0.0)

    def test_the_holdout_does_not_call_the_adoption_gate(self):
        """Weakening check_runtime_adoption must not weaken the holdout."""
        import ast
        import inspect

        source = inspect.getsource(transitive_unreachable_modules)
        tree = ast.parse(source)
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("runtime.integrity", imported)
        self.assertNotIn("check_runtime_adoption", calls)
        self.assertNotIn("run_all", calls)
