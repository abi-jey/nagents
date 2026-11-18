//! Synthetic inventory/transaction tests, not a substitute for a real CPython
//! bundle acceptance run. Only the private SyntheticProbe bypasses execution.

use super::*;
use flate2::{Compression, write::GzEncoder};
use ngn_core::bundle::{PLATFORM, PythonBundle};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    io::Cursor,
    sync::{
        Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};

const CA: &str = "runtime/lib/python3.13/site-packages/certifi/cacert.pem";

#[derive(Clone)]
struct Member {
    name: String,
    kind: tar::EntryType,
    mode: u32,
    bytes: Vec<u8>,
    target: String,
}

fn member(name: &str, bytes: &[u8]) -> Member {
    Member {
        name: name.into(),
        kind: tar::EntryType::Regular,
        mode: 0o644,
        bytes: bytes.into(),
        target: String::new(),
    }
}

fn metadata(version: &str, official: bool) -> BundleManifest {
    BundleManifest {
        bundle_schema: 1,
        ngn_version: version.into(),
        nagents_version: "0.28.5".into(),
        adapter_version: version.into(),
        runtime_protocol: ngn_protocol::VERSION,
        platform: PLATFORM.into(),
        source_commit: "a".repeat(40),
        official,
        python: PythonBundle {
            implementation: "cpython".into(),
            version: "3.13.18".into(),
            artifact: "synthetic-not-a-python-runtime.tar.gz".into(),
            sha256: "b".repeat(64),
        },
        runner: ["runtime/bin/python3", "-I", "-B", "-m", "ngn_nagents"]
            .map(String::from)
            .into(),
        ca_file: CA.into(),
        files: [
            ("ngn", "inert native fixture"),
            ("runtime/bin/python3.13", "inert Python fixture"),
            (CA, "fixture CA"),
        ]
        .into_iter()
        .map(|(name, bytes)| {
            (
                name.into(),
                format!("{:x}", Sha256::digest(bytes.as_bytes())),
            )
        })
        .collect(),
        symlinks: [("runtime/bin/python3".into(), "python3.13".into())].into(),
    }
}

fn members(manifest: &BundleManifest) -> Vec<Member> {
    let mut root = member("ngn/", b"");
    root.kind = tar::EntryType::Directory;
    let mut link = member("ngn/runtime/bin/python3", b"");
    link.kind = tar::EntryType::Symlink;
    link.target = "python3.13".into();
    vec![
        root,
        member("ngn/ngn", b"inert native fixture"),
        member("ngn/runtime/bin/python3.13", b"inert Python fixture"),
        member(&format!("ngn/{CA}"), b"fixture CA"),
        link,
        member("ngn/bundle.json", &serde_json::to_vec(manifest).unwrap()),
    ]
}

fn archive_bytes(members: &[Member]) -> Vec<u8> {
    let gzip = GzEncoder::new(Vec::new(), Compression::fast());
    let mut tar = tar::Builder::new(gzip);
    for member in members {
        let mut header = tar::Header::new_gnu();
        header.set_mode(member.mode);
        header.set_size(member.bytes.len() as u64);
        header.set_entry_type(member.kind);
        // Raw paths are intentional: the test writer must be able to represent
        // traversal that tar's safe public set_path() correctly refuses.
        assert!(member.name.len() < 100 && member.target.len() < 100);
        header.as_mut_bytes()[..member.name.len()].copy_from_slice(member.name.as_bytes());
        header.as_mut_bytes()[157..157 + member.target.len()]
            .copy_from_slice(member.target.as_bytes());
        header.set_cksum();
        tar.append(&header, Cursor::new(&member.bytes)).unwrap();
    }
    tar.into_inner().unwrap().finish().unwrap()
}

fn local(
    directory: &Path,
    manifest: &BundleManifest,
    edit: impl FnOnce(&mut Vec<Member>),
) -> Source {
    let mut payload = members(manifest);
    edit(&mut payload);
    let path = directory.join(format!("fixture-{}.tar.gz", Uuid::new_v4()));
    fs::write(&path, archive_bytes(&payload)).unwrap();
    Source {
        version: None,
        sha256: Some(file_hash(&path).unwrap()),
        archive: Some(path),
    }
}

