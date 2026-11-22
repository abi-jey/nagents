"""The `nagents engine` entry point: the server behind every ngn client."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from nagents._version import framework_version
from nagents.harness import load_config
from nagents.harness.providers.model_bindings import ModelBinding
from nagents.harness.providers.registry import ScopedProviderRegistryStore

from .registration import RegistrationStore
from .registration import workspace_state_dir

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nagents.harness.config import HarnessConfig

_ENGINE_DEPENDENCIES = {"aiortc", "anyio", "av", "fastapi", "pydantic", "starlette", "uvicorn", "websockets"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nagents", description="The local Python engine behind ngn.")
    parser.add_argument("--version", action="version", version=f"nagents {framework_version()}")
    commands = parser.add_subparsers(dest="command", required=True)
    engine = commands.add_parser("engine", help="Start the authenticated loopback API")
    importer = commands.add_parser("import-host", help="Import read-only history from an offline Rust host backup")
    for command in (engine, importer):
        command.add_argument(
            "--workspace", "-C", type=Path, default=Path.cwd(), help="Workspace (default: current directory)"
        )
        command.add_argument(
            "--state-dir",
            type=Path,
            help="Exact private state directory (default: workspace-scoped under ngn data_dir)",
        )
        command.add_argument("--config", type=Path, help="Explicit trusted Harness JSON configuration")
        command.add_argument(
            "--trust-project", action="store_true", help="Trust this project's .ngn/config.json and its code"
        )
    importer.add_argument("--host-snapshot", required=True, type=Path, help="A private SQLite backup of host.sqlite3")
    engine.add_argument("--demo", action="store_true", help="Offline demo; no model calls or workspace writes")
    engine.add_argument("--read-only", action="store_true", help="Disable workspace mutations")
    engine.add_argument("--provider-id", metavar="ID", help="Select a named provider connection")
    engine.add_argument("--model", metavar="MODEL", help="Set the default provider model")
    wire = engine.add_mutually_exclusive_group()
    wire.add_argument(
        "--wire-log", dest="wire_log", action="store_true", default=None, help="Log sensitive provider wire traffic"
    )
    wire.add_argument("--no-wire-log", dest="wire_log", action="store_false", help="Disable provider wire logging")
    engine.add_argument("--port", type=int, default=0, help="Local port (default: choose an unused port)")
    launch = engine.add_mutually_exclusive_group()
    launch.add_argument("--stdio", action="store_true", help="Private launcher pipes; shut down on stdin EOF")
    launch.add_argument("--print-state-dir", action="store_true", help=argparse.SUPPRESS)
    return parser


def _workspace_config(args: argparse.Namespace) -> HarnessConfig:
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Workspace is not a directory: {workspace}")
    config = load_config(workspace, args.config, trust_project=args.trust_project)
    directory = args.state_dir or workspace_state_dir(config.data_dir, config.workspace)
    dedicated = directory.expanduser().resolve()
    if dedicated == config.data_dir:
        raise ValueError("--state-dir must be dedicated to the engine, not the configured ngn data directory.")
    if "\r" in str(dedicated) or "\n" in str(dedicated):
        raise ValueError("The engine state directory cannot contain a line break.")
    return replace(config, data_dir=dedicated)


def _launch_config(args: argparse.Namespace) -> HarnessConfig:
    if not 0 <= args.port <= 65535:
        raise ValueError("--port must be between 0 and 65535.")
    config = _workspace_config(args)
    if not config.profile(config.agent).user_invocable:
        raise ValueError(f"Agent {config.agent!r} is available only for delegation")

    provider, providers = config.provider, config.providers
    unavailable = config.unavailable_model_binding
    if args.provider_id is not None:
        provider = args.provider_id
        registry = ScopedProviderRegistryStore(config.workspace, paths=config.provider_paths).load()
        profile = registry.providers.get(provider)
        if profile is None:
            raise ValueError(f"Unknown provider connection {provider!r}")
        providers = {**providers, provider: profile}
        unavailable = ModelBinding()
    model = config.model if args.model is None else args.model
    # An explicit connection changes the route, not the already loaded model
    # ID. Pin this startup pair so a saved default cannot undo the flag.
    pinned = args.model is not None or args.provider_id is not None
    launch = replace(
        config,
        demo=args.demo or config.demo,
        read_only=args.read_only or config.read_only,
        wire_log=config.wire_log if args.wire_log is None else args.wire_log,
        provider=provider,
        providers=providers,
        unavailable_model_binding=unavailable,
        model=model,
        model_explicit=pinned or config.model_explicit,
        model_config_explicit=pinned or config.model_config_explicit,
        global_model_default=model if pinned else config.global_model_default,
    )
    if pinned:
        ModelBinding(provider, model)
    return launch


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "import-host":
            from nagents.harness.runtime.core import session_database_path

            from .host_import import import_host

            config = _workspace_config(args)
            report = import_host(
                args.host_snapshot, config.data_dir, session_database_path(config.workspace, config.data_dir)
            )
            action = "Already imported" if report.already_imported else "Imported"
            print(
                f"{action}: {report.sessions} read-only sessions, {report.events} events. "
                f"Child conversations not imported: {report.ignored_children}."
            )
            return 0
        if args.print_state_dir:
            directory = _launch_config(args).data_dir
            RegistrationStore(directory).check_directory()
            # Keep this lossless for native launchers even with a non-UTF-8
            # state path. Consumers remove only the terminating LF.
            sys.stdout.buffer.write(os.fsencode(directory) + b"\n")
            sys.stdout.buffer.flush()
            return 0
        try:
            from .lifecycle import private_stdio
            from .lifecycle import serve_engine
        except ModuleNotFoundError as error:
            if error.name not in _ENGINE_DEPENDENCIES:
                raise
            raise ValueError(
                "Install the optional engine dependencies with `pip install 'nagents[engine]'`."
            ) from error
        launch = _launch_config(args)
        if not launch.demo:
            from nagents.harness.runtime.core import session_database_path

            from .host_import import import_legacy_once

            notice = import_legacy_once(
                launch.workspace, launch.data_dir, session_database_path(launch.workspace, launch.data_dir)
            )
            if notice:
                print(f"nagents: {notice}", file=sys.stderr)
        with private_stdio() if args.stdio else nullcontext() as lease:
            asyncio.run(serve_engine(launch, port=args.port, lease=lease))
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, RuntimeError) as error:
        print(f"nagents: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
