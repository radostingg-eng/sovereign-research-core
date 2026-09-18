"""Production modules must use the shared code/profile boundary."""

import ast
import unittest
from pathlib import Path


RUNTIME = Path(__file__).resolve().parent
ALLOWED = {"profile_paths.py"}


def _root_derivation_problems(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    problems = []
    if "Path(__file__).resolve().parent.parent" in source:
        problems.append(f"{path.name}:__file__-derived-root")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and ast.unparse(node.func) == "Path.cwd"
        ):
            problems.append(f"{path.name}:{node.lineno}:Path.cwd")
    return problems


class ProductionPathsUseTheSharedBoundaryTests(unittest.TestCase):

    def test_no_production_module_rediscovers_a_root(self):
        problems = [
            problem
            for path in sorted(RUNTIME.glob("*.py"))
            if not path.name.startswith("test_") and path.name not in ALLOWED
            for problem in _root_derivation_problems(path)
        ]
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
