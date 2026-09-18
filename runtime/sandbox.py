"""Isolated execution of a candidate mutation.

evaluate_mutation took `sandbox_passed` as a boolean the caller supplied.
Nothing verified a sandbox had ever run, so the cheapest way to clear the
sandbox gate was to pass True. This module produces the evidence instead:
a real checkout, a real patch application, a real command, and a report whose
`passed` field is DERIVED from the exit code rather than asserted.

The report is bound to the proposal it was produced for, the same way a
mutation evaluation is, so a report earned by one candidate cannot be spent
on another.

No network. The candidate runs against a clean export of HEAD plus its own
patch, in a temporary directory that is removed afterwards.

Requires a shell with `git` (and `tar` for the export). The CORE cycle
path -- orchestrator, receipts, journal, production_host -- deliberately
does NOT: it is pure stdlib, so a host with repository access but no
shell can still run a full cognitive cycle and persist a receipt. Only
the self-improvement loop needs this module.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .self_improvement import MutationProposal, proposal_digest, validate_mutation

DEFAULT_COMMAND: tuple[str, ...] = ("python3", "-m", "compileall", "-q", "runtime")
DEFAULT_TIMEOUT_S = 300
_STDOUT_TAIL = 4000


@dataclass(frozen=True)
class SandboxReport:
    """What actually happened when the candidate ran.

    `passed` is not an input. It is exit_code == 0, and verify_sandbox_report
    refuses a report where the two disagree.
    """

    proposal_digest: str
    passed: bool
    exit_code: int
    command: tuple[str, ...]
    tree_digest: str
    stdout_tail: str
    duration_s: float
    started_at: str
    stage: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_digest": self.proposal_digest,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "command": list(self.command),
            "tree_digest": self.tree_digest,
            "stdout_tail": self.stdout_tail,
            "duration_s": self.duration_s,
            "started_at": self.started_at,
            "stage": self.stage,
        }


class SandboxError(RuntimeError):
    """The candidate could not be executed, which is not the same as failing.

    A patch that does not apply has not been tested. Reporting that as a
    failed run would be as wrong as reporting it as a pass: in both cases
    the evidence does not exist.
    """


def _run(command: Sequence[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command), cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
    )


def _tree_digest(root: Path) -> str:
    """Digest of the candidate tree, so a report names the code it ran."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".git/"):
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def export_head(repo_root: Path, destination: Path) -> None:
    """Clean export of tracked files at HEAD, without the working tree's mess."""
    destination.mkdir(parents=True, exist_ok=True)
    # Streamed rather than captured: git archive emits a tar stream, and
    # decoding it as text to pass through a variable corrupts it.
    with subprocess.Popen(
        ["git", "-C", str(repo_root), "archive", "HEAD"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as source:
        extract = subprocess.run(
            ["tar", "-x", "-C", str(destination)], stdin=source.stdout,
            capture_output=True,
        )
        source.stdout.close()
        archive_code = source.wait()
        archive_error = source.stderr.read().decode(errors="replace")
    if archive_code != 0:
        raise SandboxError(f"git_archive_failed:{archive_error.strip()[:200]}")
    if extract.returncode != 0:
        raise SandboxError(f"archive_extract_failed:{extract.stderr.decode()[:200]}")


def run_candidate(
    proposal: MutationProposal,
    *,
    repo_root: Path | str,
    allowed_prefixes: Sequence[str] = ("runtime/",),
    command: Sequence[str] = DEFAULT_COMMAND,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    stage: str = "sandbox",
) -> SandboxReport:
    """Apply the proposal in a throwaway checkout and run a command against it.

    Refuses to run a proposal that does not pass validate_mutation. Executing
    an out-of-scope patch to see what happens is how a sandbox becomes the
    thing that applies the change.
    """
    errors = validate_mutation(proposal, allowed_prefixes=allowed_prefixes)
    if errors:
        raise SandboxError("invalid_proposal:" + ",".join(errors))
    if not proposal.patch.strip():
        raise SandboxError("empty_patch")

    repo_root = Path(repo_root).resolve()
    started = datetime.now(timezone.utc)
    workspace = Path(tempfile.mkdtemp(prefix="sovereign-candidate-"))
    try:
        checkout = workspace / "tree"
        export_head(repo_root, checkout)

        patch_file = workspace / "candidate.patch"
        patch_file.write_text(proposal.patch, encoding="utf-8")
        applied = _run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file)], checkout, 120
        )
        if applied.returncode != 0:
            # Not a test failure. The candidate never ran.
            raise SandboxError(f"patch_did_not_apply:{applied.stderr.strip()[:300]}")

        try:
            result = _run(command, checkout, timeout_s)
            exit_code = result.returncode
            output = (result.stdout or "") + (result.stderr or "")
        except subprocess.TimeoutExpired:
            exit_code = 124
            output = f"timeout after {timeout_s}s"

        return SandboxReport(
            proposal_digest=proposal_digest(proposal),
            passed=exit_code == 0,
            exit_code=exit_code,
            command=tuple(command),
            tree_digest=_tree_digest(checkout),
            stdout_tail=output[-_STDOUT_TAIL:],
            duration_s=(datetime.now(timezone.utc) - started).total_seconds(),
            started_at=started.isoformat(),
            stage=stage,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def verify_sandbox_report(report: Any, proposal: MutationProposal) -> list[str]:
    """Why a report does not count as evidence for THIS proposal.

    Checks internal consistency as well as binding. A report claiming
    passed=True beside a non-zero exit code is not a sandbox result, it is a
    hand-written assertion wearing one.
    """
    if report is None:
        return ["sandbox_report_missing"]
    errors: list[str] = []
    expected = proposal_digest(proposal)
    actual = str(getattr(report, "proposal_digest", "") or "")
    if actual != expected:
        errors.append(
            f"sandbox_report_for_other_proposal:expected={expected[:12]}:"
            f"got={(actual or 'absent')[:12]}"
        )
    exit_code = getattr(report, "exit_code", None)
    passed = getattr(report, "passed", None)
    if not isinstance(exit_code, int):
        errors.append("sandbox_report_missing_exit_code")
    elif bool(passed) != (exit_code == 0):
        errors.append(f"sandbox_report_inconsistent:passed={passed}:exit_code={exit_code}")
    if not tuple(getattr(report, "command", ()) or ()):
        errors.append("sandbox_report_missing_command")
    tree = str(getattr(report, "tree_digest", "") or "")
    if len(tree) != 64:
        errors.append("sandbox_report_missing_tree_digest")
    return errors
