"""Capture real terminal startup frames using a disposable tmux server.

Run: poetry run python benchmarks/startup/tmux_capture.py
Offline demo only; creates no provider requests. Captures are saved outside the
checkout by default. Does not connect to or modify the user's tmux server.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=36)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--seconds", type=float, default=4.0)
    args = parser.parse_args()
    if not 1 <= args.fps <= 60 or not 0.1 <= args.seconds <= 30:
        parser.error("fps must be 1..60 and seconds 0.1..30")
    if not shutil.which("tmux"):
        parser.error("tmux is required")
    if args.width < 40 or args.height < 15:
        parser.error("use a terminal at least 40x15")
    root = Path(__file__).resolve().parents[2]
    output = args.output or Path(tempfile.mkdtemp(prefix="ngn-startup-capture-"))
    output.mkdir(parents=True, exist_ok=True)
    workspace = output / "workspace"
    workspace.mkdir(exist_ok=True)
    socket = "ngn-capture-" + uuid4().hex[:12]
    command = ["tmux", "-L", socket, "-f", "/dev/null"]

    def tmux(*parts: str) -> str:
        return subprocess.run([*command, *parts], check=True, text=True, capture_output=True).stdout

    try:
        tmux(
            "new-session",
            "-d",
            "-s",
            "capture",
            "-x",
            str(args.width),
            "-y",
            str(args.height),
            "-c",
            str(root),
            "/bin/sh",
        )
        # Isolate settings and demo state without inspecting personal credentials.
        env = {
            "XDG_CONFIG_HOME": str(output / "config"),
            "XDG_DATA_HOME": str(output / "data"),
            "TERM": "xterm-256color",
        }
        launch = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        launch += " " + shlex.join([sys.executable, "-m", "nagents.cli", "--demo", "-C", str(workspace)])
        tmux("send-keys", "-t", "capture", "printf 'SCROLLBACK-MARKER before ngn\\n'; " + launch, "Enter")
        started = time.monotonic()
        frames: list[dict[str, object]] = []
        for index in range(int(args.seconds * args.fps) + 1):
            target = index / args.fps
            time.sleep(max(0, started + target - time.monotonic()))
            actual = time.monotonic() - started
            name = f"{index:02d}-{target:.2f}s"
            (output / f"{name}.txt").write_text(tmux("capture-pane", "-p", "-t", "capture"), encoding="utf-8")
            (output / f"{name}.ansi").write_text(tmux("capture-pane", "-p", "-e", "-t", "capture"), encoding="utf-8")
            frames.append({"file": name, "elapsed_seconds": round(actual, 4)})
        replay = output / "replay.py"
        replay.write_text(
            "import json, pathlib, sys, time\n"
            "root = pathlib.Path(__file__).parent\n"
            'frames = json.loads((root / "manifest.json").read_text())["frames"]\n'
            "start = time.monotonic()\n"
            "try:\n"
            '    sys.stdout.write("\\x1b[?1049h\\x1b[?25l")\n'
            "    for frame in frames:\n"
            '        time.sleep(max(0, start + frame["elapsed_seconds"] - time.monotonic()))\n'
            '        sys.stdout.write("\\x1b[H\\x1b[2J" + (root / (frame["file"] + ".ansi")).read_text())\n'
            "        sys.stdout.flush()\n"
            "finally:\n"
            '    sys.stdout.write("\\x1b[0m\\x1b[?25h\\x1b[?1049l")\n'
            "    sys.stdout.flush()\n",
            encoding="utf-8",
        )
        tmux("send-keys", "-t", "capture", "C-q")
        time.sleep(1)
        (output / "after-exit.txt").write_text(tmux("capture-pane", "-p", "-S", "-", "-t", "capture"), encoding="utf-8")
        (output / "manifest.json").write_text(
            json.dumps({"width": args.width, "height": args.height, "frames": frames}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(output)
    finally:
        subprocess.run([*command, "kill-server"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
