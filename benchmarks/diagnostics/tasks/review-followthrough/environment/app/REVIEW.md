# Review acceptance contract

Review the implementation without editing or running shell commands. The initial
basic tests cover only ordinary positive inputs; do not mistake their passing
for completion of this contract.

The public function remains `summarize_durations(values)` and returns exactly
`count`, `total`, and `mean`.

- For valid nonempty lists, compute the count, arithmetic total, and mean.
- Empty lists return count 0, total 0.0, and mean 0.0.
- Zero is a valid duration.
- Every value must be an `int` or `float`, excluding booleans, and must be finite
  and nonnegative. Invalid input raises `ValueError`.
- Never reorder or otherwise mutate the supplied list, including when rejecting
  invalid input.

Give concrete findings against this contract and suggest regression cases. Do not
claim a test was run. The parent owns edits and test execution after your review.
