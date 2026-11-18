//! Distribution metadata shared by the installer and version-pinned runtime loader.
//! SHA-256 inventories bind payloads to metadata, not to an independent signature.

use std::{
    collections::{BTreeMap, BTreeSet},
    fmt,
    fs::{self, File},
    io::{Read, Write},
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Deserializer, Serialize, de};
use sha2::{Digest, Sha256};

/// The validated native target this binary was built for. Each release ships
/// one bundle per platform; any other target can only build from source.
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub const PLATFORM: &str = "macos-arm64";
#[cfg(all(target_os = "macos", target_arch = "x86_64"))]
pub const PLATFORM: &str = "macos-x86_64";
#[cfg(not(any(
    all(target_os = "macos", target_arch = "aarch64"),
    all(target_os = "macos", target_arch = "x86_64")
)))]
pub const PLATFORM: &str = "linux-x86_64";
pub const MAX_MANIFEST: u64 = 8 * 1024 * 1024;
pub const MAX_COMPRESSED: u64 = 256 * 1024 * 1024;
pub const MAX_EXPANDED: u64 = 1024 * 1024 * 1024;
pub const MAX_MEMBERS: usize = 32_000;

/// Only the stable Rust release namespace; never Python releases or prereleases.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct ReleaseVersion(u32);

impl ReleaseVersion {
    pub fn parse(value: &str) -> Result<Self> {
        let patch = value
            .strip_prefix("0.100.")
            .context("Expected a stable Rust version 0.100.N (no prerelease or Python tag)")?;
        let number: u32 = patch.parse().context("Invalid Rust release version")?;
        ensure!(
            number.to_string() == patch,
            "Noncanonical Rust release version"
        );
        Ok(Self(number))
    }

    pub fn tag(value: &str) -> Result<Self> {
        Self::parse(value.strip_prefix('v').context("Not a Rust release tag")?)
    }
}

impl fmt::Display for ReleaseVersion {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(output, "0.100.{}", self.0)
    }
}

pub fn supported_platform() -> Result<()> {
    ensure!(
        cfg!(any(
            all(target_os = "linux", target_arch = "x86_64"),
            all(target_os = "macos", target_arch = "aarch64"),
            all(target_os = "macos", target_arch = "x86_64")
        )),
        "Bundled installation is validated only on Linux x86_64 and macOS (arm64, x86_64); use a source installation on this platform"
    );
    Ok(())
}

/// Linux keeps the original manifest name so earlier installers keep updating.
pub fn release_manifest_name() -> String {
    if PLATFORM == "linux-x86_64" {
        "release-manifest.json".into()
    } else {
        format!("release-manifest-{PLATFORM}.json")
    }
}

pub fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn package_version(value: &str) -> bool {
    let parts: Vec<_> = value.split('.').collect();
    parts.len() == 3
        && parts.iter().all(|part| {
            part.parse::<u32>()
                .is_ok_and(|number| number.to_string() == *part)
        })
}

/// Archive paths deliberately use a single portable spelling. Do not normalize
/// traversal into an apparently safe path, or permit Windows spellings on Unix.
pub fn relative_path(value: &str) -> Result<()> {
    ensure!(
        !value.is_empty()
            && value.len() <= 4096
            && !value
                .bytes()
                .any(|byte| byte < 32 || byte == 127 || b"\\:".contains(&byte))
            && value
                .split('/')
                .all(|part| !matches!(part, "" | "." | "..")),
        "Invalid relative bundle path"
    );
    Ok(())
}

