//! Native, deliberately isolated installation. No Config, Host, auth or workspace.
//!
//! install.json is immutable installation identity, NOT active-version metadata.
//! `current` is the sole generation commit point; there is no two-file transaction
//! to tear on interruption. Unreferenced releases are retained, never overwritten.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{IsTerminal, Read, Seek, Write},
    path::{Component, Path, PathBuf},
    process::Stdio,
    time::Duration,
};

use anyhow::{Context, Result, bail, ensure};
use clap::Args;
use flate2::read::MultiGzDecoder;
use fs2::FileExt;
use ngn_core::bundle::{
    BundleManifest, MAX_COMPRESSED, MAX_EXPANDED, MAX_MANIFEST, MAX_MEMBERS, ReleaseManifest,
    ReleaseVersion, file_hash, hex, relative_path, release_manifest_name, supported_platform,
};
use ngn_core::paths::{trusted_system_alias, without_system_aliases};
use serde::{Deserialize, Serialize};
use tempfile::TempDir;
use tokio::{io::AsyncReadExt, process::Command};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

const API: &str = "https://api.github.com/repos/abi-jey/nagents/releases";
const DOWNLOAD: &str = "https://github.com/abi-jey/nagents/releases/download";
const UNMANAGED: &str = "This binary is not the current executable of a managed ngn installation. Run the native `ngn install --prefix NEW_PREFIX --bin-dir NEW_BIN_DIR` installer; source, Python and package-manager executables are never self-replaced.";

#[derive(Debug, Args)]
pub(crate) struct Source {
    /// Stable Rust version 0.100.N. Omit to discover the newest stable Rust release.
    #[arg(long)]
    version: Option<String>,
    /// Explicit local development/acceptance bundle (not a network endpoint).
    #[arg(long = "bundle", alias = "archive", requires = "sha256")]
    archive: Option<PathBuf>,
    /// Expected lowercase SHA-256, supplied by the caller for --bundle.
    #[arg(long, requires = "archive")]
    sha256: Option<String>,
}

#[derive(Debug, Args)]
pub(crate) struct Install {
    #[command(flatten)]
    source: Source,
    /// Defaults to $XDG_DATA_HOME/ngn-harness or $HOME/.local/share/ngn-harness.
    #[arg(long)]
    prefix: Option<PathBuf>,
    /// Stable ngn symlink directory; defaults to $HOME/.local/bin.
    #[arg(long)]
    bin_dir: Option<PathBuf>,
}

#[derive(Debug, Args)]
pub(crate) struct Update {
    #[command(flatten)]
    source: Source,
    /// Check availability without downloading or executing a network payload.
    #[arg(long)]
    check: bool,
}

/// No public test endpoint or trust override. Tests provide this private trait.
trait Transport {
    async fn fetch(&self, url: &str, output: &mut File, limit: u64) -> Result<()>;
}

struct Https(reqwest::Client);

fn allowed_url(url: &reqwest::Url) -> bool {
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.port_or_known_default() == Some(443)
        && matches!(
            url.host_str(),
            Some(
                "api.github.com"
                    | "github.com"
                    | "release-assets.githubusercontent.com"
                    | "objects.githubusercontent.com"
            )
        )
}

impl Https {
    fn new() -> Result<Self> {
        let redirects = reqwest::redirect::Policy::custom(|attempt| {
            if attempt.previous().len() >= 8 || !allowed_url(attempt.url()) {
                attempt.error("Untrusted release redirect")
            } else {
                attempt.follow()
            }
        });
        Ok(Self(
            reqwest::Client::builder()
                .https_only(true)
                .redirect(redirects)
                .connect_timeout(Duration::from_secs(15))
                .timeout(Duration::from_secs(600))
                .user_agent(concat!("ngn-installer/", env!("CARGO_PKG_VERSION")))
                .build()
                .context("Cannot initialize the Rustls HTTPS installer")?,
        ))
    }
}

impl Transport for Https {
    async fn fetch(&self, url: &str, output: &mut File, limit: u64) -> Result<()> {
        ensure!(
            allowed_url(&reqwest::Url::parse(url)?),
            "Untrusted release URL"
        );
        let mut response = self
            .0
            .get(url)
            .send()
            .await
            .map_err(|error| error.without_url())
            .context("Official HTTPS release request failed")?
            .error_for_status()
            .map_err(|error| error.without_url())
            .context(
                "Official release asset is unavailable; select a released schema-2 Rust bundle",
            )?;
        ensure!(
            response
                .content_length()
                .is_none_or(|length| length <= limit),
            "Release response exceeds its byte limit"
        );
        let mut total = 0;
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|error| error.without_url())
            .context("Release download was interrupted")?
        {
            total += chunk.len() as u64;
            ensure!(total <= limit, "Release response exceeds its byte limit");
            output.write_all(&chunk)?;
        }
        Ok(())
    }
}

#[derive(Deserialize)]
struct GitHubRelease {
    tag_name: String,
    draft: bool,
    prerelease: bool,
}

async fn json<T: Transport, D: serde::de::DeserializeOwned>(transport: &T, url: &str) -> Result<D> {
    let mut file = tempfile::tempfile()?;
    transport.fetch(url, &mut file, MAX_MANIFEST).await?;
    ensure!(
        file.metadata()?.len() <= MAX_MANIFEST,
        "Release manifest exceeds 8 MiB"
    );
    file.rewind()?;
    serde_json::from_reader(file).context("Invalid official release metadata")
}

