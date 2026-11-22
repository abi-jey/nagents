"""The dev launcher must run installed artifacts while preserving operator intent."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev import (
    Invocation,
    cargo_target,
    command,
    ensure_private_cache,
    invocation,
    isolated_environment,
    main,
    offline_cache_probe,
    source_snapshot,
)


class DevTests(unittest.TestCase):
    def test_fresh_default_cache_creates_every_missing_ancestor_private_under_both_shell_umasks(self) -> None:
        code = """import json, stat, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from dev import ensure_private_cache
base = Path(sys.argv[1])
cache = base / 'xdg/ngn-rust-dev/checkout'
ensure_private_cache(cache)
print(json.dumps([stat.S_IMODE(path.stat().st_mode) for path in (cache.parent.parent, cache.parent, cache)]))
"""
        for mask in ("0002", "0022"):
            with self.subTest(mask=mask), tempfile.TemporaryDirectory() as temporary:
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'umask "$1"; shift; exec "$@"',
                        "ngn-cache-test",
                        mask,
                        sys.executable,
                        "-c",
                        code,
                        temporary,
                        str(Path(__file__).resolve().parent),
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertEqual(json.loads(result.stdout), [0o700] * 3)

    def test_existing_unsafe_cache_ancestor_and_its_symlink_are_never_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "existing"
            existing.mkdir(mode=0o700)
            existing.chmod(0o775)
            alias = root / "alias"
            alias.symlink_to(existing, target_is_directory=True)
            for parent in (existing, alias):
                with (
                    self.subTest(parent=parent),
                    self.assertRaisesRegex(
                        ValueError, "Existing paths were not changed; choose a private NGN_DEV_CACHE"
                    ),
                ):
                    ensure_private_cache(parent / "checkout")
            self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o775)
            self.assertTrue(alias.is_symlink())
            self.assertFalse((existing / "checkout").exists())

    def test_installed_engine_probe_is_offline_demo_and_stage_scoped(self) -> None:
        finished = json.dumps({"event": {"type": "run.finished", "status": "completed", "error": ""}})
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            runtime = stage / "runtime"
            completed = subprocess.CompletedProcess([], 0, f"{{}}\n{finished}\n", "")
            with patch("dev.subprocess.run", return_value=completed) as execute:
                offline_cache_probe(stage, runtime)
            workspace = stage / "offline-probe-workspace"
            self.assertEqual(
                execute.call_args.args[0],
                [
                    str(stage / "app/ngn"),
                    "--standalone",
                    "--engine-demo",
                    "-C",
                    str(workspace),
                    "--data-dir",
                    str(stage / "offline-probe-state"),
                    "run",
                    "--json",
                    "offline probe",
                ],
            )
            self.assertEqual(
                execute.call_args.kwargs["env"],
                {
                    "PATH": str(stage / "app"),
                    "VIRTUAL_ENV": str(runtime),
                    "HOME": str(workspace),
                    "XDG_DATA_HOME": str(workspace / "data"),
                    "XDG_CONFIG_HOME": str(workspace / "config"),
                },
            )
            self.assertTrue(workspace.is_dir())

    def test_installed_engine_probe_fails_closed_without_a_completed_run(self) -> None:
        failed = json.dumps({"event": {"type": "run.finished", "status": "failed", "error": "x"}})
        for code, stdout in ((1, ""), (0, ""), (0, failed + "\n")):
            with (
                self.subTest(code=code, stdout=stdout),
                tempfile.TemporaryDirectory() as temporary,
                patch("dev.subprocess.run", return_value=subprocess.CompletedProcess([], code, stdout, "")),
                self.assertRaisesRegex(ValueError, "Offline installed engine probe failed"),
            ):
                offline_cache_probe(Path(temporary), Path(temporary) / "runtime")

    def test_cli_paths_are_isolated_without_changing_other_rust_arguments(self) -> None:
        parsed = invocation(
            ["--config", "model.json", "-C", "/workspace", "serve", "--port", "18768", "--data-dir=/state"]
        )
        self.assertEqual(parsed, Invocation("model.json", "/state", ["-C", "/workspace", "serve", "--port", "18768"]))
        self.assertEqual(invocation(["run", "--", "--config", "literal text"]).config, "")
        self.assertEqual(invocation(["--config=model.json"]).config, "model.json")

    def test_ambiguous_config_and_state_options_fail_closed(self) -> None:
        for arguments in (
            ["--config"],
            ["--config="],
            ["--data-dir"],
            ["--config", "--read-only"],
            ["--config=a", "--config=b"],
            ["--data-dir=a", "--data-dir=b"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                invocation(arguments)

    def test_no_editable_or_user_site_imports_leak_into_runtime(self) -> None:
        original = {
            "PYTHONPATH": "/checkout/src",
            "PYTHONHOME": "/old",
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "not-a-real-key",
        }
        environment = isolated_environment(Path("/fresh/runtime"), original)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("PYTHONHOME", environment)
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(environment["VIRTUAL_ENV"], "/fresh/runtime")
        self.assertEqual(environment["PATH"], "/fresh/runtime/bin" + os.pathsep + "/usr/bin")
        self.assertEqual(environment["OPENAI_API_KEY"], original["OPENAI_API_KEY"])
        self.assertEqual(original["PYTHONPATH"], "/checkout/src")

    def test_shared_external_target_is_namespaced_for_each_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            shared = base / "shared-target"
            roots = [base / "checkout-a", base / "checkout-b"]
            targets = [cargo_target(root, base / "cache", {"CARGO_TARGET_DIR": str(shared)}) for root in roots]
            self.assertNotEqual(*targets)
            for root, target in zip(roots, targets, strict=True):
                identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
                self.assertEqual(target, shared / f"ngn-dev-{identity}")

    def test_canonical_checkout_and_external_target_aliases_share_one_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, shared = base / "checkout", base / "shared-target"
            root.mkdir()
            shared.mkdir()
            root_alias, target_alias = base / "checkout-alias", base / "target-alias"
            root_alias.symlink_to(root, target_is_directory=True)
            target_alias.symlink_to(shared, target_is_directory=True)
            expected = cargo_target(root, base / "cache", {"CARGO_TARGET_DIR": str(shared)})
            self.assertEqual(
                cargo_target(root_alias / "ngn/..", base / "other-cache", {"CARGO_TARGET_DIR": str(target_alias)}),
                expected,
            )

    def test_checkout_local_target_is_reused_including_ci_and_cache_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "ngn/target"
            self.assertEqual(cargo_target(root, root / "cache", {"CARGO_TARGET_DIR": str(target)}), target)
            self.assertEqual(cargo_target(root, root / "cache", {}), root / "cache/target")
            target.mkdir(parents=True)
            alias = root / "target-alias"
            alias.symlink_to(target, target_is_directory=True)
            self.assertEqual(cargo_target(root, root / "cache", {"CARGO_TARGET_DIR": str(alias)}), target)

    def test_checkout_local_symlink_to_external_target_still_gets_a_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, shared = base / "checkout", base / "shared-target"
            root.mkdir()
            shared.mkdir()
            alias = root / "target"
            alias.symlink_to(shared, target_is_directory=True)
            selected = cargo_target(root, base / "cache", {"CARGO_TARGET_DIR": str(alias)})
            self.assertEqual(selected.parent, shared)
            self.assertRegex(selected.name, r"^ngn-dev-[0-9a-f]{16}$")
            sibling = cargo_target(root, base / "cache", {"CARGO_TARGET_DIR": str(base / "checkout-other/target")})
            self.assertEqual(sibling.parent, base / "checkout-other/target")

    def test_custom_external_dev_cache_namespaces_its_default_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cache = base / "shared-dev-cache"
            environment = {"NGN_DEV_CACHE": str(cache)}
            first = cargo_target(base / "checkout-a", cache, environment)
            second = cargo_target(base / "checkout-b", cache, environment)
            self.assertEqual(first.parent, cache / "target")
            self.assertEqual(second.parent, cache / "target")
            self.assertNotEqual(first, second)

    def test_default_external_cache_target_has_a_checkout_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
            cache = base / ".cache/ngn-rust-dev" / identity
            self.assertEqual(cargo_target(root, cache, {}), cache / "target" / f"ngn-dev-{identity}")

    def test_relative_paths_resolve_from_launcher_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            relative_root = Path(os.path.relpath(root))
            selected = cargo_target(relative_root, Path("dev-cache"), {"CARGO_TARGET_DIR": "shared-target"})
            self.assertEqual(selected, cargo_target(root, Path("dev-cache"), {"CARGO_TARGET_DIR": "shared-target"}))
            self.assertEqual(selected.parent, Path.cwd() / "shared-target")
            self.assertEqual(cargo_target(relative_root, Path("dev-cache"), {}).parent, Path.cwd() / "dev-cache/target")
            local = root / "ngn/target"
            self.assertEqual(
                cargo_target(relative_root, Path("dev-cache"), {"CARGO_TARGET_DIR": os.path.relpath(local)}), local
            )

    def test_target_selection_does_not_mutate_environment_or_create_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = {"CARGO_TARGET_DIR": str(base / "shared-target"), "OPENAI_API_KEY": "not-a-real-key"}
            environment = dict(original)
            process_environment = dict(os.environ)
            cargo_target(base / "checkout", base / "cache", environment)
            self.assertEqual(environment, original)
            self.assertEqual(dict(os.environ), process_environment)
            self.assertEqual(list(base.iterdir()), [])

    def test_default_leaves_discovery_to_the_native_cli(self) -> None:
        result = command(
            Path("/stage/app/ngn"), Invocation("", "", ["doctor"]), Path("/stage/config.json"), Path("/dev/state")
        )
        self.assertEqual(result, ["/stage/app/ngn", "--data-dir", "/dev/state", "doctor"])

    def test_explicit_config_uses_staged_config_and_forwards_other_arguments(self) -> None:
        args = Invocation("chosen.json", "", ["serve", "--port", "18768"])
        result = command(Path("/stage/app/ngn"), args, Path("/stage/config.json"), Path("/dev/state"))
        self.assertEqual(
            result,
            [
                "/stage/app/ngn",
                "--config",
                "/stage/config.json",
                "--data-dir",
                "/dev/state",
                "serve",
                "--port",
                "18768",
            ],
        )

    def test_acceptance_uses_no_operator_configuration_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "stage"
            stage.mkdir()
            runtime = stage / "runtime"
            environment = {"PATH": str(runtime / "bin")}
            operator_state = Path(directory) / "operator-state"
            operator_config = Path(directory) / "operator-config.json"
            operator_config.write_text("not a config")
            with (
                patch.object(
                    sys,
                    "argv",
                    ["dev", "--test", "--config", str(operator_config), "--data-dir", str(operator_state)],
                ),
                patch("dev.version_check"),
                patch("dev.build", return_value=(stage, runtime, environment)),
                patch("dev.offline_cache_probe") as probe,
            ):
                self.assertEqual(main(), 0)
            probe.assert_called_once_with(stage, runtime)
            self.assertEqual(operator_config.read_text(), "not a config")
            self.assertFalse(operator_state.exists())
            self.assertFalse((stage / "config.json").exists())

    def test_source_identity_includes_uncommitted_and_untracked_code_not_ignored_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
            subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", str(root)], env=environment, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.invalid"], cwd=root, env=environment, check=True
            )
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, env=environment, check=True)
            subprocess.run(
                ["git", "config", "core.hooksPath", str(root / "empty-hooks")], cwd=root, env=environment, check=True
            )
            (root / "source.py").write_text("original\n")
            (root / ".gitignore").write_text("state/\n")
            subprocess.run(["git", "add", "."], cwd=root, env=environment, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, env=environment, check=True)
            clean = source_snapshot(root)
            self.assertFalse(clean[2])
            (root / "source.py").write_text("modified\n")
            changed = source_snapshot(root)
            self.assertEqual(changed[0], clean[0])
            self.assertNotEqual(changed[1], clean[1])
            self.assertTrue(changed[2])
            (root / "new.py").write_text("untracked\n")
            untracked = source_snapshot(root)
            self.assertNotEqual(untracked[1], changed[1])
            (root / "state").mkdir()
            (root / "state/private.db").write_text("not source\n")
            self.assertEqual(source_snapshot(root), untracked)
            foreign_index = root / "state/foreign-index"
            foreign_index.write_bytes(b"not a Git index")
            with patch.dict(
                os.environ, {"GIT_INDEX_FILE": str(foreign_index), "GIT_DIR": "/missing", "GIT_WORK_TREE": "/missing"}
            ):
                self.assertEqual(source_snapshot(root), untracked)
            self.assertEqual(foreign_index.read_bytes(), b"not a Git index")


if __name__ == "__main__":
    unittest.main()