pub fn unique_map<'de, D>(
    deserializer: D,
) -> std::result::Result<BTreeMap<String, String>, D::Error>
where
    D: Deserializer<'de>,
{
    struct Visitor;
    impl<'de> de::Visitor<'de> for Visitor {
        type Value = BTreeMap<String, String>;
        fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("a map without duplicate keys")
        }
        fn visit_map<A: de::MapAccess<'de>>(
            self,
            mut map: A,
        ) -> std::result::Result<Self::Value, A::Error> {
            let mut result = BTreeMap::new();
            while let Some((key, value)) = map.next_entry::<String, String>()? {
                if result.insert(key, value).is_some() {
                    return Err(de::Error::custom("Duplicate inventory key"));
                }
                if result.len() > MAX_MEMBERS {
                    return Err(de::Error::custom("Inventory member limit exceeded"));
                }
            }
            Ok(result)
        }
    }
    deserializer.deserialize_map(Visitor)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PythonBundle {
    pub implementation: String,
    pub version: String,
    pub artifact: String,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BundleManifest {
    pub bundle_schema: u16,
    pub ngn_version: String,
    pub nagents_version: String,
    pub adapter_version: String,
    pub runtime_protocol: u16,
    pub platform: String,
    pub source_commit: String,
    pub official: bool,
    pub python: PythonBundle,
    pub runner: Vec<String>,
    pub ca_file: String,
    #[serde(deserialize_with = "unique_map")]
    pub files: BTreeMap<String, String>,
    #[serde(deserialize_with = "unique_map")]
    pub symlinks: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseManifest {
    pub schema_version: u16,
    pub commit: String,
    pub ngn_version: String,
    pub nagents_version: String,
    pub runtime_protocol: u16,
    pub platform: String,
    pub bundle: String,
    #[serde(deserialize_with = "unique_map")]
    pub artifacts: BTreeMap<String, String>,
}

impl ReleaseManifest {
    pub fn validate(&self, version: ReleaseVersion) -> Result<&str> {
        ensure!(
            self.schema_version == 2,
            "Release has no schema-2 self-contained bundle; choose a newer Rust release"
        );
        ensure!(
            self.ngn_version == version.to_string(),
            "Release version mismatch"
        );
        ensure!(
            self.platform == PLATFORM && self.runtime_protocol == ngn_protocol::VERSION,
            "Release platform/protocol mismatch"
        );
        ensure!(
            hex(&self.commit, 40) && package_version(&self.nagents_version),
            "Invalid release provenance"
        );
        ensure!(
            self.bundle == format!("ngn-v{version}-{PLATFORM}-bundle.tar.gz"),
            "Unexpected bundle asset name"
        );
        for (name, hash) in &self.artifacts {
            relative_path(name)?;
            ensure!(
                !name.contains('/')
                    && name != "SHA256SUMS"
                    && !(name.starts_with("release-manifest") && name.ends_with(".json"))
                    && hex(hash, 64),
                "Invalid release artifact inventory"
            );
        }
        self.artifacts
            .get(&self.bundle)
            .map(String::as_str)
            .context("Release bundle checksum is missing")
    }
}

impl BundleManifest {
    pub fn read(root: &Path) -> Result<Self> {
        let path = root.join("bundle.json");
        ensure!(
            path.symlink_metadata()?.is_file()
                && !path.symlink_metadata()?.file_type().is_symlink(),
            "bundle.json must be a regular file"
        );
        let mut bytes = Vec::new();
        File::open(path)?
            .take(MAX_MANIFEST + 1)
            .read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() as u64 <= MAX_MANIFEST,
            "Bundle manifest exceeds 8 MiB"
        );
        let result: Self = serde_json::from_slice(&bytes).context("Invalid bundle manifest")?;
        result.validate()?;
        Ok(result)
    }

    pub fn validate(&self) -> Result<()> {
        ensure!(self.bundle_schema == 1, "Unsupported bundle schema");
        ReleaseVersion::parse(&self.ngn_version)?;
        ensure!(
            self.adapter_version == self.ngn_version && package_version(&self.nagents_version),
            "Bundle component version mismatch"
        );
        ensure!(
            self.runtime_protocol == ngn_protocol::VERSION && self.platform == PLATFORM,
            "Bundle platform/protocol mismatch"
        );
        ensure!(hex(&self.source_commit, 40), "Invalid bundle source commit");
        ensure!(
            self.python.implementation == "cpython"
                && self.python.version.starts_with("3.13.")
                && package_version(&self.python.version)
                && hex(&self.python.sha256, 64),
            "Expected pinned CPython 3.13.x provenance"
        );
        relative_path(&self.python.artifact)?;
        ensure!(
            !self.python.artifact.contains('/'),
            "Invalid Python artifact filename"
        );
        ensure!(
            self.runner == ["runtime/bin/python3", "-I", "-B", "-m", "ngn_nagents"],
            "Unsupported bundled runner argv"
        );
        relative_path(&self.ca_file)?;
        ensure!(
            self.ca_file == "runtime/lib/python3.13/site-packages/certifi/cacert.pem",
            "Unexpected bundled CA path"
        );
        ensure!(
            self.files.len() + self.symlinks.len() < MAX_MEMBERS,
            "Bundle member limit exceeded"
        );
        for (name, hash) in &self.files {
            relative_path(name)?;
            ensure!(
                name != "bundle.json" && hex(hash, 64) && !self.symlinks.contains_key(name),
                "Invalid file inventory"
            );
        }
        for (name, target) in &self.symlinks {
            relative_path(name)?;
            // CPython install-only links, not arbitrary application/plugin links.
            let cpython_link = matches!(
                name.as_str(),
                "runtime/bin/python"
                    | "runtime/bin/python3"
                    | "runtime/bin/python3-config"
                    | "runtime/bin/idle3"
                    | "runtime/bin/pydoc3"
                    | "runtime/lib/libpython3.13.so"
                    | "runtime/lib/libpython3.so"
                    | "runtime/lib/pkgconfig/python3.pc"
                    | "runtime/lib/pkgconfig/python3-embed.pc"
                    | "runtime/lib64"
                    | "runtime/share/man/man1/python.1"
                    | "runtime/share/man/man1/python3.1"
            ) || (name.starts_with("runtime/lib/python3.13/config-3.13-")
                && name.ends_with("/libpython3.13.a"))
                || (name.starts_with("runtime/share/terminfo/") && name.split('/').count() == 5);
            ensure!(cpython_link, "Only internal CPython symlinks are permitted");
            let destination = self.link_destination(name, target)?;
            let expected = match name.as_str() {
                "runtime/bin/python" => matches!(
                    destination.to_str(),
                    Some("runtime/bin/python3" | "runtime/bin/python3.13")
                ),
                "runtime/bin/python3" => destination == Path::new("runtime/bin/python3.13"),
                "runtime/bin/python3-config" => {
                    destination == Path::new("runtime/bin/python3.13-config")
                }
                "runtime/bin/idle3" => destination == Path::new("runtime/bin/idle3.13"),
                "runtime/bin/pydoc3" => destination == Path::new("runtime/bin/pydoc3.13"),
                "runtime/lib/libpython3.13.so" | "runtime/lib/libpython3.so" => matches!(
                    destination.to_str(),
                    Some("runtime/lib/libpython3.13.so.1.0" | "runtime/lib/libpython3.13.so")
                ),
                "runtime/lib/pkgconfig/python3.pc" => {
                    destination == Path::new("runtime/lib/pkgconfig/python-3.13.pc")
                }
                "runtime/lib/pkgconfig/python3-embed.pc" => {
                    destination == Path::new("runtime/lib/pkgconfig/python-3.13-embed.pc")
                }
                "runtime/lib64" => destination == Path::new("runtime/lib"),
                "runtime/share/man/man1/python.1" | "runtime/share/man/man1/python3.1" => {
                    destination == Path::new("runtime/share/man/man1/python3.13.1")
                }
                _ if name.starts_with("runtime/share/terminfo/") => {
                    destination.starts_with("runtime/share/terminfo")
                        && destination.components().count() == 5
                }
                _ => {
                    name.ends_with("/libpython3.13.a")
                        && destination == Path::new("runtime/lib/libpython3.13.a")
                }
            };
            ensure!(
                expected,
                "Symlink target is not a CPython install-only link"
            );
        }
        for name in self.files.keys().chain(self.symlinks.keys()) {
            let mut ancestor = Path::new(name).parent();
            while let Some(path) = ancestor {
                ensure!(
                    !self.symlinks.contains_key(path.to_str().unwrap_or(""))
                        && !self.files.contains_key(path.to_str().unwrap_or("")),
                    "Bundle member has a non-directory parent"
                );
                ancestor = path.parent();
            }
        }
        ensure!(
            self.files.contains_key("ngn") && self.files.contains_key(&self.ca_file),
            "Missing native executable or CA inventory"
        );
        ensure!(
            self.files.contains_key(&self.runner[0]) || self.symlinks.contains_key(&self.runner[0]),
            "Missing Python runner inventory"
        );
        Ok(())
    }

    fn link_destination(&self, name: &str, target: &str) -> Result<PathBuf> {
        ensure!(
            !target.is_empty()
                && target.len() <= 4096
                && !target.starts_with('/')
                && !target
                    .bytes()
                    .any(|byte| byte < 32 || byte == 127 || b"\\:".contains(&byte)),
            "Invalid CPython symlink target"
        );
        let mut parts: Vec<_> = name.split('/').collect();
        parts.pop();
        for part in target.split('/') {
            match part {
                ".." => {
                    ensure!(parts.len() > 1, "CPython symlink escapes runtime");
                    parts.pop();
                }
                "" | "." => anyhow::bail!("Noncanonical CPython symlink target"),
                _ => parts.push(part),
            }
        }
        ensure!(
            parts.first() == Some(&"runtime") && parts.len() > 1,
            "CPython symlink escapes runtime"
        );
        Ok(parts.iter().collect())
    }

    /// Verify the exact inventory without following links while walking or reading.
    /// Called before *any* newly installed executable or interpreter is launched.
    pub fn verify_tree(&self, root: &Path) -> Result<()> {
        self.validate()?;
        let mut files = BTreeSet::new();
        let mut links = BTreeSet::new();
        let mut directories = vec![root.to_path_buf()];
        let mut count = 0;
        let mut expanded: u64 = 0;
        while let Some(directory) = directories.pop() {
            for entry in fs::read_dir(directory)? {
                let entry = entry?;
                let path = entry.path();
                count += 1;
                ensure!(count <= MAX_MEMBERS, "Bundle member limit exceeded");
                let info = path.symlink_metadata()?;
                let name = path
                    .strip_prefix(root)?
                    .to_str()
                    .context("Non-UTF-8 bundle path")?
                    .to_owned();
                relative_path(&name)?;
                if info.is_dir() {
                    directories.push(path);
                } else if info.file_type().is_symlink() {
                    let target = self
                        .symlinks
                        .get(&name)
                        .context("Unlisted bundle symlink")?;
                    ensure!(
                        fs::read_link(&path)? == Path::new(target),
                        "Bundle symlink target mismatch"
                    );
                    links.insert(name);
                } else {
                    ensure!(info.is_file(), "Unsupported bundle member type");
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::MetadataExt;
                        ensure!(
                            info.mode() & 0o6000 == 0 && info.nlink() == 1,
                            "Set-id or hardlinked bundle file"
                        );
                    }
                    expanded = expanded
                        .checked_add(info.len())
                        .context("Expanded bundle exceeds 1 GiB")?;
                    ensure!(expanded <= MAX_EXPANDED, "Expanded bundle exceeds 1 GiB");
                    if name == "bundle.json" {
                        continue;
                    }
                    let expected = self.files.get(&name).context("Unlisted bundle file")?;
                    ensure!(
                        file_hash(&path)? == *expected,
                        "Bundle file checksum mismatch"
                    );
                    files.insert(name);
                }
            }
        }
        ensure!(
            files == self.files.keys().cloned().collect()
                && links == self.symlinks.keys().cloned().collect(),
            "Missing bundle inventory members"
        );
        let runtime = root.join("runtime").canonicalize()?;
        for name in self.symlinks.keys() {
            let destination = root
                .join(name)
                .canonicalize()
                .context("Broken or cyclic CPython symlink")?;
            ensure!(
                destination.starts_with(&runtime),
                "CPython symlink escapes runtime"
            );
        }
        Ok(())
    }
}

