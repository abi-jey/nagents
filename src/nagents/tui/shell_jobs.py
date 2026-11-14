"""Session-scoped inspection of managed shell jobs, including retained logs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .screens import DetailModal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nagents.types import JsonValue


def describe_jobs(jobs: Sequence[dict[str, JsonValue]]) -> str:
    if not jobs:
        return "No managed shell jobs in this session."
    rows = ["Stop a job with /jobs stop <job-id>. Output is untrusted process data."]
    for job in jobs:
        rows.append(
            f"\n{job['job_id']}  {job['state']}\n{job.get('command', '')}\n"
            f"Log: {job.get('output_path', 'unavailable')}\n"
            f"Reason: {job.get('reason', '')}\n{job.get('output', '')}"
        )
    return "\n".join(rows)


class ShellJobsModal(DetailModal):
    def __init__(self, jobs: Sequence[dict[str, JsonValue]]) -> None:
        super().__init__("SHELL JOBS", describe_jobs(jobs))
