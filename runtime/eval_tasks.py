"""Fixed evaluation tasks, addressed by name.

measurement.py could run tasks and nothing shipped any, so every caller had
to invent its own. Tasks invented per run are neither fixed nor protected:
whoever picks them can pick ones that flatter the candidate, which is the
measurement equivalent of grading your own exam.

Each task prints exactly ONE number to stdout and nothing else, because that
is the contract measurement.py reads. Each is deterministic, needs no
network, and runs against a bare export of the tree with no .git directory,
since that is what the sandbox gives it.

Every task here counts something that should go DOWN. There is deliberately
no "number of tests" or "lines of code" task: those reward volume, and a
candidate can always add more of either.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from .cycle_receipt import validate_audit_receipt_record
from .engine import dangling_causes
from .integrity import load_journal_records, order_chain, run_all
from .profile_paths import code_root


def integrity_open_findings() -> int:
    """Findings the integrity gate reports, after supersession."""
    return len(run_all())


def journal_unreachable_records() -> int:
    """Records stranded from the chain root."""
    records = load_journal_records()
    ordered, _ = order_chain(records)
    return len(records) - len(ordered)


def invalid_stored_receipts() -> int:
    """Persisted cycle receipts that fail envelope validation."""
    return sum(
        1 for record in load_journal_records()
        if record.get("record_type") == "cycle_receipt"
        and validate_audit_receipt_record(record)
    )


def dangling_causal_links() -> int:
    """caused_by references pointing at records that are not there."""
    return len(dangling_causes(load_journal_records()))


def transitive_unreachable_modules() -> int:
    """Modules unreachable from committed executable roots.

    This is deliberately independent of integrity.check_runtime_adoption.
    The development metric may improve because that gate was weakened; a
    protected task that calls the same gate would move with the lie and stop
    being a holdout.
    """
    root = code_root()
    runtime_dir = root / "runtime"
    modules = {
        path.stem: path
        for path in runtime_dir.glob("*.py")
        if path.stem != "__init__" and not path.stem.startswith("test_")
    }
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level and node.module:
                    imported = node.module.split(".")[0]
                    if imported in modules:
                        graph[name].add(imported)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if (len(parts) >= 2 and parts[0] == "runtime"
                            and parts[1] in modules):
                        graph[name].add(parts[1])

    roots: set[str] = set()
    pattern = re.compile(r"python[0-9.]*\s+-m\s+runtime\.(\w+)\b")
    workflows = root / ".github" / "workflows"
    for path in list(workflows.glob("*.yml")) + list(
            workflows.glob("*.yaml")):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            roots.add(match.group(1))

    def command_arrays(node: Any) -> Iterable[list[str]]:
        if isinstance(node, list):
            if node and all(isinstance(value, str) for value in node):
                yield list(node)
            for value in node:
                yield from command_arrays(value)
        elif isinstance(node, dict):
            for value in node.values():
                yield from command_arrays(value)

    for path in root.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for command in command_arrays(value):
            for index, token in enumerate(command[:-1]):
                if token == "-m" and command[index + 1].startswith("runtime."):
                    roots.add(command[index + 1].split(".")[-1])

    reached: set[str] = set()
    pending = [name for name in roots if name in graph]
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending.extend(graph[name] - reached)
    return len(set(modules) - reached)


TASKS = {
    "integrity_open_findings": integrity_open_findings,
    "journal_unreachable_records": journal_unreachable_records,
    "invalid_stored_receipts": invalid_stored_receipts,
    "dangling_causal_links": dangling_causal_links,
    "transitive_unreachable_modules": transitive_unreachable_modules,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in TASKS:
        print(f"usage: python3 -m runtime.eval_tasks <{'|'.join(sorted(TASKS))}>",
              file=sys.stderr)
        return 2
    print(TASKS[argv[0]]())
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
