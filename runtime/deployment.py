"""Applying an approved mutation, verifying it runs, and undoing it.

promote_mutation and rollback_mutation update dictionaries. They do not
apply a commit, do not check that anything still executes, and do not
restore a previous version -- so a "promoted" mutation changed a state file
and nothing else, and a "rollback" marked an entry rolled_back while the
code it was supposed to remove was never there in the first place.

This module does the executable half. Every SHA it reports comes from git
rather than from a caller, and `verified` is the exit code of a command that
actually ran against the applied tree.

Rollback is not best-effort. If verification fails, the parent commit is
restored and the restoration is itself verified; a rollback that silently
failed would leave a broken candidate live while reporting that it had been
removed, which is worse than never having rolled back at all.

Requires a shell with `git` (and `tar` for the export). The CORE cycle
path -- orchestrator, receipts, journal, production_host -- deliberately
does NOT: it is pure stdlib, so a host with repository access but no
shell can still run a full cognitive cycle and persist a receipt. Only
the self-improvement loop needs this module.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .sandbox import DEFAULT_COMMAND, SandboxError
from .self_improvement import MutationProposal, proposal_digest, validate_mutation

_SHA_LEN = 40


@dataclass(frozen=True)
class DeploymentReport:
    """What actually happened to the repository."""

    proposal_digest: str
    parent_sha: str
    candidate_sha: str | None
    applied: bool
    verified: bool
    verify_exit_code: int | None
    command: tuple[str, ...]
    rolled_back: bool
    restored_sha: str | None
    head_after: str
    reason: str
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_digest": self.proposal_digest,
            "parent_sha": self.parent_sha,
            "candidate_sha": self.candidate_sha,
            "applied": self.applied,
            "verified": self.verified,
            "verify_exit_code": self.verify_exit_code,
            "command": list(self.command),
            "rolled_back": self.rolled_back,
            "restored_sha": self.restored_sha,
            "head_after": self.head_after,
            "reason": self.reason,
            "at": self.at,
        }


class DeploymentError(RuntimeError):
    """The repository was not in a state where deployment could be attempted."""


def _git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=120
    )
    if check and result.returncode != 0:
        raise DeploymentError(f"git_failed:{' '.join(args)}:{result.stderr.strip()[:200]}")
    return result


def head_sha(repo_root: Path | str) -> str:
    return _git(Path(repo_root), "rev-parse", "HEAD").stdout.strip()


def working_tree_is_clean(repo_root: Path | str) -> bool:
    return not _git(Path(repo_root), "status", "--porcelain").stdout.strip()


def deploy_candidate(
    proposal: MutationProposal,
    *,
    repo_root: Path | str,
    allowed_prefixes: Sequence[str] = ("runtime/",),
    command: Sequence[str] = DEFAULT_COMMAND,
    timeout_s: int = 300,
    author: str = "self-improvement <self-improvement@sovereign.local>",
) -> DeploymentReport:
    """Apply, commit, verify, and roll back on failure.

    Refuses a dirty working tree. A deployment that cannot tell its own
    change from someone else's cannot honestly roll back: restoring the
    parent commit would discard work it never made.
    """
    repo_root = Path(repo_root).resolve()
    errors = validate_mutation(proposal, allowed_prefixes=allowed_prefixes)
    if errors:
        raise DeploymentError("invalid_proposal:" + ",".join(errors))
    if not proposal.patch.strip():
        raise DeploymentError("empty_patch")
    if not working_tree_is_clean(repo_root):
        raise DeploymentError(
            "working_tree_not_clean: refusing to deploy where a rollback could "
            "discard changes this deployment did not make"
        )

    parent = head_sha(repo_root)
    now = datetime.now(timezone.utc).isoformat()
    digest = proposal_digest(proposal)

    def report(**over: Any) -> DeploymentReport:
        base = dict(
            proposal_digest=digest, parent_sha=parent, candidate_sha=None, applied=False,
            verified=False, verify_exit_code=None, command=tuple(command),
            rolled_back=False, restored_sha=None, head_after=head_sha(repo_root),
            reason="", at=now,
        )
        base.update(over)
        base["head_after"] = head_sha(repo_root)
        return DeploymentReport(**base)

    patch_file = repo_root / ".candidate.patch"
    patch_file.write_text(proposal.patch, encoding="utf-8")
    try:
        applied = subprocess.run(
            ["git", "-C", str(repo_root), "apply", "--whitespace=nowarn", str(patch_file)],
            capture_output=True, text=True, timeout=120,
        )
    finally:
        patch_file.unlink(missing_ok=True)
    if applied.returncode != 0:
        _git(repo_root, "checkout", "--", ".", check=False)
        return report(reason=f"patch_did_not_apply:{applied.stderr.strip()[:200]}")

    _git(repo_root, "add", "-A")
    _git(repo_root, "-c", f"user.name={author.split(' <')[0]}",
         "-c", f"user.email={author.split('<')[-1].rstrip('>')}",
         "commit", "-q", "-m",
         f"self-improvement: apply {proposal.mutation_id}\n\nproposal_digest: {digest}")
    candidate = head_sha(repo_root)
    if candidate == parent:
        return report(reason="patch_was_a_no_op")

    # A timeout used to propagate out of this function with the candidate
    # COMMITTED and live: the exception escaped before the rollback below,
    # so the one path where verification cannot finish was also the one path
    # that left unverified code in place. Any failure to verify must roll
    # back, not just a failing exit code.
    try:
        verify = subprocess.run(
            list(command), cwd=str(repo_root), capture_output=True, text=True,
            timeout=timeout_s,
        )
        verify_code = verify.returncode
        failure_reason = f"verification_failed:exit_code={verify_code}"
    except subprocess.TimeoutExpired:
        verify_code = 124
        failure_reason = f"verification_timed_out_after:{timeout_s}s"
    except OSError as exc:
        verify_code = 125
        failure_reason = f"verification_could_not_run:{type(exc).__name__}"
    if verify_code == 0:
        return report(candidate_sha=candidate, applied=True, verified=True,
                      verify_exit_code=0, reason="verified")

    # Restore, then confirm the restore worked. A rollback that silently
    # failed leaves a broken candidate live while reporting it removed.
    _git(repo_root, "reset", "--hard", parent)
    restored = head_sha(repo_root)
    if restored != parent:
        raise DeploymentError(
            f"rollback_failed:expected={parent[:12]}:head={restored[:12]}"
        )
    return report(candidate_sha=candidate, applied=True, verified=False,
                  verify_exit_code=verify_code, rolled_back=True,
                  restored_sha=restored, reason=failure_reason)


def rollback_to(sha: str, *, repo_root: Path | str,
                command: Sequence[str] = DEFAULT_COMMAND,
                timeout_s: int = 300) -> DeploymentReport:
    """Restore a known commit and verify the restored tree runs."""
    repo_root = Path(repo_root).resolve()
    sha = str(sha).strip()
    if len(sha) != _SHA_LEN:
        raise DeploymentError(f"malformed_restore_sha:{sha!r}")
    parent = head_sha(repo_root)
    _git(repo_root, "reset", "--hard", sha)
    restored = head_sha(repo_root)
    if restored != sha:
        raise DeploymentError(f"rollback_failed:expected={sha[:12]}:head={restored[:12]}")
    verify = subprocess.run(
        list(command), cwd=str(repo_root), capture_output=True, text=True, timeout=timeout_s
    )
    return DeploymentReport(
        proposal_digest="", parent_sha=parent, candidate_sha=None, applied=False,
        verified=verify.returncode == 0, verify_exit_code=verify.returncode,
        command=tuple(command), rolled_back=True, restored_sha=restored,
        head_after=restored,
        reason="restored" if verify.returncode == 0 else "restored_but_unverified",
        at=datetime.now(timezone.utc).isoformat(),
    )


def verify_deployment_report(report: Any, proposal: MutationProposal) -> list[str]:
    """Why a deployment report does not evidence THIS proposal going live."""
    if report is None:
        return ["deployment_report_missing"]
    errors: list[str] = []
    expected = proposal_digest(proposal)
    actual = str(getattr(report, "proposal_digest", "") or "")
    if actual != expected:
        errors.append(
            f"deployment_report_for_other_proposal:expected={expected[:12]}:"
            f"got={(actual or 'absent')[:12]}"
        )
    for field in ("parent_sha", "candidate_sha"):
        value = str(getattr(report, field, "") or "")
        if len(value) != _SHA_LEN:
            errors.append(f"deployment_report_missing_{field}")
    if getattr(report, "parent_sha", None) == getattr(report, "candidate_sha", None):
        errors.append("deployment_candidate_equals_parent")
    if not getattr(report, "applied", False):
        errors.append("deployment_not_applied")
    if not getattr(report, "verified", False):
        errors.append("deployment_not_verified")
    else:
        # A report can claim verified while contradicting itself. Exit code 7
        # beside verified=True, or a HEAD still pointing at the parent, is a
        # hand-written assertion wearing a deployment result's clothes -- the
        # same shape as a sandbox report claiming passed with a failing exit.
        # An ABSENT exit code was accepted as success, which is the same
        # mistake as reading an absent order_submission_used as a denial. A
        # verification that produced no exit code did not demonstrably pass.
        exit_code = getattr(report, "verify_exit_code", None)
        if exit_code != 0:
            errors.append(f"deployment_verified_without_clean_exit:{exit_code}")
        # Comparing only against the parent let an ABSENT head, and a head
        # pointing at some unrelated commit, both count as verified. The
        # claim is that THIS candidate is live, so only the candidate's own
        # sha evidences it.
        head = str(getattr(report, "head_after", "") or "")
        candidate = str(getattr(report, "candidate_sha", "") or "")
        if not head:
            errors.append("deployment_verified_without_head")
        elif head != candidate:
            errors.append(
                "deployment_verified_but_head_is_not_the_candidate:"
                f"{head[:12]}")
    if getattr(report, "rolled_back", False):
        errors.append("deployment_was_rolled_back")
    return errors
