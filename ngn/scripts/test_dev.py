"""The dev launcher must run installed artifacts while preserving operator intent."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev import Invocation, cargo_target, command, invocation, isolated_environment, runtime_config, source_snapshot


class DevTests(unittest.TestCase):
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
            ["--config", "--demo"],
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

    def test_config_rebinds_only_runner_without_mutating_operator_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = {
                "config_version": 1,
                "runner": ["/old/python", "-m", "ngn_nagents"],
                "provider": "chosen",
                "read_only": True,
                "providers": {"chosen": {"api_key_env": "MY_PROVIDER_KEY"}},
            }
            path.write_text(json.dumps(original))
            result = runtime_config(path, Path("/fresh/runtime"))
            self.assertEqual(result["runner"], ["/fresh/runtime/bin/python", "-I", "-m", "ngn_nagents"])
            self.assertEqual(result["providers"], original["providers"])
            self.assertIs(result["read_only"], True)
            self.assertEqual(json.loads(path.read_text()), original)
            invalid_values: tuple[object, ...] = ([], "old config", {"config_version": 2})
            for invalid in invalid_values:
                path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    runtime_config(path, Path("/fresh/runtime"))

    def test_default_is_demo_and_explicit_config_is_live_without_legacy_binary(self) -> None:
        args = Invocation("", "", ["serve", "--port", "18768"])
        result = command(Path("/stage/app/ngn"), args, Path("/stage/config.json"), Path("/dev/state"), demo=True)
        self.assertEqual(
            result,
            [
                "/stage/app/ngn",
                "--config",
                "/stage/config.json",
                "--data-dir",
                "/dev/state",
                "--demo",
                "serve",
                "--port",
                "18768",
            ],
        )
        live = command(Path("/stage/app/ngn"), args, Path("/stage/config.json"), Path("/dev/state"), demo=False)
        self.assertNotIn("--demo", live)
        explicit = command(
            Path("/stage/app/ngn"), Invocation("", "", ["--demo"]), Path("/config"), Path("/state"), demo=True
        )
        self.assertEqual(explicit.count("--demo"), 1)

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
