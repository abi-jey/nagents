This is an ngn-owned workflow diagnostic, independent of Terminal-Bench.

Repair the tiny Python project in `/app` using this staged workflow:

1. Inspect `report.py` and `test_basic.py`. Use the native file tools to make the
   smallest first-phase correction so the count, total, and mean are correct for
   nonempty lists of nonnegative finite numbers. Run the existing basic tests.
   Keep the public function name and the existing tests.
2. After that first phase, call your native `delegate` tool with
   `agent='assistant'`. Ask for an independent, inspect-only review of the current
   `report.py` against `/app/REVIEW.md`. Tell the child to report concrete findings,
   not edit files or run shell commands. The child must use the existing project,
   not an invented replacement implementation. Do not implement the additional
   review acceptance cases before this review arrives: their follow-through is
   what this diagnostic measures.
3. If the review is still running, provide a brief provisional progress update
   without claiming completion. Do not poll; native task notifications resume
   your work when the review is available. Then address every valid actionable
   finding, add regression tests in a new test file, and run the full unittest
   suite again.
4. Finish with a short report of what changed and the commands/results actually
   observed. Do not stop at acknowledging an actionable review finding.

Use only the standard library. Task work needs no network: do not browse, download
code, or make external requests. Do not modify `REVIEW.md` or `test_basic.py`, or
look for verifier files outside `/app`. Add new tests in a separate file. Model transport
and pre-run package installation are supplied by the harness, not by this task.

The expected code/test work is small and should take less than five minutes.
The actual configured agent deadline is 900 seconds; shipping tool limits apply.