#[derive(Default)]
struct FixtureTransport {
    bodies: BTreeMap<String, Vec<u8>>,
    calls: Mutex<Vec<String>>,
    fail_payload: bool,
}

impl Transport for FixtureTransport {
    async fn fetch(&self, url: &str, output: &mut File, limit: u64) -> Result<()> {
        self.calls.lock().unwrap().push(url.into());
        let bytes = self
            .bodies
            .get(url)
            .context("Unexpected network call in private fixture")?;
        if self.fail_payload && url.ends_with("bundle.tar.gz") {
            output.write_all(&bytes[..bytes.len() / 2])?;
            bail!("Synthetic truncated download");
        }
        ensure!(
            bytes.len() as u64 <= limit,
            "Synthetic response exceeds limit"
        );
        output.write_all(bytes)?;
        Ok(())
    }
}

fn release(manifest: &BundleManifest, archive: &[u8]) -> ReleaseManifest {
    let name = format!("ngn-v{}-{PLATFORM}-bundle.tar.gz", manifest.ngn_version);
    ReleaseManifest {
        schema_version: 2,
        commit: manifest.source_commit.clone(),
        ngn_version: manifest.ngn_version.clone(),
        nagents_version: manifest.nagents_version.clone(),
        runtime_protocol: ngn_protocol::VERSION,
        platform: PLATFORM.into(),
        bundle: name.clone(),
        artifacts: [(name, format!("{:x}", Sha256::digest(archive)))].into(),
    }
}

fn network(manifest: &BundleManifest) -> FixtureTransport {
    let payload = archive_bytes(&members(manifest));
    let outer = release(manifest, &payload);
    let tag = format!("v{}", manifest.ngn_version);
    FixtureTransport {
        bodies: [
            (
                format!("{API}?per_page=100&page=1"),
                serde_json::to_vec(&json!([
                    {"tag_name":"v0.28.99", "draft":false, "prerelease":false},
                    {"tag_name":"v0.100.999", "draft":true, "prerelease":false},
                    {"tag_name":"v0.100.998", "draft":false, "prerelease":true},
                    {"tag_name":"v0.100.997-rc1", "draft":false, "prerelease":false},
                    {"tag_name":tag, "draft":false, "prerelease":false}
                ]))
                .unwrap(),
            ),
            (
                format!("{API}/tags/{tag}"),
                serde_json::to_vec(&json!({"tag_name":tag,"draft":false,"prerelease":false}))
                    .unwrap(),
            ),
            (
                format!("{DOWNLOAD}/{tag}/release-manifest.json"),
                serde_json::to_vec(&outer).unwrap(),
            ),
            (format!("{DOWNLOAD}/{tag}/{}", outer.bundle), payload),
        ]
        .into(),
        ..Default::default()
    }
}

struct SyntheticProbe {
    calls: AtomicUsize,
    fail: bool,
}
impl SyntheticProbe {
    fn success() -> Self {
        Self {
            calls: AtomicUsize::new(0),
            fail: false,
        }
    }
}
impl Probe for SyntheticProbe {
    async fn verify(&self, _: &Path, _: &BundleManifest) -> Result<()> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        ensure!(!self.fail, "Synthetic validation failure");
        Ok(())
    }
}

fn installation(root: &Path) -> Installation {
    Installation::prepare(root.join("prefix"), root.join("bin")).unwrap()
}

