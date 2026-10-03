from __future__ import annotations

import asyncio
import copy
import json
from typing import TYPE_CHECKING
from typing import cast

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from harbor.environments.base import BaseEnvironment

pytest.importorskip("harbor", reason="Harbor is a development-only benchmark dependency")

from benchmarks.terminal_bench import agent as adapter
from benchmarks.terminal_bench.agent import NgnAgent
from benchmarks.terminal_bench.agent import validate_container
from benchmarks.terminal_bench.prepare import DATASET_COMMIT
from benchmarks.terminal_bench.prepare import job_config
from benchmarks.terminal_bench.prepare import selected_tasks
from harbor.models.agent.context import AgentContext
from harbor.models.job.config import JobConfig


def isolated(trial: Path) -> dict[str, object]:
    return {
        "Image": "sha256:known-task-image",
        "HostConfig": {"NanoCpus": 2_000_000_000, "Memory": 4 * 1024**3, "PidsLimit": 512},
        "Mounts": [{"Type": "bind", "Source": str(trial / "agent"), "Destination": "/logs/agent"}],
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("Privileged", True),
        ("NetworkMode", "host"),
        ("PidMode", "host"),
        ("CapAdd", ["SYS_ADMIN"]),
        ("NanoCpus", 0),
        ("Memory", 0),
        ("PidsLimit", 0),
    ],
)
def test_host_isolation_and_resource_failures_are_rejected(tmp_path: Path, key: str, value: object) -> None:
    info = isolated(tmp_path)
    host = info["HostConfig"]
    assert isinstance(host, dict)
    host[key] = value
    with pytest.raises(ValueError):
        validate_container(info, tmp_path)


def test_only_exact_trial_log_mounts_allowed(tmp_path: Path) -> None:
    info = isolated(tmp_path)
    assert validate_container(info, tmp_path)["image_id"] == "sha256:known-task-image"
    for source, destination in [
        ("/home/user", "/logs/agent"),
        ("/var/run/docker.sock", "/socket"),
        (str(tmp_path), "/logs/agent"),
    ]:
        changed = copy.deepcopy(info)
        changed["Mounts"] = [{"Type": "bind", "Source": source, "Destination": destination}]
        with pytest.raises(ValueError, match="exact Harbor"):
            validate_container(changed, tmp_path)


def test_job_has_explicit_dataset_sha_serial_trials_no_retries(tmp_path: Path) -> None:
    config = job_config(tmp_path / "bundle", tmp_path / "credentials", tmp_path / "jobs", 900, "gpt-6-astra")
    assert config["n_concurrent_trials"] == config["n_attempts"] == 1
    assert config["retry"] == {"max_retries": 0}
    tasks = config["tasks"]
    assert isinstance(tasks, list)
    assert len(tasks) == 3
    assert all(task["git_commit_id"] == DATASET_COMMIT for task in tasks)
    resolved = JobConfig.model_validate(config)
    assert resolved.n_concurrent_trials == resolved.n_attempts == 1
    assert resolved.retry.max_retries == 0
    assert resolved.environment.delete is True


def test_selected_task_subset_stays_pinned_and_rejects_unsupported_names(tmp_path: Path) -> None:
    config = job_config(
        tmp_path, tmp_path / "credentials", tmp_path / "jobs", 900, "gpt-6-astra", ("data-anonymization",)
    )
    tasks = config["tasks"]
    assert isinstance(tasks, list)
    assert len(tasks) == 1
    assert tasks[0]["path"] == "tasks/data-anonymization"
    assert tasks[0]["git_commit_id"] == DATASET_COMMIT
    for names in [(), ("unknown-task",), ("../solution",), ("data-anonymization", "data-anonymization")]:
        with pytest.raises(ValueError):
            selected_tasks(names)


def test_failed_setup_collects_all_available_diagnostics_without_masking_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloaded: list[str] = []

    class Environment:
        async def download_file(self, source: str, target: Path) -> None:
            downloaded.append(source)
            if source.endswith("installed-dependencies.txt"):
                raise FileNotFoundError("pip was unavailable")

    async def fail(environment: BaseEnvironment, command: str) -> None:
        raise RuntimeError("original prerequisite installation failed")

    agent = NgnAgent(
        logs_dir=tmp_path, model_name="gpt-6-astra", bundle=str(tmp_path), credentials_path=str(tmp_path / "unused")
    )
    monkeypatch.setattr(agent, "exec_as_root", fail)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="original prerequisite installation failed"):
            await agent.install_python(
                cast("BaseEnvironment", Environment()), "/installed-agent/ngn-benchmark/wheel.whl"
            )

    asyncio.run(scenario())
    assert set(downloaded) == {
        f"{adapter.REMOTE}/{name}"
        for name in ["install.log", "python-version.txt", "installed-dependencies.txt", "system-python-packages.txt"]
    }


@pytest.mark.parametrize("cancelled", [False, True])
def test_temporary_container_credentials_cleaned_on_upload_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    source = tmp_path / "credentials.json"
    source.write_text(json.dumps({"access_token": "test-token", "account_id": "account", "residency": ""}))
    source.chmod(0o600)
    cleanups: list[str] = []

    class Environment:
        default_user = None

        async def upload_file(self, source: Path, target: str) -> None:
            assert target == adapter.CREDENTIALS
            assert source.stat().st_mode & 0o077 == 0
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError("transfer failed after creating remote file")

        async def exec(self, command: str, *, user: str, timeout_sec: int) -> None:
            cleanups.append(command)

    async def inspect(environment: BaseEnvironment) -> dict[str, object]:
        return {}

    monkeypatch.setattr(adapter, "docker_inspection", inspect)
    agent = NgnAgent(
        logs_dir=tmp_path / "logs", model_name="gpt-6-astra", bundle=str(tmp_path), credentials_path=str(source)
    )

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
            await agent.run("unused task instruction", cast("BaseEnvironment", Environment()), AgentContext())

    asyncio.run(scenario())
    assert cleanups == [f"rm -f {adapter.CREDENTIALS}"]
    assert source.is_file()  # Host cleanup belongs to the credential provisioner.
