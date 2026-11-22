//! POSIX read-only discovery on a verified workspace directory.

use std::{
    collections::HashSet,
    ffi::{OsStr, OsString},
    io::Read,
    os::unix::ffi::OsStringExt,
    path::{Component, Path, PathBuf},
};

use anyhow::{Context, Result, bail, ensure};
use cap_std::{
    ambient_authority,
    fs::{Dir, Metadata, MetadataExt, OpenOptions, OpenOptionsExt},
};
use pyo3::{exceptions::PyValueError, prelude::*};

use crate::discovery::{self, Access, Scan};

type Identity = (u64, u64);
type Protected = (Vec<String>, Vec<Identity>);
type Names = (Vec<String>, Vec<String>, Vec<String>);
type Paths = (Vec<(String, bool)>, bool);
type Matches = (Vec<(String, usize, String)>, bool, usize, bool);
const MAX_SEARCH_BYTES: usize = 8 * 1024 * 1024;

struct Workspace {
    root: Dir,
    protected: Vec<PathBuf>,
    identities: HashSet<Identity>,
    names: HashSet<String>,
    prefixes: Vec<String>,
    suffixes: Vec<String>,
    max_file: usize,
}

fn identity(metadata: &Metadata) -> Identity {
    (metadata.dev(), metadata.ino())
}

fn open_options(flags: libc::c_int) -> OpenOptions {
    let mut options = OpenOptions::new();
    options
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | flags);
    options
}

impl Workspace {
    fn protected_path(&self, path: &Path) -> bool {
        self.protected.iter().any(|prefix| path.starts_with(prefix))
    }

    fn component_is_protected(&self, name: &str) -> bool {
        let lower = if !name.is_ascii() {
            Some(name.to_lowercase())
        } else if name.bytes().any(|byte| byte.is_ascii_uppercase()) {
            Some(name.to_ascii_lowercase())
        } else {
            None
        };
        let name = lower.as_deref().unwrap_or(name);
        self.names.contains(name)
            || self.prefixes.iter().any(|prefix| name.starts_with(prefix))
            || self.suffixes.iter().any(|suffix| name.ends_with(suffix))
    }

    fn open_child(&self, parent: &Dir, name: &OsStr) -> Result<Dir> {
        let child = parent.open_with(name, &open_options(libc::O_DIRECTORY))?;
        let metadata = child.metadata()?;
        ensure!(
            metadata.is_dir() && !self.identities.contains(&identity(&metadata)),
            "Protected or invalid workspace directory"
        );
        Ok(Dir::from_std_file(child.into_std()))
    }

    fn read(&self, path: &Path, limit: usize) -> Result<Vec<u8>> {
        let parent = self.directory(path.parent().context("Missing file parent")?)?;
        let name = path.file_name().context("Missing file name")?;
        let file = parent.open_with(name, &open_options(0))?.into_std();
        let metadata = file.metadata()?;
        use std::os::unix::fs::MetadataExt as _;
        ensure!(
            metadata.is_file()
                && metadata.nlink() == 1
                && !self.identities.contains(&(metadata.dev(), metadata.ino()))
                && metadata.len() <= limit as u64,
            "Unsafe or oversized workspace file"
        );
        let mut bytes = Vec::with_capacity(metadata.len() as usize);
        file.take(limit as u64 + 1).read_to_end(&mut bytes)?;
        ensure!(bytes.len() <= limit, "Oversized workspace file");
        Ok(bytes)
    }

    fn scan(&self, path: &str, scan: Scan) -> Result<Paths> {
        let (mut entries, truncated) = discovery::walk(self, path, scan)?;
        entries.sort_unstable_by(|left, right| left.0.cmp(&right.0));
        Ok((entries, truncated))
    }

    fn search(
        &self,
        path: &str,
        query: &str,
        limit: usize,
        max_output: usize,
        allow_file: bool,
    ) -> Result<Matches> {
        ensure!(
            !query.is_empty()
                && query.chars().count() <= 1000
                && (1..=1000).contains(&limit)
                && (1..=1024 * 1024).contains(&max_output),
            "Invalid workspace search limits"
        );
        let (paths, mut truncated) = self.scan(
            path,
            Scan {
                recursive: true,
                directories: false,
                allow_file,
            },
        )?;
        let included = !paths.is_empty();
        let mut results = Vec::new();
        let (mut read, mut size, mut skipped) = (0, 0, 0);
        'files: for (file, _) in paths {
            // The scanner already applied the protected loaned policy. The
            // second descriptor-relative open also rejects replacements
            // with links, special files or a known state directory.
            let Ok(bytes) = self.read(Path::new(&file), self.max_file) else {
                skipped += 1;
                continue;
            };
            read += bytes.len();
            if read > MAX_SEARCH_BYTES {
                truncated = true;
                break;
            }
            let Ok(text) = std::str::from_utf8(&bytes) else {
                skipped += 1;
                continue;
            };
            if bytes.contains(&0) {
                skipped += 1;
                continue;
            }
            let mut start = 0;
            let mut remaining = text;
            let mut number = 0;
            // Match Python's splitlines, including lone CR, combined CRLF
            // and Unicode line separators. Never include the terminators.
            while !remaining.is_empty() {
                let boundary = remaining.find(is_line_break).unwrap_or(remaining.len());
                let line = &remaining[..boundary];
                number += 1;
                if line.contains(query) {
                    let line_end = line
                        .char_indices()
                        .nth(1000)
                        .map_or(line.len(), |(index, _)| index);
                    let line = &line[..line_end];
                    let added = file.len() + number.to_string().len() + 3 + line.len() + 1;
                    if results.len() == limit || size + added > max_output {
                        truncated = true;
                        break 'files;
                    }
                    results.push((file.clone(), number, line.to_owned()));
                    size += added;
                }
                let tail = &remaining[boundary..];
                let advance = if tail.starts_with("\r\n") {
                    2
                } else {
                    tail.chars().next().map_or(0, char::len_utf8)
                };
                start += boundary + advance;
                remaining = &text[start..];
            }
        }
        Ok((results, truncated, skipped, included))
    }
}

