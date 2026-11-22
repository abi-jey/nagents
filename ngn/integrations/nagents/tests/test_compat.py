"""The marker package answers the update probe of ngn <= 0.100.46 and runs nothing."""

from __future__ import annotations

import subprocess
import sys
import unittest

import ngn_nagents
from ngn_nagents.auth import available
from ngn_nagents.protocol import VERSION


class CompatibilityTest(unittest.TestCase):
    def test_probe_surface_is_unchanged(self) -> None:
        self.assertEqual(VERSION, 1)
        self.assertIs(available(), True)
        self.assertRegex(ngn_nagents.__version__, r"^0\.100\.\d+$")

    def test_running_the_adapter_explains_its_retirement(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "ngn_nagents"], capture_output=True, text=True, check=False, timeout=30
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("nagents engine", result.stderr)


if __name__ == "__main__":
    unittest.main()
