from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from version import __version__  # noqa: E402


class ReleaseMetadataTests(unittest.TestCase):
    def test_version_is_semver(self) -> None:
        parts = __version__.split(".")
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(part.isdigit() for part in parts))

    def test_release_metadata_is_synchronized(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_release.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
