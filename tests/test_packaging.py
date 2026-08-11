from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_pyrtlsdrlib_is_x86_64_only(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        dependencies = project["dependencies"]
        pyrtlsdrlib = [dependency for dependency in dependencies if dependency.startswith("pyrtlsdrlib")]

        self.assertEqual(len(pyrtlsdrlib), 1)
        self.assertIn("platform_machine == 'x86_64'", pyrtlsdrlib[0])
        self.assertIn("platform_machine == 'AMD64'", pyrtlsdrlib[0])

    def test_pyrtlsdr_python_wrapper_remains_unconditional(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

        self.assertIn("pyrtlsdr", project["dependencies"])


if __name__ == "__main__":
    unittest.main()
