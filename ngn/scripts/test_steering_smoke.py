"""Installed steering acceptance must not silently fall back to editable imports."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from steering_smoke import REPO, adapter_runner


class SteeringSmokeTests(unittest.TestCase):
    def test_installed_mode_clears_source_environment_and_keeps_venv_interpreter_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime-python"
            runtime.symlink_to("/usr/bin/python3")
            with (
                patch.dict(os.environ, {"PYTHONPATH": "/untrusted/source", "PYTHONHOME": "/old"}),
                patch("steering_smoke.subprocess.run") as probe,
            ):
                self.assertEqual(
                    adapter_runner(runtime, installed=True), [str(runtime), "-I", "-B", "-m", "ngn_nagents"]
                )
                self.assertNotIn("PYTHONPATH", os.environ)
                self.assertNotIn("PYTHONHOME", os.environ)
                probe.assert_called_once()
                self.assertEqual(probe.call_args.args[0][:4], [str(runtime), "-I", "-B", "-c"])
                self.assertTrue(probe.call_args.kwargs["check"])

    def test_installed_import_failure_is_fatal_without_source_fallback(self) -> None:
        with (
            patch.dict(os.environ, {"PYTHONPATH": "/untrusted/source"}),
            patch("steering_smoke.subprocess.run", side_effect=subprocess.CalledProcessError(1, "probe")),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            adapter_runner(Path("/runtime/bin/python"), installed=True)

    def test_default_source_mode_is_explicit_and_does_not_claim_installed_imports(self) -> None:
        with patch.dict(os.environ), patch("steering_smoke.subprocess.run") as probe:
            self.assertEqual(
                adapter_runner(Path("/runtime/bin/python"), installed=False),
                ["/runtime/bin/python", "-m", "ngn_nagents"],
            )
            self.assertEqual(os.environ["PYTHONPATH"], f"{REPO / 'src'}:{REPO / 'ngn/integrations/nagents/src'}")
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
