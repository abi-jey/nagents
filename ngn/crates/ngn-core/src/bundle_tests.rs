use super::*;

/// Intentionally inert inventory fixture. Never presented as an actual Python
/// distribution and never executed by these metadata/config unit tests.
pub(crate) fn fixture(root: &Path) -> BundleManifest {
    fs::create_dir_all(root.join("runtime/bin")).unwrap();
    let ca_file = "runtime/lib/python3.13/site-packages/certifi/cacert.pem";
    fs::create_dir_all(root.join(ca_file).parent().unwrap()).unwrap();
    for (name, bytes) in [
        ("ngn", "inert native fixture"),
        ("runtime/bin/python3.13", "inert Python fixture"),
        (ca_file, "fixture CA"),
    ] {
        fs::write(root.join(name), bytes).unwrap();
    }
    #[cfg(unix)]
    std::os::unix::fs::symlink("python3.13", root.join("runtime/bin/python3")).unwrap();
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
            artifact: "cpython-test-fixture.tar.gz".into(),
            sha256: "b".repeat(64),
        },
        runner: vec![
            "runtime/bin/python3".into(),
            "-I".into(),
            "-B".into(),
            "-m".into(),
            "ngn_nagents".into(),
        ],
        ca_file: ca_file.into(),
        files: ["ngn", "runtime/bin/python3.13", ca_file]
            .into_iter()
            .map(|name| (name.into(), file_hash(&root.join(name)).unwrap()))
            .collect(),
        symlinks: [("runtime/bin/python3".into(), "python3.13".into())].into(),
    };
    fs::write(
        root.join("bundle.json"),
        serde_json::to_vec(&manifest).unwrap(),
    )
    .unwrap();
    manifest
}

#[test]
fn rust_namespace_is_canonical_and_ordered() {
    for version in [
        "0.28.5",
        "1.0.0",
        "v0.100.2",
        "0.100.02",
        "0.100.-1",
        "0.100.+2",
        "0.100.2-rc1",
        "0.100.2+build",
        "0.100.4294967296",
        " 0.100.2",
        "0.100.2\n",
    ] {
        assert!(
            ReleaseVersion::parse(version).is_err(),
            "accepted {version:?}"
        );
    }
    assert!(ReleaseVersion::tag("v0.100.10").unwrap() > ReleaseVersion::tag("v0.100.9").unwrap());
    assert!(ReleaseVersion::tag("0.100.2").is_err());
}

#[test]
fn metadata_rejects_mismatched_schema_platform_versions_and_provenance() {
    let directory = crate::test_support::private_tempdir().unwrap();
    let manifest = fixture(directory.path());
    manifest.validate().unwrap();
    for (name, value) in [
        ("bundle_schema", serde_json::json!(2)),
        ("platform", serde_json::json!("linux-aarch64")),
        ("runtime_protocol", serde_json::json!(2)),
        ("ngn_version", serde_json::json!("0.28.5")),
        ("adapter_version", serde_json::json!("0.100.3")),
        ("nagents_version", serde_json::json!("0.28.5rc1")),
        ("source_commit", serde_json::json!("A".repeat(40))),
        ("ca_file", serde_json::json!("../ca.pem")),
        (
            "runner",
            serde_json::json!(["/usr/bin/python3", "-m", "evil"]),
        ),
    ] {
        let mut value_manifest = serde_json::to_value(&manifest).unwrap();
        value_manifest[name] = value;
        let changed: BundleManifest = serde_json::from_value(value_manifest).unwrap();
        assert!(changed.validate().is_err(), "accepted {name}");
    }
    for (implementation, version, hash) in [
        ("pypy", "3.13.18", "b".repeat(64)),
        ("cpython", "3.12.1", "b".repeat(64)),
        ("cpython", "3.13.18", "z".repeat(64)),
    ] {
        let mut changed = manifest.clone();
        changed.python.implementation = implementation.into();
        changed.python.version = version.into();
        changed.python.sha256 = hash;
        assert!(changed.validate().is_err());
    }
}

