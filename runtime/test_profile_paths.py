"""Code and personal state must not share a directory."""

import os
import tempfile
import unittest
from pathlib import Path

from .profile_paths import (
    PROFILE_DIR_ENV,
    PROFILE_PATHS,
    ProfileEscape,
    code_root,
    is_split,
    profile_path,
    profile_root,
    resolve,
)


class UnsetBehavesLikeBeforeTests(unittest.TestCase):
    """An existing single-directory checkout keeps working untouched."""

    def setUp(self):
        self._saved = os.environ.pop(PROFILE_DIR_ENV, None)

    def tearDown(self):
        if self._saved is not None:
            os.environ[PROFILE_DIR_ENV] = self._saved
        else:
            os.environ.pop(PROFILE_DIR_ENV, None)

    def test_profile_defaults_to_the_code_checkout(self):
        self.assertEqual(profile_root(), code_root())

    def test_unsplit_checkout_reports_itself_as_unsplit(self):
        self.assertFalse(is_split())

    def test_an_empty_value_is_treated_as_unset(self):
        os.environ[PROFILE_DIR_ENV] = "   "
        self.assertEqual(profile_root(), code_root())


class SplitKeepsStateOutOfTheCodeTreeTests(unittest.TestCase):

    def setUp(self):
        self._saved = os.environ.get(PROFILE_DIR_ENV)
        self.profile = Path(tempfile.mkdtemp(prefix="sovereign-profile-"))
        os.environ[PROFILE_DIR_ENV] = str(self.profile)

    def tearDown(self):
        if self._saved is not None:
            os.environ[PROFILE_DIR_ENV] = self._saved
        else:
            os.environ.pop(PROFILE_DIR_ENV, None)

    def test_state_resolves_under_the_profile_not_the_code(self):
        target = resolve("audit/journal.jsonl")
        self.assertTrue(
            target.is_relative_to(self.profile.resolve()), target,
        )
        self.assertFalse(
            target.is_relative_to(code_root()), target,
        )

    def test_a_split_checkout_reports_itself_as_split(self):
        self.assertTrue(is_split())

    def test_profile_path_creates_the_parent_directory(self):
        target = profile_path("goals/open/one.json")
        self.assertTrue(target.parent.is_dir())

    def test_every_declared_state_path_resolves_into_the_profile(self):
        for relpath in PROFILE_PATHS:
            with self.subTest(relpath=relpath):
                self.assertTrue(
                    resolve(relpath).is_relative_to(self.profile.resolve()),
                    relpath,
                )


class EscapesAreRefusedTests(unittest.TestCase):
    """A path that escapes writes one operator's data somewhere shared."""

    def setUp(self):
        self._saved = os.environ.get(PROFILE_DIR_ENV)
        self.profile = Path(tempfile.mkdtemp(prefix="sovereign-profile-"))
        os.environ[PROFILE_DIR_ENV] = str(self.profile)

    def tearDown(self):
        if self._saved is not None:
            os.environ[PROFILE_DIR_ENV] = self._saved
        else:
            os.environ.pop(PROFILE_DIR_ENV, None)

    def test_parent_traversal_is_refused(self):
        with self.assertRaises(ProfileEscape):
            resolve("../elsewhere/state.json")

    def test_traversal_back_into_the_code_tree_is_refused(self):
        with self.assertRaises(ProfileEscape):
            resolve("../core/audit/journal.jsonl")

    def test_an_absolute_path_is_refused(self):
        with self.assertRaises(ProfileEscape):
            resolve("/etc/passwd")

    def test_a_symlink_pointing_out_of_the_profile_is_refused(self):
        outside = Path(tempfile.mkdtemp(prefix="sovereign-outside-"))
        (self.profile / "escape").symlink_to(outside)
        with self.assertRaises(ProfileEscape):
            resolve("escape/state.json")

    def test_the_profile_root_itself_resolves(self):
        self.assertEqual(resolve("."), self.profile.resolve())


class TwoProfilesStayIsolatedTests(unittest.TestCase):
    """The reason the guard exists: A must never reach B."""

    def setUp(self):
        self._saved = os.environ.get(PROFILE_DIR_ENV)
        self.a = Path(tempfile.mkdtemp(prefix="sovereign-a-"))
        self.b = Path(tempfile.mkdtemp(prefix="sovereign-b-"))

    def tearDown(self):
        if self._saved is not None:
            os.environ[PROFILE_DIR_ENV] = self._saved
        else:
            os.environ.pop(PROFILE_DIR_ENV, None)

    def test_each_profile_resolves_only_into_itself(self):
        os.environ[PROFILE_DIR_ENV] = str(self.a)
        from_a = resolve("audit/journal.jsonl")
        os.environ[PROFILE_DIR_ENV] = str(self.b)
        from_b = resolve("audit/journal.jsonl")

        self.assertNotEqual(from_a, from_b)
        self.assertTrue(from_a.is_relative_to(self.a.resolve()))
        self.assertTrue(from_b.is_relative_to(self.b.resolve()))

    def test_one_profile_cannot_address_the_other(self):
        os.environ[PROFILE_DIR_ENV] = str(self.a)
        with self.assertRaises(ProfileEscape):
            resolve(f"../{self.b.name}/audit/journal.jsonl")


if __name__ == "__main__":
    unittest.main()
