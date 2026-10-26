"""Uvicorn exposes only explicitly audited lifecycle logger namespaces."""

from __future__ import annotations

import re
import subprocess
import sys
from textwrap import dedent


def test_server_config_emits_audited_lifecycle_info_with_utc_time_only() -> None:
    script = dedent(
        """
        import logging
        import logging.config
        from io import StringIO

        from fastapi import FastAPI
        from nagents.web.runtime import server_config

        config = server_config(FastAPI(), host="127.0.0.1", port=0).log_config
        stream = StringIO()
        config["handlers"]["default"]["stream"] = stream
        logging.config.dictConfig(config)
        logging.getLogger("nagents.harness.agents.subagents").info(
            "Subagent execution started: task=task-id profile=assistant elapsed_ms=12.5"
        )
        logging.getLogger("nagents.harness.remote.runtime").info(
            "A2A cancellation requested: session=session-id task=task-id"
        )
        logging.getLogger("nagents.harness.remote.credentials").warning(
            "Remote credential lock failed: phase=acquire_lock error_type=BlockingIOError"
        )
        logging.getLogger("nagents.harness.unrelated").info("UNRELATED_INFO_MARKER")
        print(stream.getvalue(), end="")
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines()

    assert any("Subagent execution started" in line for line in lines)
    assert any("A2A cancellation requested" in line for line in lines)
    assert any("Remote credential lock failed" in line for line in lines)
    assert "UNRELATED_INFO_MARKER" not in result.stdout
    assert all(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ", line) for line in lines)
