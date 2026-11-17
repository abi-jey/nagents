#![cfg(unix)]

#[path = "../../ngn-core/src/test_support.rs"]
mod test_support;

use std::{
    collections::BTreeMap,
    fs,
    os::unix::fs::{DirBuilderExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Output},
};

use flate2::{Compression, write::GzEncoder};
use ngn_core::bundle::{BundleManifest, PLATFORM, PythonBundle, file_hash};
use serde_json::json;

fn private(path: &Path) {
    if let Some(parent) = path.parent()
        && !parent.exists()
    {
        private(parent);
    }
    let mut builder = fs::DirBuilder::new();
    builder.mode(0o700);
    if let Err(error) = builder.create(path) {
        assert_eq!(error.kind(), std::io::ErrorKind::AlreadyExists);
    }
}

fn write(path: &Path, bytes: &[u8]) {
    fs::write(path, bytes).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}

fn command(root: &Path, binary: &Path) -> Command {
    let mut command = Command::new(binary);
    command
        .env("HOME", root)
        .env("PATH", "")
        .env("XDG_DATA_HOME", root.join("protected-data"))
        .env("XDG_CONFIG_HOME", root.join("protected-config"))
        .env("XDG_CACHE_HOME", root.join("protected-cache"))
        .arg("--config")
        .arg(root.join("missing-config.json"))
        .arg("--data-dir")
        .arg(root.join("protected-state"))
        .arg("--workspace")
        .arg(root.join("missing-workspace"));
    command
}