async fn discover<T: Transport>(
    transport: &T,
    pin: Option<ReleaseVersion>,
) -> Result<ReleaseManifest> {
    let version = if let Some(version) = pin {
        let release: GitHubRelease = json(transport, &format!("{API}/tags/v{version}")).await?;
        ensure!(
            !release.draft
                && !release.prerelease
                && ReleaseVersion::tag(&release.tag_name)? == version,
            "Not the requested stable Rust release"
        );
        version
    } else {
        let mut newest = None;
        for page in 1..=10 {
            let releases: Vec<GitHubRelease> =
                json(transport, &format!("{API}?per_page=100&page={page}")).await?;
            for release in &releases {
                if !release.draft
                    && !release.prerelease
                    && let Ok(version) = ReleaseVersion::tag(&release.tag_name)
                {
                    newest = Some(newest.map_or(version, |old: ReleaseVersion| old.max(version)));
                }
            }
            if releases.len() < 100 {
                break;
            }
            ensure!(
                page < 10,
                "Release discovery limit reached; use --version 0.100.N"
            );
        }
        newest
            .context("No stable Rust v0.100.x release found (Python tags are not Rust releases)")?
    };
    let manifest: ReleaseManifest = json(
        transport,
        &format!("{DOWNLOAD}/v{version}/{}", release_manifest_name()),
    )
    .await?;
    manifest.validate(version)?;
    Ok(manifest)
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    install_schema: u16,
    installation_id: String,
    prefix: PathBuf,
    bin_dir: PathBuf,
}

/// Each missing ancestor is private at creation, including .local/share etc.
/// Never canonicalize away a symlink or chmod an operator's existing directory.
fn ancestry(path: &Path, create: bool) -> Result<()> {
    ensure!(path.is_absolute(), "Installation paths must be absolute");
    let mut current = PathBuf::new();
    let last = path.components().count() - 1;
    for (index, component) in path.components().enumerate() {
        ensure!(
            !matches!(component, Component::ParentDir | Component::CurDir),
            "Installation path contains traversal"
        );
        current.push(component);
        if create {
            let mut builder = fs::DirBuilder::new();
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                builder.mode(0o700);
            }
            match builder.create(&current) {
                Ok(()) => (),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
                Err(error) => {
                    return Err(error).context("Cannot create a private installation ancestor");
                }
            }
        }
        let mut info = current
            .symlink_metadata()
            .context("Cannot inspect installation ancestry")?;
        if index < last
            && let Some(target) = trusted_system_alias(&current, &info)
        {
            // e.g. macOS /var -> /private/var; only root can create or replace it.
            current = target;
            info = current
                .symlink_metadata()
                .context("Cannot inspect installation ancestry")?;
        }
        ensure!(
            info.is_dir() && !info.file_type().is_symlink(),
            "Installation ancestors must be real directories, not symlinks"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let root_sticky = info.uid() == 0 && info.mode() & 0o1000 != 0 && index != last;
            let euid = nix::unistd::geteuid().as_raw();
            // Group-writable parents (e.g. ~/.local/bin under umask 0002) are
            // accepted; only world-writable or foreign-owned ones are refused.
            ensure!(
                (info.uid() == 0 || info.uid() == euid)
                    && (info.mode() & 0o002 == 0 || root_sticky),
                "Unsafe installation ancestry: a parent directory is world-writable or owned by another user"
            );
        }
    }
    Ok(())
}

fn private_directory(path: &Path) -> Result<()> {
    ancestry(path, false)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let info = path.symlink_metadata()?;
        ensure!(
            info.uid() == nix::unistd::geteuid().as_raw() && info.mode() & 0o777 == 0o700,
            "Managed installation directories must be owner-private (700); choose a new prefix, not chmod repair"
        );
    }
    Ok(())
}

fn existing_ancestry(path: &Path) -> Result<bool> {
    ensure!(
        path.is_absolute()
            && !path
                .components()
                .any(|part| matches!(part, Component::ParentDir | Component::CurDir)),
        "Installation paths must be absolute without traversal"
    );
    let mut ancestor = path;
    loop {
        match ancestor.symlink_metadata() {
            Ok(_) => {
                ancestry(ancestor, false)?;
                return Ok(ancestor == path);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                ancestor = ancestor.parent().context("Missing installation ancestry")?
            }
            Err(error) => return Err(error).context("Cannot inspect installation ancestry"),
        }
    }
}

fn unmanaged_empty(prefix: &Path) -> Result<()> {
    ensure!(
        fs::read_dir(prefix)?
            .all(|entry| entry.is_ok_and(|entry| entry.file_name() == ".install.lock")),
        "Refusing an unmanaged nonempty prefix; choose --prefix NEW_PREFIX"
    );
    Ok(())
}

fn regular(path: &Path, file: &File) -> Result<()> {
    let named = path.symlink_metadata()?;
    let info = file.metadata()?;
    ensure!(
        info.is_file() && named.is_file() && !named.file_type().is_symlink(),
        "Installation metadata must be a regular file"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        ensure!(
            info.uid() == nix::unistd::geteuid().as_raw()
                && info.mode() & 0o777 == 0o600
                && info.nlink() == 1
                && (info.dev(), info.ino()) == (named.dev(), named.ino()),
            "Installation metadata must be private (600), singly linked and stable"
        );
    }
    Ok(())
}