pub fn file_hash(path: &Path) -> Result<String> {
    let mut hash = Sha256::new();
    std::io::copy(&mut File::open(path)?, &mut hash_writer(&mut hash))?;
    Ok(format!("{:x}", hash.finalize()))
}

fn hash_writer(hash: &mut Sha256) -> impl Write + '_ {
    struct Writer<'a>(&'a mut Sha256);
    impl Write for Writer<'_> {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0.update(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    Writer(hash)
}

/// Metadata is private to memory (not part of the trusted operator config wire).
#[derive(Debug, Clone)]
pub struct BundledRuntime {
    root: PathBuf,
    manifest: BundleManifest,
}

impl BundledRuntime {
    pub fn detect(executable: &Path) -> Result<Option<Self>> {
        let executable = executable
            .canonicalize()
            .context("Cannot resolve native executable")?;
        let root = executable
            .parent()
            .context("Native executable has no parent")?;
        match root.join("bundle.json").symlink_metadata() {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error).context("Cannot inspect bundled runtime metadata"),
            Ok(_) => (),
        }
        supported_platform()?;
        let manifest = BundleManifest::read(root)?;
        ensure!(
            manifest.ngn_version == env!("CARGO_PKG_VERSION"),
            "Bundled native version mismatch"
        );
        manifest.verify_tree(root)?;
        Ok(Some(Self {
            root: root.to_path_buf(),
            manifest,
        }))
    }

    pub fn runner(&self) -> Vec<String> {
        // -I ignores PYTHON* settings, so -B is necessary to keep the verified
        // generation immutable even when the runtime is writable by its owner.
        vec![
            self.python().to_string_lossy().into_owned(),
            "-I".into(),
            "-B".into(),
            "-m".into(),
            "ngn_nagents".into(),
        ]
    }
    pub fn python(&self) -> PathBuf {
        self.root.join(&self.manifest.runner[0])
    }
    pub fn python_version(&self) -> &str {
        &self.manifest.python.version
    }
    pub fn ca_file(&self) -> PathBuf {
        self.root.join(&self.manifest.ca_file)
    }

    pub fn environment(&self, command: &mut tokio::process::Command) {
        self.environment_with_ca_settings(
            command,
            std::env::var_os("SSL_CERT_FILE").is_some(),
            std::env::var_os("SSL_CERT_DIR").is_some(),
        );
    }

    fn environment_with_ca_settings(
        &self,
        command: &mut tokio::process::Command,
        cert_file: bool,
        cert_dir: bool,
    ) {
        command.env_remove("PYTHONPATH").env_remove("PYTHONHOME");
        if !cert_file && !cert_dir {
            command.env("SSL_CERT_FILE", self.ca_file());
        }
    }
}

#[cfg(test)]
#[path = "bundle_tests.rs"]
pub(crate) mod tests;
