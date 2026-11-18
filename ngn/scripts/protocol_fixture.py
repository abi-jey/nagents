"""Test-only stdio runner for artifact topology and abrupt host loss; no model access."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

CRASH_PROMPT = "hold until host loss"
CRASH_NOTICE = "Crash fixture is waiting for host EOF"
TOPOLOGY_PROMPT = "supervise two background readers"


def write_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "runner": [sys.executable, "-I", str(Path(__file__).resolve())],
                "provider": "protocol-fixture",
                "providers": {
                    "protocol-fixture": {
                        "kind": "openai_compatible",
                        "model": "protocol-fixture",
                        "base_url": "http://127.0.0.1:1/v1",
                        "api_key_env": "NGN_UNUSED_PROTOCOL_FIXTURE_KEY",
                    }
                },
            }
        )
    )


def serve() -> None:
    def emit(run: str, message: dict[str, object]) -> None:
        print(json.dumps({"protocol_version": 1, "run_id": run, "message": message}), flush=True)

    emit("", {"type": "ready", "runtime": "offline-protocol-fixture", "capabilities": ["host_tools", "cancel"]})
    start = json.loads(sys.stdin.readline())
    assert start["message"]["type"] == "start"
    run = start["run_id"]
    prompt = start["message"]["run"]["prompt"]

    def tool(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        emit(run, {"type": "tool_request", "call": {"call_id": call_id, "name": name, "arguments": arguments}})
        frame = json.loads(sys.stdin.readline())
        assert frame["run_id"] == run and frame["message"]["type"] == "tool_result"
        result = frame["message"]["result"]
        assert result["call_id"] == call_id and not result.get("error"), result
        return cast("dict[str, object]", result["result"])

    if prompt == CRASH_PROMPT:
        emit(run, {"type": "notice", "text": CRASH_NOTICE})
        # Dropping the host's pipe on SIGKILL also exits this runner.
        line = sys.stdin.readline()
        assert not line or json.loads(line)["message"]["type"] == "cancel"
        return
    if prompt == TOPOLOGY_PROMPT:
        children = [
            cast(
                "dict[str, str]",
                tool(
                    f"delegate-{index}",
                    "to_agent",
                    {
                        "prompt": "read a fixture file",
                        "title": f"Artifact reader {index}",
                        "background": True,
                        "read_only": True,
                    },
                )["task"],
            )["run_id"]
            for index in range(1, 3)
        ]
        assert len(set(children)) == 2
        for index, child in enumerate(children, 1):
            result = tool(f"join-{index}", "agent_wait", {"run_id": child, "timeout_seconds": 20})
            task = cast("dict[str, object]", result["task"])
            assert task["status"] == "completed" and result["result"] == "Artifact fixture file was read"
    else:
        assert prompt == "read a fixture file", prompt
        result = tool("read", "read_file", {"path": "fixture.txt"})
        assert result["content"] == "1: local artifact fixture"
        emit(run, {"type": "text_completed", "text": "Artifact fixture file was read"})
    emit(run, {"type": "finished", "status": "completed", "error": ""})


if __name__ == "__main__":
    serve()
