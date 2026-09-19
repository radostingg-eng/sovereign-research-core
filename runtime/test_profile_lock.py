import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ops.profile_lock import ProfileLockTimeout, profile_lock


class ProfileLockTests(unittest.TestCase):
    def test_second_writer_times_out_while_lock_is_held(self):
        root = Path(tempfile.mkdtemp(prefix="profile-lock-"))
        lock = root / "profile.lock"
        command = [
            sys.executable,
            "-m",
            "ops.profile_lock",
            "--lock",
            str(lock),
            "--timeout",
            "2",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(0.5)",
        ]
        first = subprocess.Popen(command)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        time.sleep(0.1)

        with self.assertRaises(ProfileLockTimeout):
            with profile_lock(lock, timeout_seconds=0.1):
                self.fail("second writer acquired a held lock")

        self.assertEqual(first.wait(timeout=2), 0)
