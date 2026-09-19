"""Runnable integrity gate for the research system.

SELF_INTEGRITY.md specifies the checks this system is supposed to hold
itself to. Until now they were prose: nothing executed them. This module
makes the mechanically-checkable subset executable, and exits non-zero
when one fails.

    cd sovereign-research && python3 -m runtime.integrity

Design notes that are easy to get wrong:

* The journal is ONE chain spanning several files. Sorting filenames does
  not reconstruct it -- "2026-09-15-orchestrator-1205.jsonl" sorts BEFORE
  "2026-09-15.jsonl" because "-" < "." -- and validating each file
  separately reports two false "prev_hash mismatch" defects that are
  really just file boundaries. We rebuild the true order by following
  prev_hash links, which is both correct and independent of how records
  get split across files in future.

* Checks reuse verify_chain / dangling_causes from engine.py rather than
  reimplementing them, so the gate cannot drift from the format the
  writers actually produce.

* Known violations are grandfathered in integrity_baseline.json and the
  gate is SHRINK-ONLY: it refuses anything new, and refuses a baseline
  that lists something already fixed. A gate that fails on day one gets
  switched off on day two.

This module reads. It never writes to the journal and never places an
order.
"""
from __future__ import annotations

from .profile_paths import code_root, profile_root
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .engine import dangling_causes, hash_record, verify_chain
from .tool_artifacts import verify_artifact_records
from .input_artifacts import (
    journal_artifact_references,
    orphan_input_artifacts,
    profile_input_artifact_references,
)

ROOT = code_root()
# The journal is operator state, so it follows the profile rather than the
# code. Unsplit checkouts get the same path as before.
AUDIT_DIR = profile_root() / "audit"
BASELINE_PATH = Path(__file__).resolve().parent / "integrity_baseline.json"

# Referenced-path scan: which files may assert that a repo path exists.
PATH_CLAIM_FILES = ("STATE.json", "SOURCE_MANIFEST.json")

# A repo-relative path claim inside a state file. Two shapes count:
# top-level source files like SYSTEM.md, and paths under a known
# subdirectory like runtime/foo.py or audit/2026-*.jsonl. Deliberately
# excludes URLs (which contain "://") and anything without a source-file
# extension. This regex used to hardcode an "investment-system/" prefix
# when this tree was a subdirectory of another repo; that prefix is gone,
# and adding it back would silently ignore every claim in a state file.
_KNOWN_DIRS = (
    "runtime|audit|coordination|strategies|theses|portfolio|"
    "recommendations|runs|experiments|reviews|e2e|goals|tool_artifacts"
)
_PATH_RE = re.compile(
    rf'"((?:(?:{_KNOWN_DIRS})/[\w./-]+|[A-Z][A-Z_]+)\.(?:py|md|json|jsonl|yml|yaml))"'
)


class Failure:
    """One integrity defect, keyed so the baseline can grandfather it.

    The key carries a DEFECT TYPE, not just the subject. Without it a
    single baseline line covers every possible defect on one record, and
    the shrink-only mechanism silently stops working: grandfather a
    record as an orphan, later repair the orphan, and if that record is
    ALSO content-tampered the same baseline line keeps reproducing. The
    debt looks paid and is not. Defect-typed idents make each defect
    clear (and require removal) independently.
    """

    __slots__ = ("check", "defect", "key", "detail")

    def __init__(self, check: str, key: str, detail: str,
                 defect: str = "generic") -> None:
        self.check = check
        self.defect = defect
        self.key = key
        self.detail = detail

    @property
    def ident(self) -> str:
        return f"{self.check}:{self.defect}:{self.key}"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Failure({self.ident})"


# --------------------------------------------------------------------------
# journal loading
# --------------------------------------------------------------------------

