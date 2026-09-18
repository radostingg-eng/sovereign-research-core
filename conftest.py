"""Core runs without a profile. Tests that need one say so.

Most of the suite is synthetic: it builds its fixtures in a tmpdir and
never touches operator state. A minority reads the live journal, the
delivery ledger or the archive, and those only mean something when a
profile is attached.

Before the split both kinds passed in the same checkout, because the
checkout WAS the profile. Now that the code ships on its own, a test
that silently reads a missing journal would either fail for everyone who
clones the repo, or -- worse -- be "fixed" by pointing it at whatever
directory happened to be nearby.

So they are marked. Without a profile they skip and say why. With one
they run exactly as before. A skipped test is visible in the summary; a
test quietly reading the wrong tree is not.

    python3 -m pytest runtime/                      # synthetic only
    SOVEREIGN_PROFILE_DIR=~/my-data python3 -m pytest runtime/   # all
"""

import os

import pytest

from runtime.profile_paths import PROFILE_DIR_ENV, profile_root

# Modules whose tests read live operator state rather than building it.
PROFILE_DEPENDENT_MODULES = frozenset({
    "test_archive_integrity",
    "test_delivery_state",
    "test_high_water",
    "test_integrity",
    "test_measurement",
    "test_sandbox",
})

# Classes inside otherwise-synthetic modules that read a real cycle,
# rejected candidate or manifest from the operator's directories. Marked
# individually so the ~1200 synthetic tests around them keep running.
PROFILE_DEPENDENT_CLASSES = frozenset({
    "TheAdoptionHoldoutReachedItsTargetTests",
    "TheOptInPathIsActuallyExercisedTests",
    "ValidationFeedbackPreservesExecutorDataTests",
    "LearningDispositionValidationTests",
    "ExecutionStateIsDerivedTests",
    "FullCycleEnvelopeValidationTests",
    "KnownInstructionRecoveryTests",
    "StagedHostIntakeTests",
    "ToolManifestValidationTests",
})


def _profile_attached() -> bool:
    """A profile is attached when it is set and actually has a journal."""
    if not os.environ.get(PROFILE_DIR_ENV, "").strip():
        return False
    return (profile_root() / "audit").is_dir()


def pytest_collection_modifyitems(config, items):
    if _profile_attached():
        return
    skip = pytest.mark.skip(
        reason=(
            f"needs operator state; set {PROFILE_DIR_ENV} to a profile "
            f"containing audit/ to run it"
        )
    )
    for item in items:
        module = item.module.__name__.rsplit(".", 1)[-1]
        klass = item.cls.__name__ if item.cls else ""
        if (
            module in PROFILE_DEPENDENT_MODULES
            or klass in PROFILE_DEPENDENT_CLASSES
        ):
            item.add_marker(skip)