fn open_private(path: &Path, create: bool) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true).write(create).create(create);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK);
    }
    let file = options
        .open(path)
        .context("Cannot open private installation metadata")?;
    regular(path, &file)?;
    Ok(file)
}

struct Lock(File);

impl Lock {
    fn acquire(prefix: &Path, create: bool) -> Result<Self> {
        let path = prefix.join(".install.lock");
        let file = open_private(&path, create)?;
        ensure!(
            file.metadata()?.len() == 0,
            "Installation lock must be empty"
        );
        FileExt::try_lock_exclusive(&file)
            .context("Another installer/update owns this prefix; wait and retry")?;
        regular(&path, &file)?;
        Ok(Self(file))
    }
}

impl Drop for Lock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.0);
    }
}

fn read_identity(prefix: &Path) -> Result<Identity> {
    let file = open_private(&prefix.join("install.json"), false)?;
    ensure!(
        file.metadata()?.len() <= 16 * 1024,
        "Invalid installation identity size"
    );
    let identity: Identity =
        serde_json::from_reader(file).context("Invalid managed installation identity")?;
    ensure!(
        identity.install_schema == 1
            && identity.prefix == prefix
            && Uuid::parse_str(&identity.installation_id)?.to_string() == identity.installation_id,
        "Managed installation identity mismatch"
    );
    ancestry(&identity.bin_dir, false)?;
    ensure!(
        !identity.bin_dir.starts_with(prefix) && !prefix.starts_with(&identity.bin_dir),
        "Prefix and bin-dir must be separate directories"
    );
    Ok(identity)
}

fn sync_directory(path: &Path) -> Result<()> {
    File::open(path)?
        .sync_all()
        .context("Cannot sync installation directory")
}

fn private_tempdir_in(parent: &Path, prefix: &str) -> Result<TempDir> {
    let mut builder = tempfile::Builder::new();
    builder.prefix(prefix);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(fs::Permissions::from_mode(0o700));
    }
    Ok(builder.tempdir_in(parent)?)
}

fn write_identity(identity: &Identity) -> Result<()> {
    let mut temporary = tempfile::NamedTempFile::new_in(&identity.prefix)?;
    serde_json::to_writer_pretty(&mut temporary, identity)?;
    temporary.write_all(b"\n")?;
    temporary.as_file().sync_all()?;
    temporary
        .persist_noclobber(identity.prefix.join("install.json"))
        .context("Cannot commit installation identity")?;
    sync_directory(&identity.prefix)
}

fn foreign_binary(link: &Path) -> anyhow::Error {
    anyhow::anyhow!(
        concat!(
            "Refusing to replace the existing unmanaged entry at {:?}. It was left unchanged.\n",
            "To install alongside it, choose an unused bin directory and a separate unused prefix, for example:\n",
            "  curl -fsSL {}/v{}/install.sh | sh -s -- \\\n",
            "    --prefix \"$HOME/.local/share/ngn-native\" --bin-dir \"$HOME/.local/ngn-native-bin\"\n",
            "Then run the new binary directly: \"$HOME/.local/ngn-native-bin/ngn\""
        ),
        link,
        DOWNLOAD,
        env!("CARGO_PKG_VERSION")
    )
}

fn require_unused_binary(bin_dir: &Path) -> Result<()> {
    let link = bin_dir.join("ngn");
    match link.symlink_metadata() {
        Ok(_) => Err(foreign_binary(&link)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => bail!("Cannot inspect ngn destination {link:?}: {error}"),
    }
}

fn binary_link(identity: &Identity, required: bool) -> Result<bool> {
    let link = identity.bin_dir.join("ngn");
    match link.symlink_metadata() {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound && !required => Ok(false),
        Ok(info) if info.file_type().is_symlink() => {
            ensure!(
                fs::read_link(&link)? == identity.prefix.join("current/ngn"),
                foreign_binary(&link)
            );
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                ensure!(
                    info.uid() == nix::unistd::geteuid().as_raw(),
                    "Foreign ngn symlink owner"
                );
            }
            Ok(true)
        }
        Ok(_) => Err(foreign_binary(&link)),
        Err(error) => bail!("Cannot inspect ngn destination {link:?}: {error}"),
    }
}

fn current(identity: &Identity) -> Result<Option<ReleaseVersion>> {
    let pointer = identity.prefix.join("current");
    match pointer.symlink_metadata() {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Ok(info) => {
            ensure!(
                info.file_type().is_symlink(),
                "Managed current must be a relative release symlink"
            );
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                ensure!(
                    info.uid() == nix::unistd::geteuid().as_raw(),
                    "Foreign current pointer owner"
                );
            }
        }
        Err(error) => return Err(error.into()),
    }
    let target = fs::read_link(pointer)?;
    let name = target
        .to_str()
        .context("Invalid current target")?
        .strip_prefix("releases/")
        .context("Invalid current target")?;
    let version = ReleaseVersion::parse(name)?;
    ensure!(
        target == Path::new("releases").join(version.to_string()),
        "Invalid current target"
    );
    private_directory(&identity.prefix.join("releases"))?;
    private_directory(&identity.prefix.join(&target))?;
    Ok(Some(version))
}

struct Installation {
    identity: Identity,
    _lock: Lock,
}