#[tokio::test]
async fn local_install_check_noop_pin_upgrade_and_downgrade_are_failure_atomic() {
    let directory = crate::test_support::private_tempdir().unwrap();
    let install = installation(directory.path());
    let transport = FixtureTransport::default();
    let probe = SyntheticProbe::success();
    let cancel = CancellationToken::new();
    let first = local(directory.path(), &metadata("0.100.2", false), |_| {});
    assert_eq!(
        apply(&install, &first, false, &transport, &probe, &cancel)
            .await
            .unwrap(),
        Outcome::Installed(ReleaseVersion::parse("0.100.2").unwrap())
    );
    let identity = fs::read(install.identity.prefix.join("install.json")).unwrap();
    let native_hash = file_hash(&install.identity.prefix.join("releases/0.100.2/ngn")).unwrap();
    fs::write(directory.path().join("protected-state"), b"do not touch").unwrap();
    let mut next = local(directory.path(), &metadata("0.100.3", false), |_| {});
    next.version = Some("0.100.3".into());
    assert_eq!(
        apply(&install, &next, true, &transport, &probe, &cancel)
            .await
            .unwrap(),
        Outcome::Available(ReleaseVersion::parse("0.100.3").unwrap())
    );
    assert!(!install.identity.prefix.join("releases/0.100.3").exists());
    assert_eq!(
        fs::read_link(install.identity.prefix.join("current")).unwrap(),
        Path::new("releases/0.100.2")
    );
    assert_eq!(
        apply(&install, &next, false, &transport, &probe, &cancel)
            .await
            .unwrap(),
        Outcome::Installed(ReleaseVersion::parse("0.100.3").unwrap())
    );
    assert_eq!(
        apply(&install, &next, false, &transport, &probe, &cancel)
            .await
            .unwrap(),
        Outcome::Current(ReleaseVersion::parse("0.100.3").unwrap())
    );
    assert_eq!(probe.calls.load(Ordering::SeqCst), 2);
    assert!(
        apply(&install, &first, false, &transport, &probe, &cancel)
            .await
            .unwrap_err()
            .to_string()
            .contains("Downgrades")
    );
    next.version = Some("0.100.4".into());
    assert!(
        apply(&install, &next, false, &transport, &probe, &cancel)
            .await
            .is_err()
    );
    assert_eq!(
        file_hash(&install.identity.prefix.join("releases/0.100.2/ngn")).unwrap(),
        native_hash
    );
    assert_eq!(
        fs::read(install.identity.prefix.join("install.json")).unwrap(),
        identity
    );
    assert_eq!(
        fs::read(directory.path().join("protected-state")).unwrap(),
        b"do not touch"
    );
    assert_eq!(
        fs::read_link(install.identity.bin_dir.join("ngn")).unwrap(),
        install.identity.prefix.join("current/ngn")
    );
    assert!(transport.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn official_discovery_is_rust_only_and_check_never_fetches_or_executes_payload() {
    let directory = crate::test_support::private_tempdir().unwrap();
    let install = installation(directory.path());
    let manifest = metadata("0.100.3", true);
    let transport = network(&manifest);
    let probe = SyntheticProbe::success();
    let source = Source {
        version: None,
        archive: None,
        sha256: None,
    };
    assert_eq!(
        apply(
            &install,
            &source,
            true,
            &transport,
            &probe,
            &CancellationToken::new()
        )
        .await
        .unwrap(),
        Outcome::Available(ReleaseVersion::parse("0.100.3").unwrap())
    );
    assert_eq!(probe.calls.load(Ordering::SeqCst), 0);
    {
        let calls = transport.calls.lock().unwrap();
        assert_eq!(calls.len(), 2);
        assert!(
            calls
                .iter()
                .all(|url| !url.contains("latest") && !url.ends_with("bundle.tar.gz"))
        );
    }
    let pinned = discover(&transport, Some(ReleaseVersion::parse("0.100.3").unwrap()))
        .await
        .unwrap();
    assert_eq!(pinned.ngn_version, "0.100.3");
    assert!(
        discover(&transport, Some(ReleaseVersion::parse("0.100.2").unwrap()))
            .await
            .is_err()
    );
    // SyntheticProbe is private to this test: the inventory placeholders are
    // never executed or represented as a real official Python distribution.
    assert_eq!(
        apply(
            &install,
            &source,
            false,
            &transport,
            &probe,
            &CancellationToken::new()
        )
        .await
        .unwrap(),
        Outcome::Installed(ReleaseVersion::parse("0.100.3").unwrap())
    );
    assert_eq!(probe.calls.load(Ordering::SeqCst), 1);
    let pinned = Source {
        version: Some("0.100.3".into()),
        archive: None,
        sha256: None,
    };
    assert_eq!(
        apply(
            &install,
            &pinned,
            true,
            &transport,
            &probe,
            &CancellationToken::new()
        )
        .await
        .unwrap(),
        Outcome::Current(ReleaseVersion::parse("0.100.3").unwrap())
    );
    assert_eq!(probe.calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn network_requires_official_inner_metadata_and_matching_commit_core_and_archive_hash() {
    for mutation in ["unofficial", "commit", "core", "hash", "platform"] {
        let directory = crate::test_support::private_tempdir().unwrap();
        let install = installation(directory.path());
        let bundle = metadata("0.100.2", mutation != "unofficial");
        let mut transport = network(&bundle);
        let url = format!("{DOWNLOAD}/v0.100.2/release-manifest.json");
        if mutation != "unofficial" {
            let mut outer: serde_json::Value =
                serde_json::from_slice(&transport.bodies[&url]).unwrap();
            match mutation {
                "commit" => outer["commit"] = json!("c".repeat(40)),
                "core" => outer["nagents_version"] = json!("0.28.6"),
                "hash" => {
                    let name = outer["bundle"].as_str().unwrap().to_owned();
                    outer["artifacts"][name] = json!("0".repeat(64))
                }
                "platform" => outer["platform"] = json!("linux-aarch64"),
                _ => unreachable!(),
            }
            transport
                .bodies
                .insert(url, serde_json::to_vec(&outer).unwrap());
        }
        let probe = SyntheticProbe::success();
        let source = Source {
            version: None,
            archive: None,
            sha256: None,
        };
        assert!(
            apply(
                &install,
                &source,
                false,
                &transport,
                &probe,
                &CancellationToken::new()
            )
            .await
            .is_err(),
            "accepted {mutation}"
        );
        assert_eq!(probe.calls.load(Ordering::SeqCst), 0);
        assert!(current(&install.identity).unwrap().is_none());
    }
}

#[tokio::test]
async fn failed_hash_inventory_probe_truncated_download_or_cancellation_never_changes_current() {
    let directory = crate::test_support::private_tempdir().unwrap();
    let install = installation(directory.path());
    let transport = FixtureTransport::default();
    let probe = SyntheticProbe::success();
    let first = local(directory.path(), &metadata("0.100.2", false), |_| {});
    apply(
        &install,
        &first,
        false,
        &transport,
        &probe,
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    let pointer = fs::read_link(install.identity.prefix.join("current")).unwrap();
    let identity = fs::read(install.identity.prefix.join("install.json")).unwrap();
    for failure in ["hash", "inventory", "probe", "cancel", "download"] {
        let mut source = local(directory.path(), &metadata("0.100.3", false), |members| {
            if failure == "inventory" {
                members[1].bytes.push(1);
            }
        });
        if failure == "hash" {
            source.sha256 = Some("0".repeat(64));
        }
        let probe = SyntheticProbe {
            calls: AtomicUsize::new(0),
            fail: failure == "probe",
        };
        let cancel = CancellationToken::new();
        if failure == "cancel" {
            cancel.cancel();
        }
        let mut transport = network(&metadata("0.100.3", true));
        if failure == "download" {
            transport.fail_payload = true;
            source.archive = None;
            source.sha256 = None;
        }
        assert!(
            apply(&install, &source, false, &transport, &probe, &cancel)
                .await
                .is_err(),
            "accepted {failure}"
        );
        assert_eq!(
            fs::read_link(install.identity.prefix.join("current")).unwrap(),
            pointer
        );
        assert_eq!(
            fs::read(install.identity.prefix.join("install.json")).unwrap(),
            identity
        );
        assert!(!install.identity.prefix.join("releases/0.100.3").exists());
    }
}

#[test]
fn malicious_tar_paths_types_modes_links_duplicates_and_inventory_sets_are_rejected() {
    for attack in [
        "traversal",
        "absolute",
        "other-root",
        "hardlink",
        "char",
        "block",
        "fifo",
        "setid",
        "duplicate",
        "link-escape",
        "unlisted",
        "missing",
        "hash",
        "pax-sparse",
    ] {
        let directory = crate::test_support::private_tempdir().unwrap();
        let mut entries = members(&metadata("0.100.2", false));
        match attack {
            "traversal" => entries[1].name = "ngn/../../outside".into(),
            "absolute" => entries[1].name = "/outside".into(),
            "other-root" => entries[1].name = "other/file".into(),
            "hardlink" => {
                entries[1].kind = tar::EntryType::Link;
                entries[1].target = "ngn/runtime/bin/python3.13".into();
                entries[1].bytes.clear();
            }
            "char" => {
                entries[1].kind = tar::EntryType::Char;
                entries[1].bytes.clear();
            }
            "block" => {
                entries[1].kind = tar::EntryType::Block;
                entries[1].bytes.clear();
            }
            "fifo" => {
                entries[1].kind = tar::EntryType::Fifo;
                entries[1].bytes.clear();
            }
            "setid" => entries[1].mode = 0o4755,
            "duplicate" => entries.push(entries[1].clone()),
            "link-escape" => entries[4].target = "../../../outside".into(),
            "unlisted" => entries.push(member("ngn/evil.py", b"unlisted")),
            "missing" => {
                entries.remove(1);
            }
            "hash" => entries[1].bytes.push(1),
            "pax-sparse" => {
                let mut pax = member("pax", b"25 GNU.sparse.size=1024\n");
                pax.kind = tar::EntryType::XHeader;
                entries.insert(1, pax);
            }
            _ => unreachable!(),
        }
        let archive = directory.path().join("malicious.tar.gz");
        fs::write(&archive, archive_bytes(&entries)).unwrap();
        let stage = crate::test_support::private_tempdir().unwrap();
        assert!(
            unpack(&archive, stage.path(), &CancellationToken::new()).is_err(),
            "accepted {attack}"
        );
        assert!(!directory.path().join("outside").exists());
    }
}

#[test]
fn manifest_and_expansion_limits_and_truncated_gzip_are_enforced() {
    let directory = crate::test_support::private_tempdir().unwrap();
    let mut entries = members(&metadata("0.100.2", false));
    entries.last_mut().unwrap().bytes = vec![b' '; MAX_MANIFEST as usize + 1];
    let archive = directory.path().join("huge-manifest.tar.gz");
    fs::write(&archive, archive_bytes(&entries)).unwrap();
    let stage = crate::test_support::private_tempdir().unwrap();
    assert!(unpack(&archive, stage.path(), &CancellationToken::new()).is_err());
    let mut header = tar::Header::new_gnu();
    header.set_mode(0o600);
    header.set_size(MAX_EXPANDED + 1);
    header.set_path("ngn/huge").unwrap();
    header.set_cksum();
    let mut gzip = GzEncoder::new(Vec::new(), Compression::fast());
    gzip.write_all(header.as_bytes()).unwrap();
    fs::write(&archive, gzip.finish().unwrap()).unwrap();
    let stage = crate::test_support::private_tempdir().unwrap();
    assert!(
        unpack(&archive, stage.path(), &CancellationToken::new())
            .unwrap_err()
            .to_string()
            .contains("1 GiB")
    );
    let mut bytes = archive_bytes(&members(&metadata("0.100.2", false)));
    bytes.truncate(bytes.len() - 5);
    fs::write(&archive, bytes).unwrap();
    let stage = crate::test_support::private_tempdir().unwrap();
    assert!(unpack(&archive, stage.path(), &CancellationToken::new()).is_err());
}

#[cfg(unix)]
#[test]
fn group_writable_parent_is_accepted_but_world_writable_is_refused() {
    use std::os::unix::fs::PermissionsExt;
    let directory = crate::test_support::private_tempdir().unwrap();
    // Like ~/.local/bin created under the Debian/Ubuntu default umask 0002.
    let bin = directory.path().join("bin");
    fs::create_dir(&bin).unwrap();
    fs::set_permissions(&bin, fs::Permissions::from_mode(0o775)).unwrap();
    assert!(Installation::prepare(directory.path().join("prefix"), bin.clone()).is_ok());
    fs::set_permissions(&bin, fs::Permissions::from_mode(0o777)).unwrap();
    assert!(ancestry(&bin, false).is_err());
}

#[test]
fn unsafe_parents_prefix_collision_binary_collision_and_stable_lock_are_refused() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let directory = crate::test_support::private_tempdir().unwrap();
    let unsafe_parent = directory.path().join("writable");
    fs::create_dir(&unsafe_parent).unwrap();
    fs::set_permissions(&unsafe_parent, fs::Permissions::from_mode(0o777)).unwrap();
    assert!(
        Installation::prepare(unsafe_parent.join("prefix"), directory.path().join("bin")).is_err()
    );
    assert!(!unsafe_parent.join("prefix").exists());
    symlink(directory.path(), directory.path().join("alias")).unwrap();
    assert!(
        Installation::prepare(
            directory.path().join("alias/prefix"),
            directory.path().join("bin")
        )
        .is_err()
    );
    let foreign = directory.path().join("foreign");
    ancestry(&foreign, true).unwrap();
    fs::write(foreign.join("sentinel"), b"foreign prefix").unwrap();
    assert!(Installation::prepare(foreign.clone(), directory.path().join("bin")).is_err());
    assert!(!foreign.join(".install.lock").exists());
    assert!(!foreign.join("install.json").exists());
    assert!(!directory.path().join("bin").exists());
    let bin = directory.path().join("python-bin");
    ancestry(&bin, true).unwrap();
    fs::write(bin.join("ngn"), b"Python entry point").unwrap();
    assert!(Installation::prepare(directory.path().join("new-prefix"), bin.clone()).is_err());
    assert_eq!(fs::read(bin.join("ngn")).unwrap(), b"Python entry point");
    let install = installation(directory.path());
    let second = Installation::prepare(
        install.identity.prefix.clone(),
        install.identity.bin_dir.clone(),
    )
    .err()
    .unwrap();
    assert!(second.to_string().contains("Another installer"));
    let lock_path = install.identity.prefix.join(".install.lock");
    use std::os::unix::fs::MetadataExt;
    let inode = lock_path.metadata().unwrap().ino();
    let prefix = install.identity.prefix.clone();
    let bin = install.identity.bin_dir.clone();
    drop(install);
    let _next = Installation::prepare(prefix, bin).unwrap();
    assert_eq!(lock_path.metadata().unwrap().ino(), inode);
}

#[cfg(unix)]
#[tokio::test]
async fn corrupt_current_identity_foreign_bin_and_existing_generation_fail_before_activation() {
    let directory = crate::test_support::private_tempdir().unwrap();
    let install = installation(directory.path());
    let first = local(directory.path(), &metadata("0.100.2", false), |_| {});
    let transport = FixtureTransport::default();
    let probe = SyntheticProbe::success();
    let cancel = CancellationToken::new();
    apply(&install, &first, false, &transport, &probe, &cancel)
        .await
        .unwrap();
    let next = local(directory.path(), &metadata("0.100.3", false), |_| {});
    ancestry(&install.identity.prefix.join("releases/0.100.3"), true).unwrap();
    fs::write(
        install.identity.prefix.join("releases/0.100.3/sentinel"),
        b"immutable orphan",
    )
    .unwrap();
    assert!(
        apply(&install, &next, false, &transport, &probe, &cancel)
            .await
            .is_err()
    );
    assert_eq!(
        current(&install.identity).unwrap().unwrap().to_string(),
        "0.100.2"
    );
    assert_eq!(
        fs::read(install.identity.prefix.join("releases/0.100.3/sentinel")).unwrap(),
        b"immutable orphan"
    );
    fs::remove_file(install.identity.prefix.join("current")).unwrap();
    std::os::unix::fs::symlink("../foreign", install.identity.prefix.join("current")).unwrap();
    assert!(current(&install.identity).is_err());
    assert!(
        apply(&install, &next, false, &transport, &probe, &cancel)
            .await
            .is_err()
    );
    fs::remove_file(install.identity.bin_dir.join("ngn")).unwrap();
    std::os::unix::fs::symlink("/usr/bin/false", install.identity.bin_dir.join("ngn")).unwrap();
    assert!(binary_link(&install.identity, true).is_err());
}

#[test]
fn https_redirect_allowlist_excludes_http_userinfo_ports_and_lookalike_hosts() {
    for url in [
        "http://github.com/asset",
        "https://github.com.evil.invalid/asset",
        "https://user:secret@github.com/asset",
        "https://github.com:444/asset",
        "https://evil.invalid/asset",
    ] {
        assert!(
            !allowed_url(&reqwest::Url::parse(url).unwrap()),
            "accepted {url}"
        );
    }
    for url in [
        API,
        DOWNLOAD,
        "https://release-assets.githubusercontent.com/asset?server-issued-signature=value",
        "https://objects.githubusercontent.com/asset",
    ] {
        assert!(allowed_url(&reqwest::Url::parse(url).unwrap()));
    }
}

#[cfg(unix)]
#[tokio::test]
async fn old_same_version_native_asset_is_rejected_before_the_python_probe() {
    use std::os::unix::fs::PermissionsExt;
    let directory = crate::test_support::private_tempdir().unwrap();
    let source = local(directory.path(), &metadata("0.100.2", false), |_| {});
    let stage = crate::test_support::private_tempdir().unwrap();
    let (root, mut manifest) = unpack(
        source.archive.as_ref().unwrap(),
        stage.path(),
        &CancellationToken::new(),
    )
    .unwrap();
    // A harmless old-CLI stand-in, not a Python/runtime fixture. The real native
    // installer itself is covered separately by the CLI integration tests.
    fs::write(
        root.join("ngn"),
        b"#!/bin/sh\nif [ \"$1\" = --version ]; then printf 'ngn 0.100.2\\n'; else exit 2; fi\n",
    )
    .unwrap();
    fs::set_permissions(root.join("ngn"), fs::Permissions::from_mode(0o700)).unwrap();
    manifest
        .files
        .insert("ngn".into(), file_hash(&root.join("ngn")).unwrap());
    fs::write(
        root.join("bundle.json"),
        serde_json::to_vec(&manifest).unwrap(),
    )
    .unwrap();
    manifest.verify_tree(&root).unwrap();
    let error = RuntimeProbe.verify(&root, &manifest).await.unwrap_err();
    assert!(
        error
            .to_string()
            .contains("lacks the required installer/update CLI")
    );
}

#[cfg(unix)]
#[test]
fn prefix_and_lock_permissions_are_never_repaired_or_followed() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let directory = crate::test_support::private_tempdir().unwrap();
    let prefix = directory.path().join("public-prefix");
    ancestry(&prefix, true).unwrap();
    fs::set_permissions(&prefix, fs::Permissions::from_mode(0o750)).unwrap();
    assert!(Installation::prepare(prefix.clone(), directory.path().join("bin")).is_err());
    assert_eq!(
        prefix.metadata().unwrap().permissions().mode() & 0o777,
        0o750
    );
    let prefix = directory.path().join("prefix");
    ancestry(&prefix, true).unwrap();
    let target = directory.path().join("outside-lock");
    fs::write(&target, b"untouched").unwrap();
    symlink(&target, prefix.join(".install.lock")).unwrap();
    assert!(Lock::acquire(&prefix, true).is_err());
    assert_eq!(fs::read(&target).unwrap(), b"untouched");
    fs::remove_file(prefix.join(".install.lock")).unwrap();
    fs::write(prefix.join(".install.lock"), b"").unwrap();
    fs::set_permissions(
        prefix.join(".install.lock"),
        fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    assert!(Lock::acquire(&prefix, true).is_err());
    assert_eq!(
        prefix
            .join(".install.lock")
            .metadata()
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o644
    );
}

#[tokio::test]
async fn cancellation_during_download_joins_without_activation_or_replacing_the_lock() {
    struct PausedTransport {
        fixture: FixtureTransport,
        entered: tokio::sync::Notify,
    }
    impl Transport for PausedTransport {
        async fn fetch(&self, url: &str, output: &mut File, limit: u64) -> Result<()> {
            if url.ends_with("bundle.tar.gz") {
                output.write_all(b"interrupted compressed bytes")?;
                self.entered.notify_one();
                std::future::pending().await
            } else {
                self.fixture.fetch(url, output, limit).await
            }
        }
    }
    let directory = crate::test_support::private_tempdir().unwrap();
    let install = installation(directory.path());
    let first = local(directory.path(), &metadata("0.100.2", false), |_| {});
    let probe = SyntheticProbe::success();
    apply(
        &install,
        &first,
        false,
        &FixtureTransport::default(),
        &probe,
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    let pointer = fs::read_link(install.identity.prefix.join("current")).unwrap();
    let transport = PausedTransport {
        fixture: network(&metadata("0.100.3", true)),
        entered: tokio::sync::Notify::new(),
    };
    let cancel = CancellationToken::new();
    let source = Source {
        version: None,
        archive: None,
        sha256: None,
    };
    let (result, ()) = tokio::join!(
        apply(&install, &source, false, &transport, &probe, &cancel),
        async {
            transport.entered.notified().await;
            cancel.cancel();
        },
    );
    assert!(result.is_err());
    assert_eq!(probe.calls.load(Ordering::SeqCst), 1);
    assert_eq!(
        fs::read_link(install.identity.prefix.join("current")).unwrap(),
        pointer
    );
    assert!(!install.identity.prefix.join("releases/0.100.3").exists());
    assert!(
        fs::read_dir(&install.identity.prefix)
            .unwrap()
            .all(|entry| !entry
                .unwrap()
                .file_name()
                .to_str()
                .unwrap()
                .starts_with(".stage-"))
    );
}

#[test]
fn pax_mtime_is_accepted_but_member_budget_and_hidden_trailing_archives_are_rejected() {
    fn pax_record(key: &str, value: &str) -> Vec<u8> {
        let body = format!("{key}={value}\n");
        let mut length = body.len() + 2;
        loop {
            let record = format!("{length} {body}");
            if record.len() == length {
                return record.into_bytes();
            }
            length = record.len();
        }
    }
    let directory = crate::test_support::private_tempdir().unwrap();
    let mut entries = members(&metadata("0.100.2", false));
    let mut pax = member("ngn/PaxHeader", &pax_record("mtime", "123.456"));
    pax.kind = tar::EntryType::XHeader;
    entries.insert(1, pax);
    let archive = directory.path().join("pax.tar.gz");
    fs::write(&archive, archive_bytes(&entries)).unwrap();
    let stage = crate::test_support::private_tempdir().unwrap();
    unpack(&archive, stage.path(), &CancellationToken::new()).unwrap();
    let mut many = Vec::new();
    for index in 0..=MAX_MEMBERS {
        let mut entry = member(&format!("ngn/dir-{index}"), b"");
        entry.kind = tar::EntryType::Directory;
        many.push(entry);
    }
    fs::write(&archive, archive_bytes(&many)).unwrap();
    let stage = crate::test_support::private_tempdir().unwrap();
    assert!(
        unpack(&archive, stage.path(), &CancellationToken::new())
            .unwrap_err()
            .to_string()
            .contains("member limit")
    );
    let bytes = archive_bytes(&entries);
    let mut concatenated = bytes.clone();
    concatenated.extend_from_slice(&bytes);
    fs::write(&archive, concatenated).unwrap();
    let stage = crate::test_support::private_tempdir().unwrap();
    assert!(
        unpack(&archive, stage.path(), &CancellationToken::new())
            .unwrap_err()
            .to_string()
            .contains("trailing archive")
    );
}

#[tokio::test]
async fn compressed_limit_invalid_pins_and_same_version_mutation_are_refused() {
    let directory = crate::test_support::private_tempdir().unwrap();
    let install = installation(directory.path());
    let transport = FixtureTransport::default();
    let probe = SyntheticProbe::success();
    let cancel = CancellationToken::new();
    let archive = directory.path().join("sparse-too-large.tar.gz");
    File::create(&archive)
        .unwrap()
        .set_len(MAX_COMPRESSED + 1)
        .unwrap();
    let mut source = Source {
        archive: Some(archive),
        sha256: Some("0".repeat(64)),
        version: None,
    };
    assert!(
        apply(&install, &source, false, &transport, &probe, &cancel)
            .await
            .unwrap_err()
            .to_string()
            .contains("256 MiB")
    );
    source.version = Some("0.28.5".into());
    assert!(
        apply(&install, &source, false, &transport, &probe, &cancel)
            .await
            .is_err()
    );
    let first = local(directory.path(), &metadata("0.100.2", false), |_| {});
    apply(&install, &first, false, &transport, &probe, &cancel)
        .await
        .unwrap();
    let mut modified = metadata("0.100.2", false);
    modified.source_commit = "c".repeat(40);
    let modified = local(directory.path(), &modified, |_| {});
    assert!(
        apply(&install, &modified, false, &transport, &probe, &cancel)
            .await
            .unwrap_err()
            .to_string()
            .contains("immutable version")
    );
    assert_eq!(probe.calls.load(Ordering::SeqCst), 1);
    assert!(transport.calls.lock().unwrap().is_empty());
}
