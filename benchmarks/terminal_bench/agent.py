"""Harbor 0.23.0 installed-agent adapter. Not a replacement agent loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import stat
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.agents.options import InstalledAgentOptions

if TYPE_CHECKING:
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext

REMOTE = "/installed-agent/ngn-benchmark"
CREDENTIALS = "/run/ngn-benchmark-creds.json"


def installation_command(wheel: str) -> str:
    """Install prerequisites explicitly; keep diagnostics outside task artifacts."""
    return f"""(
set -eu
trap 'python3 --version > {REMOTE}/python-version.txt 2>&1 || true
if command -v dpkg-query >/dev/null 2>&1; then
  dpkg-query -W "python3*" "*pip*" "*venv*" > {REMOTE}/system-python-packages.txt 2>&1 || true
fi
if [ -x {REMOTE}/venv/bin/python ]; then
  {REMOTE}/venv/bin/python -m pip freeze > {REMOTE}/installed-dependencies.txt 2>&1 || true
fi' EXIT
python3 -c 'import sys; assert sys.version_info >= (3, 11), "ngn requires Python 3.11 or newer"'
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  echo 'ngn benchmark prerequisite: Python ensurepip is missing; installing the matching venv package.'
  if [ ! -f /etc/debian_version ] || ! command -v apt-get >/dev/null 2>&1; then
    echo 'Unsupported benchmark image: missing ensurepip; automatic prerequisite installation supports Debian/Ubuntu with apt-get only.'
    exit 2
  fi
  ngn_benchmark_venv_package="$(python3 -c 'import sys; print("python" + str(sys.version_info.major) + "." + str(sys.version_info.minor) + "-venv")')"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$ngn_benchmark_venv_package"
  python3 -c 'import ensurepip'
fi
python3 -m venv {REMOTE}/venv
{REMOTE}/venv/bin/python -m pip install -c {REMOTE}/runtime-constraints.txt {shlex.quote(wheel)}
{REMOTE}/venv/bin/python -m pip freeze > {REMOTE}/installed-dependencies.txt
printf 'harbor-docker\\n' > {REMOTE}/isolated
) > {REMOTE}/install.log 2>&1
ngn_benchmark_setup_status=$?
if [ "$ngn_benchmark_setup_status" -ne 0 ]; then tail -n 80 {REMOTE}/install.log; fi
exit "$ngn_benchmark_setup_status"
"""


class NgnOptions(InstalledAgentOptions):  # type: ignore[misc]
    bundle: str
    credentials_path: str
    timeout_seconds: int = 900


def validate_container(info: dict[str, object], trial: Path) -> dict[str, object]:
    """Reject host access before copying credentials or enabling approvals."""
    host = info.get("HostConfig")
    mounts = info.get("Mounts")
    if not isinstance(host, dict) or not isinstance(mounts, list):
        raise ValueError("Docker inspection returned an incomplete isolation record")
    if any(host.get(key) for key in ("Privileged", "CapAdd", "Devices", "DeviceRequests")):
        raise ValueError("Benchmark containers may not be privileged or receive extra capabilities/devices")
    if any(host.get(key) == "host" for key in ("NetworkMode", "PidMode", "IpcMode", "UTSMode", "UsernsMode")):
        raise ValueError("Benchmark containers may not share host namespaces")
    cpu = host.get("NanoCpus")
    memory = host.get("Memory")
    pids = host.get("PidsLimit")
    if not isinstance(cpu, int) or not 0 < cpu <= 2_000_000_000:
        raise ValueError("Pilot requires a real Docker CPU limit of at most 2 CPUs")
    if not isinstance(memory, int) or not 0 < memory <= 8 * 1024**3:
        raise ValueError("Pilot requires a real Docker memory limit of at most 8 GiB")
    if not isinstance(pids, int) or not 0 < pids <= 512:
        raise ValueError("Pilot requires a real Docker process limit of at most 512")
    allowed = {
        "/logs/agent": trial / "agent",
        "/logs/verifier": trial / "verifier",
        "/logs/artifacts": trial / "artifacts/logs/artifacts",
    }
    for mount in mounts:
        if not isinstance(mount, dict):
            raise ValueError("Malformed Docker mount")
        destination = mount.get("Destination")
        source = mount.get("Source")
        if (
            mount.get("Type") != "bind"
            or destination not in allowed
            or not isinstance(source, str)
            or Path(source).resolve() != allowed[destination].resolve()
        ):
            raise ValueError("Only this trial's exact Harbor log/artifact mounts are allowed")
    return {"image_id": info.get("Image"), "cpu_nano": cpu, "memory_bytes": memory, "pids": pids, "mounts": mounts}


async def docker_inspection(environment: BaseEnvironment) -> dict[str, object]:
    if environment.type() != "docker":
        raise ValueError("This benchmark adapter only permits local Docker isolation")
    project = environment.session_id.lower()
    if not re.match(r"^[a-z0-9]", project):
        project = "0" + project
    project = re.sub(r"[^a-z0-9_-]", "-", project)

    async def docker(*arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker", *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError("Docker isolation inspection failed")
        return stdout.decode()

    ids = (await docker("ps", "-q", "--filter", f"label=com.docker.compose.project={project}")).split()
    if len(ids) != 1:
        raise ValueError("This pilot supports exactly one task container, without sidecars")
    records = json.loads(await docker("inspect", ids[0]))
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError("Malformed Docker inspection")
    return validate_container(records[0], environment.trial_paths.trial_dir)


class NgnAgent(BaseInstalledAgent):  # type: ignore[misc]
    options_model = NgnOptions

    @staticmethod
    def name() -> str:
        return "ngn-shipping-harness"

    def options_value(self) -> NgnOptions:
        if not isinstance(self.options, NgnOptions):
            raise ValueError("ngn benchmark options are required")
        return self.options

    async def install(self, environment: BaseEnvironment) -> None:
        options = self.options_value()
        if not 1 <= options.timeout_seconds <= 1800:
            raise ValueError("Pilot timeout must be between 1 and 1800 seconds")
        bundle = Path(options.bundle).resolve()
        manifest = json.loads((bundle / "provenance.json").read_text())
        if version("harbor") != manifest["harbor"]["version"]:
            raise ValueError("Installed Harbor version differs from benchmark provenance")
        if Path(__file__).resolve() != bundle / "benchmarks/terminal_bench/agent.py":
            raise ValueError("Load the frozen adapter with PYTHONPATH pointing to the prepared bundle")
        for relative, expected in manifest["adapter_files_sha256"].items():
            if hashlib.sha256((bundle / relative).read_bytes()).hexdigest() != expected:
                raise ValueError("Frozen adapter file digest does not match provenance")
        for name in ("isolation", "job"):
            if hashlib.sha256((bundle / f"{name}.json").read_bytes()).hexdigest() != manifest[f"{name}_sha256"]:
                raise ValueError(f"Frozen {name} config digest does not match provenance")
        wheel = bundle / manifest["wheel"]["filename"]
        if wheel.parent != bundle or not wheel.name.endswith(".whl"):
            raise ValueError("Invalid benchmark wheel path")
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != manifest["wheel"]["sha256"]:
            raise ValueError("Benchmark wheel digest does not match provenance")
        runner = bundle / "runner.py"
        if hashlib.sha256(runner.read_bytes()).hexdigest() != manifest["runner_sha256"]:
            raise ValueError("Benchmark runner digest does not match provenance")
        constraints = bundle / "runtime-constraints.txt"
        if hashlib.sha256(constraints.read_bytes()).hexdigest() != manifest["runtime_constraints_sha256"]:
            raise ValueError("Runtime constraints digest does not match provenance")
        isolation = await docker_inspection(environment)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "isolation.json").write_text(json.dumps(isolation, indent=2) + "\n")
        (self.logs_dir / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n")
        await self.exec_as_root(environment, f"mkdir -p {REMOTE}")
        await environment.upload_file(wheel, f"{REMOTE}/{wheel.name}")
        await environment.upload_file(runner, f"{REMOTE}/runner.py")
        await environment.upload_file(constraints, f"{REMOTE}/runtime-constraints.txt")
        await self.install_python(environment, f"{REMOTE}/{wheel.name}")
        if environment.default_user is not None:
            await self.exec_as_root(environment, f"chown -R {shlex.quote(str(environment.default_user))} {REMOTE}")

    async def install_python(self, environment: BaseEnvironment, wheel: str) -> None:
        try:
            await self.exec_as_root(environment, installation_command(wheel))
        finally:
            for filename in (
                "install.log",
                "python-version.txt",
                "installed-dependencies.txt",
                "system-python-packages.txt",
            ):
                try:
                    await environment.download_file(f"{REMOTE}/{filename}", self.logs_dir / filename)
                except Exception:
                    self.logger.warning("Setup diagnostic unavailable: %s", filename)

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        options = self.options_value()
        if not self.model_name or "/" in self.model_name:
            raise ValueError("Supply an explicit native OpenAI model ID, for example gpt-6-astra")
        await docker_inspection(environment)
        source = Path(options.credentials_path)
        info = source.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_size > 32768
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            raise ValueError("Credentials must be an explicit private regular file of at most 32 KiB")
        # Validate only the narrow schema. Never log, hash, serialize, or refresh credentials.
        credentials = json.loads(source.read_text())
        if (
            not isinstance(credentials, dict)
            or set(credentials) != {"access_token", "account_id", "residency"}
            or any(not isinstance(value, str) for value in credentials.values())
            or not credentials["access_token"]
        ):
            raise ValueError("Credentials must contain only access_token, account_id, residency")
        try:
            # Copy the validated payload, not a host path which may be replaced
            # between validation and Harbor's file transfer.
            with tempfile.TemporaryDirectory(prefix="ngn-benchmark-secret-") as directory:
                staged = Path(directory) / "credentials.json"
                with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                    json.dump(credentials, stream)
                await environment.upload_file(staged, CREDENTIALS)
            owner = str(environment.default_user) if environment.default_user is not None else "root"
            await self.exec_as_root(environment, f"chown {shlex.quote(owner)} {CREDENTIALS} && chmod 600 {CREDENTIALS}")
            with tempfile.TemporaryDirectory(prefix="ngn-benchmark-instruction-") as directory:
                prompt = Path(directory) / "instruction.txt"
                prompt.write_text(instruction)
                await environment.upload_file(prompt, f"{REMOTE}/instruction.txt")
            output = str(self.environment_logs_dir)
            await self.exec_as_agent(
                environment,
                f"{REMOTE}/venv/bin/python {REMOTE}/runner.py "
                f"--instruction {REMOTE}/instruction.txt --credentials {CREDENTIALS} "
                f"--model {shlex.quote(self.model_name)} --output {shlex.quote(output)} "
                f"--timeout {options.timeout_seconds} > {shlex.quote(output)}/process.log 2>&1",
                env={"NGN_BENCHMARK_ISOLATED": "1"},
            )
        finally:
            # Also covers setup inside run(), process failure, and Harbor cancellation.
            await asyncio.shield(environment.exec(f"rm -f {CREDENTIALS}", user="root", timeout_sec=10))

    def populate_context_post_run(self, context: AgentContext) -> None:
        from benchmarks.terminal_bench.summarize import summarize_events

        summary = summarize_events(self.logs_dir / "events.jsonl")
        (self.logs_dir / "harness-metrics.json").write_text(json.dumps(summary, indent=2) + "\n")
        context.metadata = {"ngn_harness": summary}