impl Installation {
    fn prepare(prefix: PathBuf, bin_dir: PathBuf) -> Result<Self> {
        // Record locations as the running executable will later see them.
        let (prefix, bin_dir) = (
            without_system_aliases(&prefix),
            without_system_aliases(&bin_dir),
        );
        ensure!(
            !prefix.starts_with(&bin_dir) && !bin_dir.starts_with(&prefix),
            "Prefix and bin-dir must be separate directories"
        );
        // Preflight both destinations before creating *anything*. An explicit
        // foreign/legacy nonempty prefix must not even receive a lock inode.
        let existing = existing_ancestry(&prefix)?;
        existing_ancestry(&bin_dir)?;
        let has_identity = existing && prefix.join("install.json").symlink_metadata().is_ok();
        if existing {
            private_directory(&prefix)?;
            if has_identity {
                let identity = read_identity(&prefix)?;
                ensure!(
                    identity.bin_dir == bin_dir,
                    "This prefix belongs to a different bin-dir; use its recorded bin-dir or a new prefix"
                );
                binary_link(&identity, false)?;
            } else {
                unmanaged_empty(&prefix)?;
            }
        }
        if !has_identity {
            require_unused_binary(&bin_dir)?;
        }
        ancestry(&prefix, true)?;
        ancestry(&bin_dir, true)?;
        private_directory(&prefix)?;
        let lock = Lock::acquire(&prefix, true)?;
        let identity = if prefix.join("install.json").symlink_metadata().is_ok() {
            let identity = read_identity(&prefix)?;
            ensure!(
                identity.bin_dir == bin_dir,
                "This prefix belongs to a different bin-dir; use its recorded bin-dir or a new prefix"
            );
            binary_link(&identity, false)?;
            identity
        } else {
            unmanaged_empty(&prefix)?;
            require_unused_binary(&bin_dir)?;
            let identity = Identity {
                install_schema: 1,
                installation_id: Uuid::new_v4().to_string(),
                prefix,
                bin_dir,
            };
            write_identity(&identity)?;
            identity
        };
        ancestry(&identity.prefix.join("releases"), true)?;
        private_directory(&identity.prefix.join("releases"))?;
        current(&identity)?;
        Ok(Self {
            identity,
            _lock: lock,
        })
    }

    fn from_executable(executable: &Path, progress: &mut Progress) -> Result<Self> {
        let executable = executable.canonicalize().context(UNMANAGED)?;
        let release = executable.parent().context(UNMANAGED)?;
        let releases = release.parent().context(UNMANAGED)?;
        let prefix = releases.parent().context(UNMANAGED)?;
        ensure!(
            executable.file_name().is_some_and(|name| name == "ngn")
                && releases.file_name().is_some_and(|name| name == "releases"),
            UNMANAGED
        );
        private_directory(prefix).context(UNMANAGED)?;
        let lock = Lock::acquire(prefix, false)?;
        let identity = read_identity(prefix).context(UNMANAGED)?;
        binary_link(&identity, true)?;
        let version = current(&identity)?.context(UNMANAGED)?;
        ensure!(
            release == prefix.join("releases").join(version.to_string())
                && version.to_string() == env!("CARGO_PKG_VERSION"),
            UNMANAGED
        );
        progress.phase("Inspecting installed bundle...");
        let bundle = BundleManifest::read(release)?;
        ensure!(
            bundle.ngn_version == version.to_string(),
            "Installed bundle version mismatch"
        );
        bundle.verify_tree(release)?;
        Ok(Self {
            identity,
            _lock: lock,
        })
    }

    fn installed_bundle(&self) -> Result<Option<BundleManifest>> {
        current(&self.identity)?
            .map(|version| {
                let root = self
                    .identity
                    .prefix
                    .join("releases")
                    .join(version.to_string());
                let manifest = BundleManifest::read(&root)?;
                ensure!(
                    manifest.ngn_version == version.to_string(),
                    "Installed bundle version mismatch"
                );
                manifest.verify_tree(&root)?;
                Ok(manifest)
            })
            .transpose()
    }
}

fn symlink(target: &Path, link: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(target, link)?;
        Ok(())
    }
    #[cfg(not(unix))]
    {
        let _ = (target, link);
        bail!("Bundled installation requires a Unix platform")
    }
}

fn cancelled(cancel: &CancellationToken) -> Result<()> {
    ensure!(
        !cancel.is_cancelled(),
        "Installation cancelled before activation"
    );
    Ok(())
}

fn copy_bounded(reader: impl Read, output: &mut File, limit: u64) -> Result<()> {
    let count = std::io::copy(&mut reader.take(limit + 1), output)?;
    ensure!(count <= limit, "Archive exceeds its byte limit");
    Ok(())
}

