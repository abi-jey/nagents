from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("harbor", reason="Harbor belongs only in the optional benchmark environment")

from benchmarks.diagnostics.prepare import JOB_NAME
from benchmarks.diagnostics.prepare import REPOSITORY
from benchmarks.diagnostics.prepare import TASK_SOURCE
from benchmarks.diagnostics.prepare import prepare_verifier_check
from harbor.environments.definition import require_agent_environment_definition
from harbor.models.task.task import Task


def test_separate_verifier_requires_its_own_environment_definition(tmp_path: Path) -> None:
    task = Task(TASK_SOURCE)
    assert task.config.verifier.environment_mode.value == "separate"
    require_agent_environment_definition(TASK_SOURCE / "tests")
    damaged = tmp_path / "task"
    shutil.copytree(TASK_SOURCE, damaged)
    (damaged / "tests/Dockerfile").unlink()
    # Harbor's generic task discovery alone did not catch the original defect.
    Task(damaged)
    with pytest.raises(FileNotFoundError, match="environment definition"):
        require_agent_environment_definition(damaged / "tests")


@pytest.mark.skipif(
    os.environ.get("NGN_DIAGNOSTIC_DOCKER_TESTS") != "1", reason="Opt in to the no-model Docker setup check"
)
def test_real_harbor_artifact_transfer_and_separate_verifier_without_a_model(tmp_path: Path) -> None:
    bundle, jobs = tmp_path / "bundle", tmp_path / "jobs"
    config = prepare_verifier_check(REPOSITORY, bundle, jobs)
    subprocess.run(
        [str(Path(sys.executable).with_name("harbor")), "run", "--config", str(config)],
        cwd=bundle,
        capture_output=True,
        text=True,
        check=True,
        timeout=240,
    )
    trials = list((jobs / (JOB_NAME + "-verifier-check")).glob("*/result.json"))
    assert len(trials) == 1
    result = json.loads(trials[0].read_text())
    assert result["exception_info"] is None
    assert result["verifier_result"]["rewards"]["reward"] == 0.0
    verifier = json.loads((trials[0].parent / "verifier/diagnostic-verifier.json").read_text())
    assert verifier["tests"] == 9 and verifier["passed"] is False