def load_journal_records(audit_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every JSONL record under audit/, in arbitrary order."""
    audit_dir = audit_dir or AUDIT_DIR
    records: list[dict[str, Any]] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{lineno}: invalid JSON: {exc}") from exc
    return records


def order_chain(records: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Failure]]:
    """Rebuild true chain order by following prev_hash.

    Returns (ordered, failures). Records that cannot be reached from a root
    are reported as orphans rather than silently dropped -- a dropped
    record is exactly the kind of hole this gate exists to catch.
    """
    records = list(records)
    failures: list[Failure] = []

    by_prev: dict[str | None, list[dict[str, Any]]] = {}
    for record in records:
        by_prev.setdefault(record.get("prev_hash"), []).append(record)

    roots = by_prev.get(None, [])
    if len(roots) > 1:
        for record in roots[1:]:
            failures.append(
                Failure("chain", _rid(record), "multiple chain roots (prev_hash=null)",
                        defect="multiple_roots")
            )
    if not roots and records:
        failures.append(Failure("chain", "<journal>", "no chain root: every record has a prev_hash",
                                defect="no_root"))
        return records, failures

    # A fork is still reported, but BOTH branches are walked. Following
    # successors[0] and abandoning the rest reported the fork and then
    # stranded every record on the branch it did not take, so a validly
    # linked record was counted unreachable because of a defect in a sibling
    # it had no relationship to. Reporting a problem and also hiding its
    # victims is worse than reporting it alone.
    ordered: list[dict[str, Any]] = []
    seen: set[int] = set()
    stack: list[dict[str, Any]] = [roots[0]] if roots else []
    while stack:
        cursor = stack.pop()
        if id(cursor) in seen:
            failures.append(Failure("chain", _rid(cursor), "cycle in prev_hash links",
                                    defect="cycle"))
            continue
        seen.add(id(cursor))
        ordered.append(cursor)
        successors = by_prev.get(cursor.get("record_hash"), [])
        if len(successors) > 1:
            for extra in successors[1:]:
                failures.append(
                    Failure("chain", _rid(extra),
                            f"fork: two records share prev_hash {_short(cursor)}",
                            defect="fork")
                )
        # Reversed so the first successor is visited first, keeping an
        # unforked chain's order byte-identical to before.
        stack.extend(reversed(successors))

    if len(ordered) != len(records):
        # Attribute every orphan to the break that caused it instead of
        # listing victims. One malformed prev_hash orphans its whole
        # descendant lineage, and each later cycle anchored onto that
        # lineage adds more -- so a victim-keyed baseline grows on every
        # run and a SHRINK-ONLY list that grows is a contradiction. Root
        # keys are stable: the same break claiming more descendants does
        # not manufacture a new defect, while a genuinely new break does.
        by_hash = {
            str(r.get("record_hash")): r for r in records if r.get("record_hash")
        }
        reachable = {id(r) for r in ordered}
        emitted: set[str] = set()
        for record in records:
            if id(record) in seen:
                continue
            origin, kind = _orphan_origin(record, by_hash, reachable)
            if origin is record:
                ident_key, defect = _rid(record), "orphan"
                detail = "orphan: unreachable from the chain root"
            else:
                ident_key, defect = _rid(origin), kind
                detail = (f"{kind}: unreachable because of {_rid(origin)}; "
                          "repair the origin rather than this record")
            if f"{defect}:{ident_key}" in emitted:
                continue
            emitted.add(f"{defect}:{ident_key}")
            failures.append(Failure("chain", ident_key, detail, defect=defect))
    return ordered, failures


def _orphan_origin(record: dict[str, Any], by_hash: dict[str, dict[str, Any]],
                   reachable: set[int]) -> tuple[dict[str, Any], str]:
    """Walk back to whatever actually broke this record's path to the root.

    Two outcomes. Reaching an unresolvable prev_hash means a broken LINK,
    and that record is the origin. Reaching a record that IS on the chain
    means the path exists but was not taken -- order_chain follows a single
    successor, so a fork strands the branch it did not walk.
    """
    current = record
    visited: set[int] = set()
    while True:
        if id(current) in visited:
            return current, "orphan_cycle"
        visited.add(id(current))
        prev = current.get("prev_hash")
        if prev is None:
            return current, "orphan_second_root"
        parent = by_hash.get(str(prev))
        if parent is None:
            return current, "orphan_broken_link"
        if id(parent) in reachable:
            return parent, "orphan_forked_from"
        current = parent


def _rid(record: dict[str, Any]) -> str:
    return str(record.get("record_id") or "<no record_id>")


def _short(record: dict[str, Any]) -> str:
    return str(record.get("record_hash"))[:12]


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def _classify_chain_defect(detail: str) -> str:
    """Map a verify_chain defect string to a stable defect type.

    verify_chain returns prose. The baseline needs a stable key, so the
    prose is classified once here rather than being embedded in a
    baseline line that would churn whenever the wording changes.
    """
    lowered = detail.lower()
    if "record_hash" in lowered:
        return "content_mismatch"
    if "prev_hash" in lowered:
        return "prev_mismatch"
    if "duplicate" in lowered:
        return "duplicate_id"
    return "generic"


def check_record_validity(records: list[dict[str, Any]]) -> list[Failure]:
    """Validate EVERY parsed record independently of chain reachability.

    This runs before ordering, deliberately. verify_chain only inspects
    records on the selected chain path, so a tampered record that is also
    orphaned is invisible to it -- in this journal that was two of three
    content-tamper records, and seven records in total were never
    content-validated at all. Reachability is a graph property; "these
    bytes hash to this value" is not, and must not be conditional on it.

    Checks, each with its own defect type so the shrink-only baseline can
    distinguish them:

    * content_mismatch -- stored record_hash does not match the hash of
      the record's own content
    * duplicate_id     -- the same record_id appears more than once
    * duplicate_hash   -- two distinct records share a record_hash, which
      makes prev_hash references ambiguous
    """
    failures: list[Failure] = []

    seen_ids: dict[str, int] = {}
    seen_hashes: dict[str, list[str]] = {}

    for record in records:
        rid = _rid(record)
        seen_ids[rid] = seen_ids.get(rid, 0) + 1

        stored = record.get("record_hash")
        if isinstance(stored, str):
            seen_hashes.setdefault(stored, []).append(rid)
            computed = hash_record(record)
            if stored != computed:
                failures.append(Failure(
                    "record", rid,
                    f"content does not hash to its stored record_hash "
                    f"(stored {stored[:12]}..., computed {computed[:12]}...)",
                    defect="content_mismatch"))

    for rid, count in sorted(seen_ids.items()):
        if count > 1:
            failures.append(Failure(
                "record", rid, f"record_id appears {count} times",
                defect="duplicate_id"))

    for digest, owners in sorted(seen_hashes.items()):
        distinct = sorted(set(owners))
        if len(distinct) > 1:
            failures.append(Failure(
                "record", digest[:16],
                f"record_hash shared by {len(distinct)} records: "
                f"{', '.join(distinct)}; prev_hash references are ambiguous",
                defect="duplicate_hash"))

    return failures


def check_causal_integrity(records: list[dict[str, Any]]) -> list[Failure]:
    """SELF_INTEGRITY.md 'Causal integrity': chain intact, no dangling caused_by."""
    ordered, failures = order_chain(records)
    for defect in verify_chain(ordered):
        rid, _, detail = defect.partition(": ")
        failures.append(Failure("chain", rid, detail or defect,
                                defect=_classify_chain_defect(detail or defect)))
    for ref in dangling_causes(ordered):
        failures.append(Failure("causal", str(ref), "caused_by points at no known record",
                                  defect="dangling"))
    return failures


_HEX_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def check_hash_format(records: list[dict[str, Any]]) -> list[Failure]:
    """Every record's record_hash and prev_hash must be either null (root only)
    or exactly 64 lowercase hex characters.

    Exists because a sovereign-host E2E run wrote a receipt with a prev_hash
    truncated to 63 characters (final `b` dropped). Without this check the
    truncation surfaces as a chain-orphan cascade three records downstream,
    which points at the wrong writer to fix. A one-character truncation is a
    specific class of bug worth catching immediately at the writer, not in the
    consumer four hops away.
    """
    failures: list[Failure] = []
    for record in records:
        rid = str(record.get("record_id") or "<no record_id>")
        stored = record.get("record_hash")
        if not isinstance(stored, str) or not _HEX_HASH_RE.match(stored):
            failures.append(Failure(
                "hash_format", rid,
                f"record_hash is not 64 lowercase hex: {stored!r}",
                defect="bad_record_hash"))
        prev = record.get("prev_hash")
        if prev is None:
            continue
        if not isinstance(prev, str) or not _HEX_HASH_RE.match(prev):
            n = len(prev) if isinstance(prev, str) else "N/A"
            failures.append(Failure(
                "hash_format", rid,
                f"prev_hash malformed (len={n}, expected 64 lowercase hex): {prev!r}",
                defect="bad_prev_hash"))
    return failures


def check_state_integrity(root: Path | None = None) -> list[Failure]:
    """SELF_INTEGRITY.md 'State integrity': required files readable, JSON parses."""
    root = root or ROOT
    failures: list[Failure] = []
    for name in PATH_CLAIM_FILES:
        path = root / name
        if not path.exists():
            failures.append(Failure("state", name, "required state file missing", defect="missing"))
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(Failure("state", name, f"invalid JSON: {exc}", defect="invalid_json"))
    return failures


def check_tool_artifacts(
    records: Sequence[Mapping[str, Any]],
    root: Path | None = None,
) -> list[Failure]:
    failures = []
    for problem in verify_artifact_records(
        records,
        profile_root=root or profile_root(),
    ):
        if problem.endswith(":index_missing"):
            failures.append(Failure(
                "tool_provenance",
                problem,
                "required tool provenance index is missing",
                "index_missing",
            ))
        elif (
            problem.endswith(":capture_fields_missing")
            or problem.endswith(":calls_invalid")
            or problem.endswith(":call_not_object")
        ):
            failures.append(Failure(
                "tool_provenance",
                problem,
                "schema-v4 tool provenance index shape is incomplete",
                "index_shape",
            ))
        else:
            failures.append(Failure(
                "tool_artifact",
                problem,
                "private canonical tool response artifact is missing or "
                "changed",
                "artifact_integrity",
            ))
    return failures


def check_input_artifact_orphans(
    records: Sequence[Mapping[str, Any]],
    root: Path | None = None,
) -> list[Failure]:
    profile = (root or profile_root()).resolve()
    referenced = profile_input_artifact_references(profile)
    referenced.update(journal_artifact_references(tuple(records)))
    return [
        Failure(
            "input_artifact",
            path,
            "content-addressed input artifact has no cycle, staged, rejected, "
            "or journal reference",
            "orphan",
        )
        for path in orphan_input_artifacts(
            profile_root=profile,
            referenced_digests=referenced,
        )
    ]


def check_referenced_paths(root: Path | None = None) -> list[Failure]:
    """Every repo path a state file claims exists must actually exist.

    This is the check that catches an agent describing files it never
    wrote, and the slower drift where a file is renamed and the manifest
    still names the old one. Persisted audit JSONL is included because it
    is part of the causal state that STATE.json can legitimately reference.
    """
    root = root or ROOT
    # When the project is a subdirectory of another repo, path claims
    # were resolved against root.parent. Now the project is its own repo
    # and ROOT is that repo's root, so claims resolve against root itself.
    repo_root = root
    failures: list[Failure] = []
    for name in PATH_CLAIM_FILES:
        path = root / name
        if not path.exists():
            continue
        for claimed in sorted(set(_PATH_RE.findall(path.read_text(encoding="utf-8")))):
            if not (repo_root / claimed).exists():
                failures.append(
                    Failure("path", claimed, f"{name} references a path that does not exist",
                            defect="missing")
                )
    return failures


def _imported_modules(source: str) -> set[str]:
    """Modules actually imported by this source, via AST.

    Deliberately not a regex. A pattern like r"\\bstress\\." matches
    portfolio.py's own stress_nav, and r"\\bintegrity\\." matches
    governance.py's docstring ending "...and integrity." -- which is
    exactly how this module escaped its own adoption check on the first
    version of this gate. Parsing imports is the only way to tell a
    reference from a mention.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from .stress import x` -> node.module == "stress", level == 1
            if node.level and node.module:
                found.add(node.module.split(".")[0])
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
    return found


def _cli_entry_points(root: Path) -> set[str]:
    """Modules invoked as `python -m <pkg>.<module>` by executable config.

    A module with a __main__ block is reached by a workflow or a runbook,
    not by an import, so the import graph alone would call it dead. Only
    counts if something actually invokes it: having a __main__ block is
    not on its own evidence that anyone runs it.

    Documentation deliberately does NOT count. A sentence in any Markdown
    file used to confer adoption, including records under audit/ and
    coordination/ that helper agents append. DELIVERY_PLAN.md says
    documentation alone does not satisfy an item; the gate now agrees.
    """
    invoked: set[str] = set()
    pattern = re.compile(
        r"python[0-9.]*(?:\s+-[A-Za-z]+)*\s+-m\s+[\w.]*\b(\w+)\b"
    )
    workflow_dirs = [
        root / ".github" / "workflows",
        root / "profile_templates" / ".github" / "workflows",
    ]
    workflow_paths = [
        path
        for directory in workflow_dirs
        if directory.exists()
        for path in [
            *directory.glob("*.yml"),
            *directory.glob("*.yaml"),
        ]
    ]
    if not workflow_paths:
        workflow_paths = [
            *root.glob("*.yml"),
            *root.glob("*.yaml"),
        ]
    for path in workflow_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in pattern.finditer(text):
            invoked.add(match.group(1))

    # Committed JSON can invoke a module too, and it does not look like a
    # shell string: evaluation_tasks.json declares commands as ARRAYS, so
    # "-m" and "runtime.eval_tasks" are separate elements and the regex
    # above cannot see them. That reference is real, committed and
    # machine-checkable, so it counts as adoption.
    for path in root.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        for command in _command_arrays(data):
            for index, token in enumerate(command[:-1]):
                if token == "-m":
                    invoked.add(str(command[index + 1]).split(".")[-1])
    return invoked


def _command_arrays(node: Any) -> Iterable[list[str]]:
    """Every list-of-strings anywhere in a JSON document."""
    if isinstance(node, list):
        if node and all(isinstance(x, str) for x in node):
            yield list(node)
        for item in node:
            yield from _command_arrays(item)
    elif isinstance(node, dict):
        for item in node.values():
            yield from _command_arrays(item)


def scheduled_workflows(repo_root: Path | None = None) -> list[str]:
    """Workflow files carrying a `schedule:` trigger.

    The single-scheduler contract is only meaningful if something counts
    them. Two scheduled full-cycle workflows would race on the audit
    journal, which is append-only and therefore unrecoverable by retry.
    """
    repo_root = repo_root or ROOT
    workflows = repo_root / ".github" / "workflows"
    if not workflows.exists():
        return []
    found = []
    for path in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            # An exact match on the stripped line is the whole guard. A
            # commented line keeps its "#" through strip(), and prose like
            # "we removed the schedule: it raced" keeps its trailing words,
            # so neither can match. An explicit startswith("#") test here
            # looked prudent and was unreachable -- mutation testing caught
            # it surviving, which is how dead code in a gate gets found.
            if line.strip() == "schedule:":
                found.append(path.name)
                break
    return found


def check_single_scheduler(repo_root: Path | None = None) -> list[Failure]:
    """AGENT_ORCHESTRATOR.md allows exactly one enabled production scheduler.

    Two scheduled full-cycle workflows would interleave appends to the
    audit journal. Append-only means a corrupted interleaving cannot be
    fixed by re-running, so this is enforced rather than advised.

    Zero schedulers is not a failure here: the repository is allowed to
    have the cycle disabled. More than one never is.
    """
    found = scheduled_workflows(repo_root)
    if len(found) <= 1:
        return []
    return [
        Failure("scheduler", name,
                f"{len(found)} workflows carry a schedule: trigger; the contract allows one",
                defect="multiple")
        for name in found
    ]


def _runtime_import_graph(runtime_dir: Path) -> dict[str, set[str]]:
    """Imports between production runtime modules.

    __init__.py is intentionally excluded. Re-exporting a module makes it
    available to a library caller; it does not prove any caller exists.
    """
    modules = {
        path.stem: path
        for path in runtime_dir.glob("*.py")
        if path.stem != "__init__" and not path.stem.startswith("test_")
    }
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        imported = _imported_modules(path.read_text(encoding="utf-8"))
        graph[name] = {module for module in imported if module in modules}
    return graph


def _reachable_modules(graph: Mapping[str, set[str]],
                       roots: Iterable[str]) -> set[str]:
    """Transitive closure from executable entry points."""
    reached: set[str] = set()
    pending = [root for root in roots if root in graph]
    while pending:
        module = pending.pop()
        if module in reached:
            continue
        reached.add(module)
        pending.extend(graph[module] - reached)
    return reached


def check_runtime_adoption(runtime_dir: Path | None = None,
                           search_roots: Iterable[Path] | None = None) -> list[Failure]:
    """Every runtime module must be transitively reachable from a real root.

    A module that only its own tests import is not part of the system:
    it is tested, cited in state files, and never actually reached. The
    trading engine in this repo hit that exact shape five separate times,
    including on a fix that had been merged for 55 commits without ever
    taking effect. Cheapest possible guard, so it runs here from day one.

    A one-hop import is not enough. `learning.py` importing five helpers did
    not make any of the six live when learning.py itself had no caller.
    Adoption is the transitive closure from modules invoked by committed
    workflows or command arrays.
    """
    runtime_dir = runtime_dir or Path(__file__).resolve().parent
    if search_roots is None:
        # Derived from this file's real location, NOT from runtime_dir.
        # Deriving from the argument means a caller passing a temp
        # directory makes this walk the whole system temp tree, which is
        # unbounded and hangs. Callers testing a synthetic package should
        # pass search_roots explicitly.
        search_roots = [ROOT]

    invoked: set[str] = set()
    for root in search_roots:
        if root.exists():
            invoked |= _cli_entry_points(root)

    graph = _runtime_import_graph(runtime_dir)
    reachable = _reachable_modules(graph, invoked)
    failures: list[Failure] = []
    for module in sorted(set(graph) - reachable):
        failures.append(
            Failure("adoption", module,
                    "not transitively reachable from a workflow/command entry point",
                    defect="unreachable")
        )
    return failures


SUPERSESSION_TYPE = "supersession"
MIN_SUPERSESSION_REASON = 30


def supersession_errors(record: Mapping[str, Any],
                        by_id: Mapping[str, dict[str, Any]]) -> list[str]:
    """Why a supersession record does not authorize anything.

    Every baseline entry says a damaged record must be "resolved only by
    appending a superseding replacement", and until now there was no way to
    append one. A documented recovery path with no implementation means the
    only route out of a defect was editing the baseline, which is how the
    list became permanent.

    A supersession is an administrative act on the audit trail, so it is
    bound tightly: it must name a record that exists, quote that record's
    CURRENT broken hash, name the defect it retires, and give a reason long
    enough to be an explanation. Quoting the observed hash is what stops a
    supersession being written ahead of time or retargeted later -- it is
    only valid against the exact damaged state it describes.
    """
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return ["supersession_payload_must_be_object"]
    errors: list[str] = []
    target_id = str(payload.get("supersedes") or "")
    if not target_id:
        errors.append("supersession_missing_target")
    elif target_id not in by_id:
        errors.append(f"supersession_target_not_in_journal:{target_id}")
    if not str(payload.get("defect") or "").strip():
        errors.append("supersession_missing_defect")
    reason = str(payload.get("reason") or "").strip()
    if len(reason) < MIN_SUPERSESSION_REASON:
        errors.append("supersession_reason_too_short")
    observed = str(payload.get("observed_hash") or "")
    if not observed:
        errors.append("supersession_missing_observed_hash")
    elif target_id in by_id and observed != str(by_id[target_id].get("record_hash")):
        # Bound to the damaged state it describes, not to the record's name.
        errors.append(f"supersession_observed_hash_stale:{target_id}")
    return errors


def superseded_defects(records: Sequence[dict[str, Any]]) -> set[tuple[str, str]]:
    """(record_id, defect) pairs retired by a VALID supersession record.

    A supersession only counts when it is itself sound and reachable from
    the chain root. That is the whole protection: retiring a defect requires
    writing into the part of the journal that still verifies, where the act
    is attributable and auditable, rather than editing a JSON allowlist.
    """
    ordered, _ = order_chain(records)
    reachable = {_rid(r) for r in ordered}
    by_id = {_rid(r): r for r in records}
    retired: set[tuple[str, str]] = set()
    for record in records:
        if record.get("record_type") != SUPERSESSION_TYPE:
            continue
        if _rid(record) not in reachable:
            continue
        if record.get("record_hash") != hash_record(dict(record)):
            continue
        if supersession_errors(record, by_id):
            continue
        payload = record["payload"]
        retired.add((str(payload["supersedes"]), str(payload["defect"])))
    return retired


def run_all(records: list[dict[str, Any]] | None = None) -> list[Failure]:
    records = load_journal_records() if records is None else records
    failures: list[Failure] = []
    failures += check_state_integrity()
    failures += check_referenced_paths()
    failures += check_hash_format(records)
    failures += check_record_validity(records)
    failures += check_causal_integrity(records)
    failures += check_tool_artifacts(records)
    failures += check_input_artifact_orphans(records)
    failures += check_runtime_adoption()
    failures += check_single_scheduler()
    failures += check_supersession_records(records)
    failures += check_journal_not_truncated(records)
    failures += check_schema_versions()
    retired = superseded_defects(records)
    return [f for f in failures if (f.key, f.defect) not in retired]


# Each state file declares a schema_version that NOTHING read. Three
# decorative integers, free to drift from the shape of the file they claim to
# describe, and nothing to notice when a field was added or removed.
#
# These are not meant to equal each other: they are three different schemas
# with independent histories, so the SELF_INTEGRITY bullet cannot mean
# cross-file equality. What it can mean, and what this enforces, is that the
# declared version agrees with what the runtime expects. Bumping a schema now
# fails the gate until someone changes this line, which is the moment to ask
# whether the readers were updated too.
EXPECTED_SCHEMA_VERSIONS: dict[str, int] = {
    "STATE.json": 14,
    "PARAMETERS.json": 2,
    "DELIVERY_STATE.json": 1,
}


def check_schema_versions() -> list[Failure]:
    """Declared schema versions match what the runtime expects."""
    failures: list[Failure] = []
    for filename, expected in sorted(EXPECTED_SCHEMA_VERSIONS.items()):
        path = ROOT / filename
        if not path.exists():
            failures.append(Failure(
                "state", f"schema_version:{filename}:missing_file",
                "a file declaring a pinned schema version is gone",
                "schema_version_drift"))
            continue
        try:
            declared = json.loads(path.read_text(encoding="utf-8")).get(
                "schema_version")
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(Failure(
                "state", f"schema_version:{filename}:unreadable",
                f"cannot read a pinned schema version: {exc}",
                "schema_version_drift"))
            continue
        if declared != expected:
            failures.append(Failure(
                "state",
                f"schema_version:{filename}:declared={declared}:expected={expected}",
                "schema version changed without updating the runtime's "
                "expectation, so readers may be parsing a shape they were "
                "not written for",
                "schema_version_drift"))
    return failures


def check_journal_not_truncated(records: Sequence[dict[str, Any]]) -> list[Failure]:
    """The chain cannot police its own tail.

    Every record's hash covers the one before it, so a REWRITE is caught.
    Dropping the last N records is not: a prefix of a valid chain is a valid
    chain, and it verified clean at every depth tried. The newest records are
    the receipts saying what the system just decided, so that is the cheapest
    thing to erase and the least likely to be noticed.

    The high-water mark lives outside the journal, because the journal is the
    thing being truncated.
    """
    from .high_water import check_high_water

    return [
        Failure("chain", f"journal_high_water:{problem}",
                "audit journal shrank or lost its recorded tip",
                "journal_truncated")
        for problem in check_high_water(records, AUDIT_DIR)
    ]


def check_supersession_records(records: Sequence[dict[str, Any]]) -> list[Failure]:
    """A malformed supersession is itself a defect, not a silent no-op.

    Dropping invalid ones without reporting would let a broken recovery
    attempt look like no attempt at all.
    """
    by_id = {_rid(r): r for r in records}
    failures: list[Failure] = []
    for record in records:
        if record.get("record_type") != SUPERSESSION_TYPE:
            continue
        for error in supersession_errors(record, by_id):
            failures.append(
                Failure("supersession", _rid(record), error, defect="invalid")
            )
    return failures


# --------------------------------------------------------------------------
# shrink-only baseline
# --------------------------------------------------------------------------

def load_baseline(path: Path | None = None) -> dict[str, str]:
    path = path or BASELINE_PATH
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("grandfathered", {}))


def apply_baseline(
    failures: list[Failure], baseline: dict[str, str]
) -> tuple[list[Failure], list[str]]:
    """Split failures into (new, ...) and report baseline entries now fixed.

    Shrink-only: a baseline entry whose defect no longer reproduces is
    itself an error, so the list cannot quietly keep granting permission
    for something already repaired.
    """
    live = {f.ident for f in failures}
    new = [f for f in failures if f.ident not in baseline]
    stale = sorted(ident for ident in baseline if ident not in live)
    return new, stale


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    strict = "--strict" in argv

    try:
        records = load_journal_records()
    except ValueError as exc:
        print(f"FAIL  journal unreadable: {exc}")
        return 1

    failures = run_all(records)
    baseline = load_baseline()
    new, stale = apply_baseline(failures, baseline)
    grandfathered = [f for f in failures if f.ident in baseline]

    print(f"records checked: {len(records)}")
    for check in ("state", "path", "hash_format", "record", "chain",
                  "causal", "adoption", "scheduler"):
        hits = [f for f in new if f.check == check]
        status = "FAIL" if hits else "ok"
        print(f"  {status:4} {check}")
        for failure in hits:
            print(f"         {failure.key}: {failure.detail}")

    if grandfathered:
        print(f"\ngrandfathered ({len(grandfathered)}), see integrity_baseline.json:")
        for failure in grandfathered:
            print(f"  - {failure.ident}: {baseline.get(failure.ident, '')}")

    if stale:
        print("\nbaseline entries that no longer reproduce -- delete them:")
        for ident in stale:
            print(f"  - {ident}")

    if new or (strict and grandfathered) or stale:
        print(f"\nintegrity: FAIL ({len(new)} new, {len(stale)} stale baseline)")
        return 1
    print("\nintegrity: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
