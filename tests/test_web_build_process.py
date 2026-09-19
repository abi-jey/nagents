"""Build cancellation must not orphan npm or overlap a subsequent reload."""

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from nagents.web.build_process import run_build_command
from tests.hang_guard import HANG_GUARD


def test_failed_build_reports_exit_status(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_build_command([sys.executable, "-c", "raise SystemExit(7)"], cwd=tmp_path)
    assert error.value.returncode == 7


@pytest.mark.requires_posix
@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_interrupt_cleans_up_build_and_grandchild(tmp_path: Path, stop_signal: int) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "import time\nfrom pathlib import Path\n"
        "while True:\n    Path('heartbeat').write_text(str(time.monotonic_ns()))\n    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    build = tmp_path / "build.py"
    build.write_text(
        "import os, subprocess, sys, time\nfrom pathlib import Path\n"
        "Path('build-pid').write_text(str(os.getpid()))\n"
        "subprocess.Popen([sys.executable, 'child.py'])\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    driver = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from nagents.web.build_process import run_build_command; "
            "run_build_command([sys.executable, sys.argv[1]], cwd=Path(sys.argv[2]))",
            str(build),
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + HANG_GUARD
        heartbeat = tmp_path / "heartbeat"
        while not heartbeat.exists():
            assert driver.poll() is None, "Build driver exited before spawning children"
            assert time.monotonic() < deadline, "Build subprocess did not start"
            time.sleep(0.02)
        driver.send_signal(stop_signal)
        assert driver.wait(timeout=HANG_GUARD) != 0
        # If the grandchild survived, its monotonic heartbeat would keep advancing.
        last_write = heartbeat.stat().st_mtime_ns
        time.sleep(0.15)
        assert heartbeat.stat().st_mtime_ns == last_write, "Build grandchild was orphaned"
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait(timeout=HANG_GUARD)
        pid_file = tmp_path / "build-pid"
        if pid_file.exists():
            with suppress(ProcessLookupError):
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