/// Preflight raw headers before tar's GNU/PAX interpreter can allocate long-name
/// records. A bounded, CRC-checked spool also rejects concatenated hidden archives.
fn unpack(
    archive: &Path,
    stage: &Path,
    cancel: &CancellationToken,
) -> Result<(PathBuf, BundleManifest)> {
    cancelled(cancel)?;
    let mut spool = tempfile::tempfile_in(stage)?;
    copy_bounded(
        MultiGzDecoder::new(File::open(archive)?),
        &mut spool,
        MAX_EXPANDED + MAX_MEMBERS as u64 * 2048 + MAX_MANIFEST,
    )?;
    spool.rewind()?;
    let mut raw = tar::Archive::new(&mut spool);
    let mut count = 0;
    let mut expanded: u64 = 0;
    for entry in raw.entries()?.raw(true) {
        cancelled(cancel)?;
        let mut entry = entry?;
        count += 1;
        ensure!(count <= MAX_MEMBERS, "Archive member limit exceeded");
        let kind = entry.header().entry_type();
        let size = entry.size();
        ensure!(
            entry.header().mode()? & 0o7000 == 0,
            "Set-id/sticky archive member is forbidden"
        );
        if kind.is_pax_local_extensions() || kind.is_gnu_longname() || kind.is_gnu_longlink() {
            ensure!(size <= MAX_MANIFEST, "Archive extension exceeds 8 MiB");
            if kind.is_pax_local_extensions()
                && let Some(extensions) = entry.pax_extensions()?
            {
                for extension in extensions {
                    let extension = extension?;
                    ensure!(
                        matches!(
                            extension.key()?,
                            "path"
                                | "linkpath"
                                | "mtime"
                                | "atime"
                                | "ctime"
                                | "uid"
                                | "gid"
                                | "uname"
                                | "gname"
                                | "size"
                        ),
                        "Unsupported PAX metadata (sparse files/xattrs are forbidden)"
                    );
                }
            }
        } else {
            ensure!(
                kind.is_file() || kind.is_dir() || kind.is_symlink(),
                "Hardlinks, devices, FIFOs and unsupported archive extensions are forbidden"
            );
            ensure!(
                kind.is_file() || size == 0,
                "Non-file archive member has data"
            );
            expanded = expanded
                .checked_add(size)
                .context("Expanded archive exceeds 1 GiB")?;
            ensure!(expanded <= MAX_EXPANDED, "Expanded archive exceeds 1 GiB");
        }
        std::io::copy(&mut entry, &mut std::io::sink())?;
    }
    let mut tail = [0; 8192];
    loop {
        let size = spool.read(&mut tail)?;
        if size == 0 {
            break;
        }
        ensure!(
            tail[..size].iter().all(|byte| *byte == 0),
            "Unexpected trailing archive data"
        );
    }
    spool.rewind()?;
    let root = stage.join("ngn");
    let mut archive = tar::Archive::new(spool);
    let mut seen = BTreeSet::new();
    let mut links = BTreeMap::new();
    let mut expanded: u64 = 0;
    for entry in archive.entries()? {
        cancelled(cancel)?;
        let mut entry = entry?;
        let kind = entry.header().entry_type();
        expanded = expanded
            .checked_add(entry.size())
            .context("Expanded archive exceeds 1 GiB")?;
        ensure!(expanded <= MAX_EXPANDED, "Expanded archive exceeds 1 GiB");
        let bytes = entry.path_bytes();
        let raw = std::str::from_utf8(&bytes).context("Non-UTF-8 archive member")?;
        let name = if kind.is_dir() {
            raw.strip_suffix('/').unwrap_or(raw)
        } else {
            raw
        };
        relative_path(name)?;
        ensure!(seen.insert(name.to_owned()), "Duplicate archive member");
        ensure!(
            name == "ngn" || name.starts_with("ngn/"),
            "Archive must have exactly one ngn/ root"
        );
        ensure!(
            name != "ngn" || kind.is_dir(),
            "Archive root must be a directory"
        );
        let path = stage.join(name);
        if kind.is_dir() {
            ancestry(&path, true)?;
        } else if kind.is_file() {
            ancestry(path.parent().context("Missing member parent")?, true)?;
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options
                    .mode(if entry.header().mode()? & 0o111 != 0 {
                        0o700
                    } else {
                        0o600
                    })
                    .custom_flags(nix::libc::O_NOFOLLOW);
            }
            let mut file = options.open(&path).context("Archive file collision")?;
            std::io::copy(&mut entry, &mut file)?;
            file.sync_all()?;
        } else {
            ensure!(kind.is_symlink(), "Unsupported interpreted archive member");
            let target = entry.link_name_bytes().context("Missing symlink target")?;
            let target = std::str::from_utf8(&target).context("Non-UTF-8 symlink target")?;
            links.insert(
                name.strip_prefix("ngn/")
                    .context("Invalid symlink root")?
                    .to_owned(),
                target.to_owned(),
            );
        }
    }
    let manifest = BundleManifest::read(&root)?;
    ensure!(
        links == manifest.symlinks,
        "Archive symlink inventory mismatch"
    );
    for (name, target) in links {
        let path = root.join(name);
        ancestry(path.parent().context("Missing symlink parent")?, true)?;
        symlink(Path::new(&target), &path)?;
    }
    manifest.verify_tree(&root)?;
    Ok((root, manifest))
}

trait Probe {
    async fn verify(&self, root: &Path, manifest: &BundleManifest) -> Result<()>;
}

struct RuntimeProbe;

// The same bounded-child ownership pattern as the runtime, without involving a
// Host/auth identity. A dropped/cancelled probe must not leave its process group.
struct ProbeGroup(Option<u32>);

impl Drop for ProbeGroup {
    fn drop(&mut self) {
        #[cfg(not(unix))]
        let _ = self.0;
        #[cfg(unix)]
        if let Some(id) = self.0.and_then(|id| i32::try_from(id).ok()) {
            let _ = nix::sys::signal::killpg(
                nix::unistd::Pid::from_raw(id),
                nix::sys::signal::Signal::SIGKILL,
            );
        }
    }
}