#[test]
fn exact_inventory_hashes_unlisted_files_and_duplicate_json_keys() {
    let directory = crate::test_support::private_tempdir().unwrap();
    let manifest = fixture(directory.path());
    manifest.verify_tree(directory.path()).unwrap();
    fs::write(directory.path().join("extra.py"), "not inventoried").unwrap();
    assert!(manifest.verify_tree(directory.path()).is_err());
    fs::remove_file(directory.path().join("extra.py")).unwrap();
    fs::write(directory.path().join("ngn"), "wrong hash").unwrap();
    assert!(manifest.verify_tree(directory.path()).is_err());
    fs::remove_file(directory.path().join("ngn")).unwrap();
    assert!(manifest.verify_tree(directory.path()).is_err());
    let duplicate = format!(
        "{{\"x\":\"{}\",\"x\":\"{}\"}}",
        "a".repeat(64),
        "b".repeat(64)
    );
    #[derive(Deserialize)]
    struct Inventory {
        #[serde(deserialize_with = "unique_map")]
        files: BTreeMap<String, String>,
    }
    assert!(serde_json::from_str::<Inventory>(&format!("{{\"files\":{duplicate}}}")).is_err());
    let valid: Inventory = serde_json::from_str("{\"files\":{}}").unwrap();
    assert!(valid.files.is_empty());
}

#[test]
fn paths_and_only_internal_cpython_links() {
    for path in [
        "/ngn", "../ngn", "a/../b", "a//b", "a/./b", "a/", "C:/ngn", "a\\b", "a\nb", "",
    ] {
        assert!(relative_path(path).is_err(), "accepted {path:?}");
    }
    let directory = crate::test_support::private_tempdir().unwrap();
    let manifest = fixture(directory.path());
    for target in [
        "/usr/bin/python3",
        "../../outside",
        "../bin/../../outside",
        "python3",
        "python3.13/..",
        "./python3.13",
    ] {
        let mut changed = manifest.clone();
        changed
            .symlinks
            .insert("runtime/bin/python3".into(), target.into());
        if changed.validate().is_ok() {
            // Self-cycles/dangling paths are rejected by the filesystem pass.
            #[cfg(unix)]
            {
                fs::remove_file(directory.path().join("runtime/bin/python3")).unwrap();
                std::os::unix::fs::symlink(target, directory.path().join("runtime/bin/python3"))
                    .unwrap();
                assert!(changed.verify_tree(directory.path()).is_err());
            }
        }
    }
    let mut changed = manifest.clone();
    changed
        .symlinks
        .insert("runtime/sitecustomize.py".into(), "bin/python3.13".into());
    assert!(changed.validate().is_err());
    let mut changed = manifest;
    changed
        .files
        .insert("runtime/bin/python3/child".into(), "a".repeat(64));
    assert!(changed.validate().is_err());
}

#[cfg(unix)]
#[test]
fn setid_and_hardlinked_files_are_rejected() {
    use std::os::unix::fs::PermissionsExt;
    let directory = crate::test_support::private_tempdir().unwrap();
    let manifest = fixture(directory.path());
    fs::set_permissions(
        directory.path().join("ngn"),
        fs::Permissions::from_mode(0o4755),
    )
    .unwrap();
    assert!(manifest.verify_tree(directory.path()).is_err());
    fs::set_permissions(
        directory.path().join("ngn"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    fs::hard_link(directory.path().join("ngn"), directory.path().join("alias")).unwrap();
    assert!(manifest.verify_tree(directory.path()).is_err());
}

#[test]
fn ca_defaults_are_limited_to_bundled_children_and_respect_either_operator_setting() {
    let directory = crate::test_support::private_tempdir().unwrap();
    fixture(directory.path());
    let runtime = BundledRuntime::detect(&directory.path().join("ngn"))
        .unwrap()
        .unwrap();
    for (cert_file, cert_dir) in [(false, false), (true, false), (false, true), (true, true)] {
        let mut command = tokio::process::Command::new("unused");
        runtime.environment_with_ca_settings(&mut command, cert_file, cert_dir);
        let env: BTreeMap<_, _> = command.as_std().get_envs().collect();
        assert_eq!(
            env.contains_key(std::ffi::OsStr::new("SSL_CERT_FILE")),
            !cert_file && !cert_dir
        );
        assert_eq!(env.get(std::ffi::OsStr::new("PYTHONPATH")), Some(&None));
        assert_eq!(env.get(std::ffi::OsStr::new("PYTHONHOME")), Some(&None));
    }
}
