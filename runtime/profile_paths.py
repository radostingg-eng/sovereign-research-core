"""Where personal data lives, as distinct from where the code lives.

Until now these were the same directory: the runtime derived its root from
``__file__`` and wrote the audit journal, theses, goals and host inputs
beside its own source. That is why the repository could not be shared --
handing someone the code handed them the operator's portfolio with it.

So there are two roots now, and they answer different questions:

``code_root()``    where the runtime, schemas and generic prompts live.
                   Shared. Identical for every operator. Read-only in
                   normal operation.

``profile_root()`` where one operator's state lives: the journal, theses,
                   goals, preferences, host inputs, portfolio snapshots.
                   Private. Different for every operator. The only place
                   the runtime may write.

``profile_root()`` defaults to ``code_root()`` when ``SOVEREIGN_PROFILE_DIR``
is unset, so an existing single-directory checkout keeps working unchanged.
Setting the variable is what separates them.

``resolve()`` is the guard. Every state path goes through it, and it refuses
anything that would land outside the profile: absolute paths, ``..`` escapes,
and symlinks pointing out of the tree. A path that escapes is a bug that
would write one operator's data into shared code or another operator's
profile, so it raises rather than returning a best guess.
"""

from __future__ import annotations

import os
from pathlib import Path

PROFILE_DIR_ENV = "SOVEREIGN_PROFILE_DIR"

# Directories and files that belong to one operator rather than to the code.
# Kept here rather than spread across modules so that "is this private?" has
# one answer, and so a new state directory cannot be added without appearing
# in this list.
PROFILE_PATHS: tuple[str, ...] = (
    "audit",
    "audit_archive",
    "coordination",
    "experiments",
    "feedback_signals",
    "feedback_staging",
    "goals",
    "host_input",
    "host_staging",
    "portfolio",
    "recommendations",
    "reviews",
    "runs",
    "strategies",
    "theses",
    "tool_artifacts",
    "var",
    "OPERATOR_PREFERENCES.md",
    "PARAMETERS.json",
    "STATE.json",
    "SOURCE_MANIFEST.json",
    "DELIVERY_STATE.json",
)


class ProfileEscape(ValueError):
    """A path resolved outside the profile root."""


def code_root() -> Path:
    """The shared checkout: runtime, schemas, generic prompts."""
    return Path(__file__).resolve().parent.parent


def profile_root() -> Path:
    """This operator's private state directory.

    Falls back to the code root so that an unsplit checkout behaves exactly
    as it did before. Once ``SOVEREIGN_PROFILE_DIR`` is set, no state write
    can reach the shared tree.
    """
    configured = os.environ.get(PROFILE_DIR_ENV)
    if not configured or not configured.strip():
        return code_root()
    return Path(configured).expanduser().resolve()


def is_split() -> bool:
    """True when profile state has been moved out of the code checkout."""
    return profile_root() != code_root()


def resolve(relpath: str | Path) -> Path:
    """Resolve a state path under the profile root, or refuse.

    Deny-by-default: an absolute path, a ``..`` escape, or a symlink whose
    target sits outside the profile raises ``ProfileEscape`` rather than
    silently writing somewhere else.
    """
    candidate = Path(relpath)
    if candidate.is_absolute():
        raise ProfileEscape(f"absolute path is not profile-relative: {relpath}")

    root = profile_root()
    # Resolve the root and the target the same way, so a symlinked profile
    # directory is compared against its own real location rather than being
    # rejected for pointing somewhere else.
    real_root = root.resolve()
    target = (root / candidate).resolve()
    if target != real_root and real_root not in target.parents:
        raise ProfileEscape(
            f"{relpath!r} resolves outside the profile root {real_root}"
        )
    return target


def profile_path(relpath: str | Path) -> Path:
    """``resolve`` with the parent directory created."""
    target = resolve(relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target