async fn output(mut command: Command) -> Result<Vec<u8>> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    #[cfg(unix)]
    command.process_group(0);
    let mut child = command
        .spawn()
        .context("Verified bundle executable cannot start on this system")?;
    let _group = ProbeGroup(child.id());
    let mut stdout = child
        .stdout
        .take()
        .context("No validation output")?
        .take(8193);
    let result = tokio::time::timeout(Duration::from_secs(30), async {
        let mut bytes = Vec::new();
        stdout.read_to_end(&mut bytes).await?;
        ensure!(
            bytes.len() <= 8192,
            "Bundle validation output exceeded its limit"
        );
        ensure!(
            child.wait().await?.success(),
            "Bundle validation probe failed; no payload was activated"
        );
        Ok(bytes)
    })
    .await
    .context("Bundle validation timed out; no payload was activated")?;
    if result.is_err() {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
    result
}

impl Probe for RuntimeProbe {
    async fn verify(&self, root: &Path, manifest: &BundleManifest) -> Result<()> {
        let home = private_tempdir_in(root.parent().context("Missing staging parent")?, ".probe-")?;
        let command = |executable: &Path| {
            let mut command = Command::new(executable);
            command
                .env_clear()
                .current_dir(home.path())
                .env("HOME", home.path())
                .env("TMPDIR", home.path())
                .env("XDG_DATA_HOME", home.path())
                .env("XDG_CONFIG_HOME", home.path())
                .env("XDG_CACHE_HOME", home.path());
            for name in ["SSL_CERT_FILE", "SSL_CERT_DIR"] {
                if let Some(value) = std::env::var_os(name) {
                    command.env(name, value);
                }
            }
            if std::env::var_os("SSL_CERT_FILE").is_none()
                && std::env::var_os("SSL_CERT_DIR").is_none()
            {
                command.env("SSL_CERT_FILE", root.join(&manifest.ca_file));
            }
            command
        };
        let mut native = command(&root.join("ngn"));
        native.arg("--version");
        ensure!(
            output(native).await? == format!("ngn {}\n", manifest.ngn_version).as_bytes(),
            "Native bundle version probe mismatch"
        );
        for operation in ["install", "update"] {
            let mut native = command(&root.join("ngn"));
            native.args([operation, "--help"]);
            output(native).await.context("Bundle native binary lacks the required installer/update CLI; use the matching new bundled release, not an older same-version native asset")?;
        }
        let mut python = command(&root.join(&manifest.runner[0]));
        python.args(["-I", "-B", "-c", r#"
import importlib.metadata as m, json, pathlib, platform, sys
import nagents, ngn_nagents
from nagents import Agent
from ngn_nagents.auth import available
from ngn_nagents.protocol import VERSION
r = pathlib.Path(sys.argv[1]).resolve()
assert all(pathlib.Path(x.__file__).resolve().is_relative_to(r) for x in (nagents, ngn_nagents))
assert pathlib.Path(sys.base_prefix).resolve().is_relative_to(r)
print(json.dumps([platform.python_implementation().lower(), platform.python_version(), m.version('nagents'), m.version('ngn-nagents'), available(), VERSION]))
"#]).arg(root.join("runtime"));
        let result: (String, String, String, String, bool, u16) =
            serde_json::from_slice(&output(python).await?)
                .context("Invalid bundled runtime validation output")?;
        ensure!(
            result.0 == "cpython"
                && result.1 == manifest.python.version
                && result.2 == manifest.nagents_version
                && result.3 == manifest.adapter_version,
            "Bundled Python/core/adapter version mismatch"
        );
        ensure!(
            result.5 == manifest.runtime_protocol,
            "Bundled runtime protocol probe mismatch"
        );
        ensure!(
            !manifest.official || result.4,
            "Official bundle lacks the published public device-auth API; official publication/install remains blocked"
        );
        // Imports must not have mutated the generation (not even __pycache__).
        manifest.verify_tree(root)
    }
}

#[derive(Debug, PartialEq, Eq)]
enum Outcome {
    Installed(ReleaseVersion),
    Current(ReleaseVersion),
    Available(ReleaseVersion),
}

impl std::fmt::Display for Outcome {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Installed(version) => write!(
                output,
                "Installed ngn {version}. Previous releases were retained; running hosts are unchanged."
            ),
            Self::Current(version) => write!(output, "ngn {version} is already current."),
            Self::Available(version) => write!(
                output,
                "ngn {version} is available. Run ngn update to install it."
            ),
        }
    }
}

/// Best-effort stderr feedback; a broken progress stream must not affect activation.
#[derive(Default)]
struct Progress {
    output: Option<Box<dyn Write + Send>>,
    color: bool,
    animate: bool,
}

impl Progress {
    fn stderr(no_animations: bool) -> Self {
        let capable = std::env::var_os("TERM").as_deref() != Some("dumb".as_ref());
        let color = std::env::var_os("NO_COLOR").is_none_or(|value| value.is_empty());
        // The installer never loads host/TUI configuration.
        let animate = crate::animations_enabled(
            no_animations,
            None,
            std::env::var("TEXTUAL_ANIMATIONS").ok().as_deref(),
        );
        Self::new(std::io::stderr(), capable && color, capable && animate)
    }