fn is_line_break(character: char) -> bool {
    matches!(
        character,
        '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}'..='\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}'
    )
}

impl Access for Workspace {
    fn relative(&self, path: &str) -> Result<PathBuf> {
        ensure!(
            !path.is_empty()
                && path.len() <= 4096
                && !path.contains('\\')
                && !path.chars().any(char::is_control),
            "Invalid workspace discovery path"
        );
        let mut clean = PathBuf::new();
        for part in Path::new(path).components() {
            match part {
                Component::Normal(name) => {
                    ensure!(
                        !self.component_is_protected(name.to_str().context("Non-UTF-8 path")?),
                        "Protected workspace discovery path"
                    );
                    clean.push(name);
                }
                Component::CurDir => (),
                _ => bail!("Discovery path must remain inside the workspace"),
            }
        }
        ensure!(
            !self.protected_path(&clean),
            "Internal state is not accessible to discovery"
        );
        Ok(clean)
    }

    fn candidate(&self, path: &str, name: &str) -> bool {
        // Walk has already checked the ancestor components and pruned
        // protected names. Only a newly seen basename needs validation.
        path.len() <= 4096 && !name.contains('\\') && !name.chars().any(char::is_control)
    }

    fn directory(&self, path: &Path) -> Result<Dir> {
        let mut directory = self.root.try_clone()?;
        for part in path.components() {
            match part {
                Component::Normal(name) => directory = self.open_child(&directory, name)?,
                Component::CurDir => (),
                _ => bail!("Invalid discovery directory"),
            }
        }
        Ok(directory)
    }

    fn read_ignore(&self, path: &str) -> Result<Vec<u8>> {
        let path = self.relative(path)?;
        self.read(&path, self.max_file.min(discovery::MAX_IGNORE_FILE))
    }

    fn pruned(&self, name: &str) -> bool {
        discovery::dependency_directory(name) || self.component_is_protected(name)
    }

    fn permits(&self, path: &Path, metadata: &Metadata) -> bool {
        !self.protected_path(path) && !self.identities.contains(&identity(metadata))
    }
}

fn open(
    root: Vec<u8>,
    root_identity: Identity,
    protected: Protected,
    names: Names,
    max_file: usize,
) -> Result<Workspace> {
    let root = PathBuf::from(OsString::from_vec(root));
    let (paths, identities) = protected;
    let (names, prefixes, suffixes) = names;
    ensure!(
        root.is_absolute()
            && paths.len() <= 16
            && identities.len() <= 4096
            && names.len() <= 128
            && prefixes.len() <= 16
            && suffixes.len() <= 16
            && (1..=4 * 1024 * 1024).contains(&max_file),
        "Invalid workspace discovery policy"
    );
    // Python has already opened and authorized this root. Only use reopened
    // authority if it is still the exact directory, including after a rename.
    let root = Dir::open_ambient_dir(root, ambient_authority())?;
    ensure!(
        identity(&root.dir_metadata()?) == root_identity && !identities.contains(&root_identity),
        "Workspace directory changed or is protected"
    );
    Ok(Workspace {
        root,
        protected: paths.into_iter().map(PathBuf::from).collect(),
        identities: identities.into_iter().collect(),
        names: names.into_iter().collect(),
        prefixes,
        suffixes,
        max_file,
    })
}

#[pyfunction]
pub(super) fn workspace_paths(
    py: Python<'_>,
    root: Vec<u8>,
    root_identity: Identity,
    protected: Protected,
    names: Names,
    path: String,
    scan_policy: (bool, bool, usize),
) -> PyResult<Paths> {
    py.detach(move || {
        let (recursive, allow_file, max_file) = scan_policy;
        open(root, root_identity, protected, names, max_file)?.scan(
            &path,
            Scan {
                recursive,
                directories: true,
                allow_file,
            },
        )
    })
    .map_err(|error| PyValueError::new_err(error.to_string()))
}

#[pyfunction]
pub(super) fn workspace_search(
    py: Python<'_>,
    root: Vec<u8>,
    root_identity: Identity,
    protected: Protected,
    names: Names,
    path: String,
    policy: (String, usize, usize, usize, bool),
) -> PyResult<Matches> {
    py.detach(move || {
        let (query, limit, max_file, max_output, allow_file) = policy;
        open(root, root_identity, protected, names, max_file)?
            .search(&path, &query, limit, max_output, allow_file)
    })
    .map_err(|error| PyValueError::new_err(error.to_string()))
}
