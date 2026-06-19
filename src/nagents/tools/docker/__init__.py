"""Docker execution for agent servers.

Provides :func:`docker_run` — runs shell commands inside a Docker container,
returning stdout, stderr, and exit code.
"""

from __future__ import annotations

import os
import subprocess

# Defaults are read from env vars so the server config controls the execution environment.
_DEFAULT_IMAGE = os.getenv("NAGENTS_SERVER_IMAGE", "python:3.12-slim")
_DEFAULT_NETWORK = os.getenv("NAGENTS_SERVER_NETWORK", "bridge")
_DEFAULT_MEMORY = os.getenv("NAGENTS_SERVER_MEMORY", "512m")
_DEFAULT_CPUS = os.getenv("NAGENTS_SERVER_CPUS", "1.0")
_DEFAULT_TIMEOUT = int(os.getenv("NAGENTS_SERVER_TIMEOUT", "120"))


def docker_run(
    command: str,
    *,
    image: str = "",
    network: str = "",
    memory: str = "",
    cpus: str = "",
    timeout: int = 0,
    workdir: str = "/workspace",
) -> str:
    """Run a shell command inside a Docker container.

    Args:
        command: Shell command to execute.
        image: Docker image (default: from NAGENTS_SANDBOX_IMAGE env or python:3.12-slim).
        network: Docker network mode — ``bridge`` allows internet, ``none`` blocks it.
        memory: Memory limit (e.g. ``512m``, ``1g``).
        cpus: CPU limit (e.g. ``1.0``, ``2.0``).
        timeout: Max runtime in seconds before container is killed.
        workdir: Working directory inside the container.

    Returns:
        Formatted string with exit code, stdout, and stderr.
    """
    img = image or _DEFAULT_IMAGE
    net = network or _DEFAULT_NETWORK
    mem = memory or _DEFAULT_MEMORY
    cpu = cpus or _DEFAULT_CPUS
    tout = timeout or _DEFAULT_TIMEOUT

    try:
        subprocess.run(
            ["docker", "version", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "Error: Docker is not available on this system"

    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        net,
        "--memory",
        mem,
        "--cpus",
        cpu,
        "--workdir",
        workdir,
        img,
        "sh",
        "-c",
        command,
    ]

    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=tout + 10,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {tout}s"

    out = result.stdout.strip()
    err = result.stderr.strip()
    parts = [f"exit_code: {result.returncode}"]
    if out:
        parts.append(f"stdout:\n{out}")
    if err:
        parts.append(f"stderr:\n{err}")
    return "\n".join(parts) or "(no output)"