    fn new(output: impl Write + IsTerminal + Send + 'static, color: bool, animate: bool) -> Self {
        let tty = output.is_terminal();
        Self {
            output: Some(Box::new(output)),
            color: tty && color,
            animate: tty && animate,
        }
    }

    fn write(&mut self, text: std::fmt::Arguments<'_>) {
        if let Some(output) = &mut self.output {
            let _ = output.write_fmt(text).and_then(|()| output.flush());
        }
    }

    fn phase(&mut self, text: &str) {
        let prefix = if self.color {
            "\x1b[1;36mngn update:\x1b[0m"
        } else {
            "ngn update:"
        };
        self.write(format_args!("{prefix} {text}\n"));
    }

    async fn wait<T>(&mut self, work: impl Future<Output = T>) -> T {
        if !self.animate {
            return work.await;
        }
        let mut spinner = Spinner {
            progress: self,
            visible: false,
        };
        let mut tick = tokio::time::interval_at(
            tokio::time::Instant::now() + Duration::from_millis(250),
            Duration::from_millis(100),
        );
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        tokio::pin!(work);
        let frames = ['|', '/', '-', '\\'];
        let mut frame = 0;
        loop {
            tokio::select! {
                biased;
                result = &mut work => return result,
                _ = tick.tick() => {
                    spinner.visible = true;
                    spinner.progress.write(format_args!("\r{}", frames[frame]));
                    frame = (frame + 1) % frames.len();
                }
            }
        }
    }
}

struct Spinner<'a> {
    progress: &'a mut Progress,
    visible: bool,
}

impl Drop for Spinner<'_> {
    fn drop(&mut self) {
        // Only the single transient cell below the phase line is redrawn.
        if self.visible {
            self.progress.write(format_args!("\r \r"));
        }
    }
}

fn compare(version: ReleaseVersion, installed: Option<&BundleManifest>) -> Result<()> {
    if let Some(old) = installed {
        ensure!(
            version >= ReleaseVersion::parse(&old.ngn_version)?,
            "Downgrades are refused; previous immutable releases were retained but are not automatically activated"
        );
    }
    Ok(())
}

async fn apply<T: Transport, P: Probe>(
    installation: &Installation,
    source: &Source,
    check: bool,
    transport: &T,
    probe: &P,
    cancel: &CancellationToken,
    mut progress: Progress,
) -> Result<Outcome> {
    cancelled(cancel)?;
    let pin = source
        .version
        .as_deref()
        .map(ReleaseVersion::parse)
        .transpose()?;
    ensure!(
        source.archive.is_some() == source.sha256.is_some(),
        "Local archives require an explicit caller-supplied --sha256"
    );
    if let Some(hash) = &source.sha256 {
        ensure!(hex(hash, 64), "Expected a lowercase 64-character SHA-256");
    }
    let installed = installation.installed_bundle()?;
    if let Some(pin) = pin {
        compare(pin, installed.as_ref())?;
    }
    let release = if source.archive.is_none() {
        progress.phase("Checking release metadata...");
        Some(tokio::select! {
            result = progress.wait(discover(transport, pin)) => result?,
            _ = cancel.cancelled() => bail!("Installation cancelled before activation"),
        })
    } else {
        None
    };
    if let Some(release) = &release {
        let version = ReleaseVersion::parse(&release.ngn_version)?;
        compare(version, installed.as_ref())?;
        if let Some(old) = &installed
            && old.ngn_version == release.ngn_version
        {
            ensure!(
                old.official
                    && old.source_commit == release.commit
                    && old.nagents_version == release.nagents_version,
                "Installed generation differs from the official release provenance; versions are never overwritten"
            );
            return Ok(Outcome::Current(version));
        }
        if check {
            return Ok(Outcome::Available(version));
        }
    }
    let stage = private_tempdir_in(&installation.identity.prefix, ".stage-")?;
    let mut archive = tempfile::NamedTempFile::new_in(stage.path())?;
    let expected = if let Some(path) = &source.archive {
        progress.phase("Reading local bundle...");
        ensure!(
            File::open(path)?.metadata()?.len() <= MAX_COMPRESSED,
            "Compressed archive exceeds 256 MiB"
        );
        copy_bounded(File::open(path)?, archive.as_file_mut(), MAX_COMPRESSED)?;
        source.sha256.as_deref().context("Missing local checksum")?
    } else {
        let release = release.as_ref().context("Missing release")?;
        let url = format!("{DOWNLOAD}/v{}/{}", release.ngn_version, release.bundle);
        progress.phase("Downloading bundle...");
        tokio::select! {
            result = progress.wait(transport.fetch(&url, archive.as_file_mut(), MAX_COMPRESSED)) => result?,
            _ = cancel.cancelled() => bail!("Installation cancelled before activation"),
        }
        release.validate(ReleaseVersion::parse(&release.ngn_version)?)?
    };
    progress.phase("Verifying and staging bundle...");
    ensure!(
        archive.as_file().metadata()?.len() <= MAX_COMPRESSED
            && file_hash(archive.path())? == expected,
        "Bundle archive SHA-256 mismatch; no payload was extracted or activated"
    );
    cancelled(cancel)?;
    let archive_path = archive.path().to_path_buf();
    let stage_path = stage.path().to_path_buf();
    let token = cancel.clone();
    let (root, bundle) = progress
        .wait(tokio::task::spawn_blocking(move || {
            unpack(&archive_path, &stage_path, &token)
        }))
        .await??;
    let version = ReleaseVersion::parse(&bundle.ngn_version)?;
    ensure!(
        pin.is_none_or(|pin| pin == version),
        "Local bundle does not match --version"
    );
    compare(version, installed.as_ref())?;
    if let Some(release) = &release {
        ensure!(
            bundle.official
                && bundle.source_commit == release.commit
                && bundle.ngn_version == release.ngn_version
                && bundle.nagents_version == release.nagents_version,
            "Official release/bundle provenance mismatch (unofficial bundles require explicit local --bundle and --sha256)"
        );
    }
    if let Some(old) = &installed
        && old.ngn_version == bundle.ngn_version
    {
        ensure!(
            serde_json::to_value(old)? == serde_json::to_value(&bundle)?,
            "A different bundle already owns this immutable version"
        );
        return Ok(Outcome::Current(version));
    }
    if check {
        return Ok(Outcome::Available(version));
    }
    cancelled(cancel)?;
    tokio::select! {
        result = progress.wait(probe.verify(&root, &bundle)) => result?,
        _ = cancel.cancelled() => bail!("Installation cancelled before activation"),
    }
    cancelled(cancel)?;
    sync_tree(&root, cancel)?;
    progress.phase("Activating release...");
    activate(installation, &root, &bundle, &stage, cancel)?;
    Ok(Outcome::Installed(version))
}

