"""Docker execution for agent servers.

Provides :func:`docker_run` — runs shell commands inside a Docker container,
returning stdout, stderr, and exit code.
"""

from __future__ import annotations

import subprocess


def docker_run(
    command: str,
    *,
    image: str = "python:3.12-slim",
    network: str = "none",
    memory: str = "256m",
    cpus: str = "1.0",
    timeout: int = 120,
    workdir: str = "/workspace",
) -> str:
    """Run a shell command inside a Docker container.

    Args:
        command: Shell command to execute.
        image: Docker image (default: ``python:3.12-slim``).
        network: Docker network mode — ``none`` blocks all external access.
        memory: Memory limit (e.g. ``256m``, ``1g``).
        cpus: CPU limit (e.g. ``1.0``, ``2.0``).
        timeout: Max runtime in seconds before container is killed.
        workdir: Working directory inside the container.

    Returns:
        Formatted string with exit code, stdout, and stderr.
    """
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
        network,
        "--memory",
        memory,
        "--cpus",
        cpus,
        "--workdir",
        workdir,
        image,
        "sh",
        "-c",
        command,
    ]

    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"

    out = result.stdout.strip()
    err = result.stderr.strip()
    parts = [f"exit_code: {result.returncode}"]
    if out:
        parts.append(f"stdout:\n{out}")
    if err:
        parts.append(f"stderr:\n{err}")
    return "\n".join(parts) or "(no output)"
