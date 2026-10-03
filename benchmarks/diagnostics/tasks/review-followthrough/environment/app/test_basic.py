import unittest

from report import summarize_durations


class BasicSummaryTests(unittest.TestCase):
    def test_positive_durations(self) -> None:
        result = summarize_durations([5.0, 1.0, 4.0])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["total"], 10.0)
        self.assertAlmostEqual(result["mean"], 10.0 / 3)

    def test_one_duration(self) -> None:
        self.assertEqual(summarize_durations([2.5]), {"count": 1, "total": 2.5, "mean": 2.5})


if __name__ == "__main__":
    unittest.main()