fn succeeded(output: &Output) {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn no_host_state(root: &Path) {
    for name in [
        "protected-state",
        "protected-data",
        "protected-config",
        "protected-cache",
        ".nagents",
        ".codex",
    ] {
        assert!(!root.join(name).exists(), "touched {name}");
    }
}

#[test]
fn installer_and_unmanaged_check_dispatch_before_config_workspace_and_host() {
    let root = test_support::private_tempdir().unwrap();
    let output = command(root.path(), Path::new(env!("CARGO_BIN_EXE_ngn")))
        .args(["update", "--check"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    let error = String::from_utf8_lossy(&output.stderr);
    assert!(
        error.contains("not the current executable of a managed"),
        "{error}"
    );
    assert!(!error.contains("configuration") && !error.contains("Workspace"));
    let archive = root.path().join("corrupt.tar.gz");
    write(&archive, b"not a gzip bundle");
    let hash = file_hash(&archive).unwrap();
    let output = command(root.path(), Path::new(env!("CARGO_BIN_EXE_ngn")))
        .args(["install", "--bundle"])
        .arg(&archive)
        .args(["--sha256", &hash, "--version", "0.100.2"])
        .arg("--prefix")
        .arg(root.path().join("prefix"))
        .arg("--bin-dir")
        .arg(root.path().join("bin"))
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(!root.path().join("prefix/current").exists());
    assert!(!String::from_utf8_lossy(&output.stderr).contains("configuration"));
    no_host_state(root.path());
}

/// A real native binary with INERT inventory placeholders. Used only to prove
/// doctor/check never launch Python; not a functioning bundled runtime fixture.
fn managed(root: &Path) -> (PathBuf, PathBuf, BundleManifest) {
    let prefix = root.join("prefix");
    let release = prefix.join("releases").join(env!("CARGO_PKG_VERSION"));
    let bin = root.join("bin");
    private(&release);
    private(&bin);
    let native = release.join("ngn");
    fs::copy(env!("CARGO_BIN_EXE_ngn"), &native).unwrap();
    fs::set_permissions(&native, fs::Permissions::from_mode(0o700)).unwrap();
    let python = "runtime/bin/python3.13";
    let ca = "runtime/lib/python3.13/site-packages/certifi/cacert.pem";
    private(release.join(python).parent().unwrap());
    private(release.join(ca).parent().unwrap());
    write(
        &release.join(python),
        b"INERT placeholder: execution must fail",
    );
    write(&release.join(ca), b"inert CA fixture");
    symlink("python3.13", release.join("runtime/bin/python3")).unwrap();
    let manifest = BundleManifest {
        bundle_schema: 1,
        ngn_version: env!("CARGO_PKG_VERSION").into(),
        nagents_version: "0.28.5".into(),
        adapter_version: env!("CARGO_PKG_VERSION").into(),
        runtime_protocol: ngn_protocol::VERSION,
        platform: PLATFORM.into(),
        source_commit: "a".repeat(40),
        official: false,
        python: PythonBundle {
            implementation: "cpython".into(),
            version: "3.13.18".into(),
            artifact: "inert-unit-fixture.tar.gz".into(),
            sha256: "b".repeat(64),
        },
        runner: ["runtime/bin/python3", "-I", "-B", "-m", "ngn_nagents"]
            .map(String::from)
            .into(),
        ca_file: ca.into(),
        files: ["ngn", python, ca]
            .into_iter()
            .map(|name| (name.into(), file_hash(&release.join(name)).unwrap()))
            .collect(),
        symlinks: BTreeMap::from([("runtime/bin/python3".into(), "python3.13".into())]),
    };
    write(
        &release.join("bundle.json"),
        &serde_json::to_vec(&manifest).unwrap(),
    );
    write(&prefix.join("install.json"), &serde_json::to_vec(&json!({"install_schema":1,"installation_id":uuid::Uuid::new_v4().to_string(),"prefix":prefix,"bin_dir":bin})).unwrap());
    write(&prefix.join(".install.lock"), b"");
    symlink(
        format!("releases/{}", env!("CARGO_PKG_VERSION")),
        prefix.join("current"),
    )
    .unwrap();
    symlink(prefix.join("current/ngn"), bin.join("ngn")).unwrap();
    (prefix, bin, manifest)
}

#[test]
fn managed_local_check_noop_and_doctor_need_no_python_config_or_host_state() {
    let root = test_support::private_tempdir().unwrap();
    let (prefix, bin, manifest) = managed(root.path());
    let release = prefix.join("releases").join(&manifest.ngn_version);
    let bundle = root.path().join("inert-doctor-fixture.tar.gz");
    let gzip = GzEncoder::new(fs::File::create(&bundle).unwrap(), Compression::fast());
    let mut archive = tar::Builder::new(gzip);
    archive.follow_symlinks(false);
    archive.append_dir_all("ngn", &release).unwrap();
    archive.into_inner().unwrap().finish().unwrap();
    let hash = file_hash(&bundle).unwrap();
    for check in [true, false] {
        let mut cli = command(root.path(), &bin.join("ngn"));
        cli.args(["update", "--bundle"]).arg(&bundle).args([
            "--sha256",
            &hash,
            "--version",
            &manifest.ngn_version,
        ]);
        if check {
            cli.arg("--check");
        }
        let output = cli.output().unwrap();
        succeeded(&output);
        assert!(String::from_utf8_lossy(&output.stdout).contains("already current"));
    }
    let output = Command::new(bin.join("ngn"))
        .env("HOME", root.path())
        .env("PATH", "")
        .arg("--data-dir")
        .arg(root.path().join("protected-state"))
        .arg("--workspace")
        .arg(root.path())
        .arg("doctor")
        .output()
        .unwrap();
    succeeded(&output);
    let text = String::from_utf8_lossy(&output.stdout);
    assert!(text.contains("Bundled CPython: 3.13.18"));
    assert!(text.contains(release.join("runtime/bin/python3").to_str().unwrap()));
    assert!(!text.contains("prefix/current/runtime"));
    no_host_state(root.path());
}

#[test]
fn foreign_python_entry_point_is_never_overwritten() {
    let root = test_support::private_tempdir().unwrap();
    let bin = root.path().join(".local/bin");
    private(&bin);
    write(&bin.join("ngn"), b"#!/old/venv/python\nlegacy ngn\n");
    let output = command(root.path(), Path::new(env!("CARGO_BIN_EXE_ngn")))
        .arg("install")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("--bin-dir"));
    assert_eq!(
        fs::read(bin.join("ngn")).unwrap(),
        b"#!/old/venv/python\nlegacy ngn\n"
    );
    assert!(
        !root
            .path()
            .join(".local/share/ngn-harness/current")
            .exists()
    );
    no_host_state(root.path());
}

#[test]
fn default_install_uses_ngn_harness_and_never_touches_legacy_or_native_host_state() {
    for use_xdg in [false, true] {
        let root = test_support::private_tempdir().unwrap();
        let xdg = root
            .path()
            .join(if use_xdg { "xdg" } else { ".local/share" });
        private(&xdg);
        for name in ["ngn", "ngn-rust"] {
            let protected = xdg.join(name);
            private(&protected);
            write(&protected.join("sentinel"), b"protected existing state");
            fs::set_permissions(protected, fs::Permissions::from_mode(0o000)).unwrap();
        }
        let bundle = root.path().join("corrupt-local-bundle.tar.gz");
        write(&bundle, b"not gzip");
        let hash = file_hash(&bundle).unwrap();
        let mut cli = command(root.path(), Path::new(env!("CARGO_BIN_EXE_ngn")));
        if use_xdg {
            cli.env("XDG_DATA_HOME", &xdg);
        } else {
            cli.env_remove("XDG_DATA_HOME");
        }
        let output = cli
            .args(["install", "--bundle"])
            .arg(&bundle)
            .args(["--sha256", &hash])
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(xdg.join("ngn-harness/install.json").is_file());
        for name in ["ngn", "ngn-rust"] {
            let protected = xdg.join(name);
            assert_eq!(
                protected.metadata().unwrap().permissions().mode() & 0o777,
                0o000
            );
            fs::set_permissions(&protected, fs::Permissions::from_mode(0o700)).unwrap();
            assert_eq!(
                fs::read(protected.join("sentinel")).unwrap(),
                b"protected existing state"
            );
            assert_eq!(fs::read_dir(&protected).unwrap().count(), 1);
            for new in [".identity", ".install.lock", "install.json"] {
                assert!(!protected.join(new).exists());
            }
        }
        no_host_state(root.path());
    }
}
