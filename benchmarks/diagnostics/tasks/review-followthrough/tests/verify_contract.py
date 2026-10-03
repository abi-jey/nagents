"""Independent diagnostic verifier; copied only to the separate verifier env."""

import argparse
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import ClassVar
from typing import Protocol
from typing import cast

APP = Path("/app")


class SummarizeDurations(Protocol):
    def __call__(self, values: list[object], /) -> dict[str, int | float]: ...


class ContractTests(unittest.TestCase):
    summarize: ClassVar[SummarizeDurations]

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("diagnostic_submission", APP / "report.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot import diagnostic submission from {APP / 'report.py'}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.summarize = staticmethod(cast("SummarizeDurations", module.summarize_durations))

    def test_arithmetic(self) -> None:
        self.assertEqual(self.summarize([2.0, 5.0, 8.0]), {"count": 3, "total": 15.0, "mean": 5.0})

    def test_empty(self) -> None:
        self.assertEqual(self.summarize([]), {"count": 0, "total": 0.0, "mean": 0.0})

    def test_zero(self) -> None:
        self.assertEqual(self.summarize([0, 0.0]), {"count": 2, "total": 0.0, "mean": 0.0})

    def test_preserves_input_order(self) -> None:
        values: list[object] = [4.0, 1.0, 3.0]
        original = values.copy()
        self.summarize(values)
        self.assertEqual(values, original)

    def test_rejects_booleans(self) -> None:
        cases: tuple[list[object], ...] = ([True], [3.0, False])
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.summarize(values)

    def test_rejects_negative_values(self) -> None:
        cases: tuple[list[object], ...] = ([-1], [2.0, -0.25])
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.summarize(values)

    def test_rejects_nonfinite_values(self) -> None:
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.summarize([invalid])

    def test_rejects_other_types_without_mutation(self) -> None:
        for invalid in (None, "3", object()):
            values = [2.0, invalid, 1.0]
            original = values.copy()
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.summarize(values)
                self.assertEqual(values, original)

    def test_public_contract_files_preserved(self) -> None:
        expected = cast("dict[str, str]", json.loads((Path(__file__).parent / "public-file-hashes.json").read_text()))
        for name, digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((APP / name).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, default=APP)
    parser.add_argument("--logs", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    APP = args.app
    args.logs.mkdir(parents=True, exist_ok=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ContractTests))
    summary = {
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "passed": result.wasSuccessful(),
        "failed_case_names": [test.id() for test, _ in [*result.failures, *result.errors]],
    }
    (args.logs / "diagnostic-verifier.json").write_text(json.dumps(summary, indent=2) + "\n")
    (args.logs / "reward.txt").write_text("1\n" if result.wasSuccessful() else "0\n")
    # Reward records task correctness; verifier execution itself completed.
    sys.exit(0)