fn sync_tree(root: &Path, cancel: &CancellationToken) -> Result<()> {
    let mut directories = vec![root.to_path_buf()];
    let mut index = 0;
    while index < directories.len() {
        cancelled(cancel)?;
        for entry in fs::read_dir(&directories[index])? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                directories.push(entry.path());
            }
        }
        index += 1;
    }
    for directory in directories.into_iter().rev() {
        cancelled(cancel)?;
        sync_directory(&directory)?;
    }
    Ok(())
}

fn activate(
    installation: &Installation,
    root: &Path,
    bundle: &BundleManifest,
    stage: &TempDir,
    cancel: &CancellationToken,
) -> Result<()> {
    let identity = &installation.identity;
    private_directory(&identity.prefix)?;
    ancestry(&identity.bin_dir, false)?;
    let releases = identity.prefix.join("releases");
    private_directory(&releases)?;
    let destination = releases.join(&bundle.ngn_version);
    ensure!(
        destination
            .symlink_metadata()
            .is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound),
        "Release version directory already exists; immutable releases are never overwritten. Use a new release version/prefix."
    );
    let had_binary = binary_link(identity, false)?;
    current(identity)?;
    cancelled(cancel)?;
    // No fallible payload checks after this point. An unreferenced generation is
    // safe to retain on a filesystem/sync failure, and is never executed.
    fs::rename(root, &destination).context("Cannot retain the verified release")?;
    sync_directory(&releases)?;
    if !had_binary {
        symlink(
            &identity.prefix.join("current/ngn"),
            &identity.bin_dir.join("ngn"),
        )?;
        sync_directory(&identity.bin_dir)?;
    }
    let pointer = stage.path().join("current");
    symlink(&Path::new("releases").join(&bundle.ngn_version), &pointer)?;
    sync_directory(stage.path())?;
    cancelled(cancel)?;
    fs::rename(pointer, identity.prefix.join("current"))
        .context("Cannot atomically activate verified release")?;
    // The rename is the commit. A later fsync failure must not be reported as a
    // failed update or roll back a generation another process may already use.
    if sync_directory(&identity.prefix).is_err() {
        eprintln!(
            "ngn: activated successfully, but directory durability could not be confirmed; inspect the managed current pointer after a system crash."
        );
    }
    Ok(())
}

pub(crate) async fn install(arguments: &Install, cancel: &CancellationToken) -> Result<()> {
    supported_platform()?;
    let home = || {
        std::env::var_os("HOME")
            .map(PathBuf::from)
            .context("HOME is unset; supply --prefix and --bin-dir")
    };
    let prefix = match &arguments.prefix {
        Some(path) => std::path::absolute(path)?,
        None => match std::env::var_os("XDG_DATA_HOME") {
            Some(base) => PathBuf::from(base).join("ngn-harness"),
            None => home()?.join(".local/share/ngn-harness"),
        },
    };
    let bin_dir = match &arguments.bin_dir {
        Some(path) => std::path::absolute(path)?,
        None => home()?.join(".local/bin"),
    };
    let installation = Installation::prepare(prefix, bin_dir)?;
    let result = apply(
        &installation,
        &arguments.source,
        false,
        &Https::new()?,
        &RuntimeProbe,
        cancel,
        Progress::default(),
    )
    .await?;
    println!(
        "{result}\nExecutable: {}",
        installation.identity.bin_dir.join("ngn").display()
    );
    Ok(())
}

pub(crate) async fn update(
    arguments: &Update,
    cancel: &CancellationToken,
    no_animations: bool,
) -> Result<()> {
    supported_platform()?;
    let mut progress = Progress::stderr(no_animations);
    let installation = Installation::from_executable(&std::env::current_exe()?, &mut progress)?;
    let result = apply(
        &installation,
        &arguments.source,
        arguments.check,
        &Https::new()?,
        &RuntimeProbe,
        cancel,
        progress,
    )
    .await?;
    println!("{result}");
    Ok(())
}

#[cfg(test)]
#[path = "install_tests.rs"]
mod tests;
