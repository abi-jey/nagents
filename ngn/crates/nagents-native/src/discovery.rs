//! Workspace-only discovery with case-sensitive `ignore::gitignore` matching.
//!
//! Only `.gitignore` in the workspace root and visited ancestor/child directories
//! is supported. Each file's rules are relative to its containing directory;
//! anchoring, directory-only patterns, wildcards (including `**`), escapes,
//! comments, UTF-8 BOMs and last-match `!` negation follow the matcher. Deeper
//! files take precedence over ancestors. Excluded directories are pruned before
//! reading their rules, so re-including a descendant requires re-including its
//! parents first. Explicit starting directories/files undergo the same checks
//! from the workspace root. Built-in build, credential and protected-state
//! exclusions cannot be negated. This is not complete legacy-tool parity.
//!
//! There is no `.ignore`, `.git/info/exclude`, parent-of-workspace/global/user
//! Git configuration, Git subprocess, or automatic project-instruction loading.
//! Listings return paths only; search reads ordinary candidate files, without
//! attaching instructions or changing prompts. Pattern text is read with the
//! existing capability-relative, bounded, single-link/no-follow file policy;
//! the matcher's ambient file-reading APIs are never used. Missing `.gitignore`
//! is allowed; other unsafe/unreadable/invalid files abort the entire discovery
//! without returning partial results or pattern contents.
//!
//! Per discovery: at most MAX_PATHS entries/explicit path components are examined
//! (then results are marked truncated), 256 ignore files, 256 KiB of rule text,
//! and 2048 rule-file lines. Each ignore file is at most 64 KiB and each line at
//! most 1024 bytes, before compilation; matcher compilation errors fail closed.
//! Paths remain subject to the tool's 4096-byte policy. The caller also bounds
//! file reads, cumulative search bytes, result count and serialized output.

use std::{io::ErrorKind, path::Path, sync::Arc};

use anyhow::{Result, bail, ensure};
use cap_std::fs::{Dir, Metadata};
use ignore::{Match, gitignore::Gitignore, gitignore::GitignoreBuilder};

pub const MAX_PATHS: usize = 5000;
pub const MAX_IGNORE_FILE: usize = 64 * 1024;
const MAX_IGNORE_BYTES: usize = 256 * 1024;
pub const MAX_IGNORE_FILES: usize = 256;
pub const MAX_IGNORE_LINES: usize = 2048;
pub const MAX_IGNORE_LINE: usize = 1024;

#[derive(Default)]
struct Budget {
    paths: usize,
    files: usize,
    bytes: usize,
    lines: usize,
}

impl Budget {
    fn visit(&mut self) -> bool {
        self.paths += 1;
        self.paths <= MAX_PATHS
    }
}

#[derive(Clone, Default)]
struct Rules(Option<Arc<RuleSet>>);

struct RuleSet {
    matcher: Gitignore,
    parent: Rules,
}

/// Filesystem authority and path policy are supplied by the engine, not the matcher.
pub trait Access {
    fn relative(&self, path: &str) -> Result<std::path::PathBuf>;
    fn candidate(&self, path: &str, _name: &str) -> bool {
        self.relative(path).is_ok()
    }
    fn directory(&self, path: &Path) -> Result<Dir>;
    fn read_ignore(&self, path: &str) -> Result<Vec<u8>>;
    fn pruned(&self, name: &str) -> bool;
    fn permits(&self, _path: &Path, _metadata: &Metadata) -> bool {
        true
    }
}

impl Rules {
    fn in_directory(
        self,
        tools: &impl Access,
        directory: &Path,
        budget: &mut Budget,
    ) -> Result<Self> {
        let path = directory.join(".gitignore");
        let path = path.to_str().expect("validated UTF-8 workspace path");
        let bytes = match tools.read_ignore(path) {
            Ok(bytes) => bytes,
            Err(error)
                if error
                    .downcast_ref::<std::io::Error>()
                    .is_some_and(|error| error.kind() == ErrorKind::NotFound) =>
            {
                return Ok(self);
            }
            Err(_) => bail!("Cannot safely read workspace ignore file: {path}"),
        };
        budget.files += 1;
        budget.bytes += bytes.len();
        ensure!(
            bytes.len() <= MAX_IGNORE_FILE
                && budget.files <= MAX_IGNORE_FILES
                && budget.bytes <= MAX_IGNORE_BYTES,
            "Workspace ignore file/byte limit exceeded"
        );
        let text = std::str::from_utf8(&bytes)
            .map_err(|_| anyhow::anyhow!("Workspace ignore file is not UTF-8: {path}"))?;
        ensure!(
            !text.contains('\0'),
            "Invalid workspace ignore file: {path}"
        );
        budget.lines += text.lines().count();
        ensure!(
            budget.lines <= MAX_IGNORE_LINES,
            "Workspace ignore line-count limit exceeded"
        );
        let mut builder = GitignoreBuilder::new(directory);
        for (index, line) in text.lines().enumerate() {
            let line = if index == 0 {
                line.trim_start_matches('\u{feff}')
            } else {
                line
            };
            ensure!(
                line.len() <= MAX_IGNORE_LINE,
                "Workspace ignore line-length limit exceeded"
            );
            builder
                .add_line(None, line)
                .map_err(|_| anyhow::anyhow!("Invalid workspace ignore pattern: {path}"))?;
        }
        let matcher = builder
            .build()
            .map_err(|_| anyhow::anyhow!("Cannot compile workspace ignore patterns: {path}"))?;
        if matcher.is_empty() {
            return Ok(self);
        }
        Ok(Self(Some(Arc::new(RuleSet {
            matcher,
            parent: self,
        }))))
    }

    fn ignores(&self, path: &Path, is_directory: bool) -> bool {
        let mut rules = self.0.as_deref();
        while let Some(current) = rules {
            match current.matcher.matched(path, is_directory) {
                Match::Ignore(_) => return true,
                Match::Whitelist(_) => return false,
                Match::None => rules = current.parent.0.as_deref(),
            }
        }
        false
    }
}

pub fn dependency_directory(name: &str) -> bool {
    [
        "node_modules",
        "target",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    ]
    .contains(&name)
}

fn single_link_file(metadata: &Metadata) -> bool {
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use cap_std::fs::MetadataExt;
        if metadata.nlink() != 1 {
            return false;
        }
    }
    true
}

#[derive(Clone, Copy)]
pub struct Scan {
    pub recursive: bool,
    pub directories: bool,
    pub allow_file: bool,
}

pub fn walk(tools: &impl Access, path: &str, scan: Scan) -> Result<(Vec<(String, bool)>, bool)> {
    let path = tools.relative(path)?;
    let mut directory = std::path::PathBuf::from(".");
    let mut rules = Rules::default();
    let mut budget = Budget::default();

    // Check each ancestor before reading the next directory's rules. In
    // particular, a child's negation must not bypass an excluded parent.
    for component in path.components() {
        let std::path::Component::Normal(name) = component else {
            continue;
        };
        if !budget.visit() {
            return Ok((Vec::new(), true));
        }
        rules = rules.in_directory(tools, &directory, &mut budget)?;
        if tools.pruned(name.to_str().expect("validated UTF-8 workspace path")) {
            return Ok((Vec::new(), false));
        }
        let child = directory.join(name);
        let metadata = tools.directory(&directory)?.symlink_metadata(name)?;
        if metadata.file_type().is_symlink()
            || !tools.permits(&child, &metadata)
            || rules.ignores(&child, metadata.is_dir())
        {
            return Ok((Vec::new(), false));
        }
        if metadata.is_dir() {
            directory = child;
        } else {
            ensure!(
                scan.allow_file && child.strip_prefix(".").unwrap_or(&child) == path,
                "Expected a workspace directory"
            );
            let files = if single_link_file(&metadata) {
                vec![(path.to_str().unwrap().to_owned(), false)]
            } else {
                Vec::new()
            };
            return Ok((files, false));
        }
    }

    let mut pending = vec![(directory, rules)];
    let mut files = Vec::new();
    while let Some((directory, inherited)) = pending.pop() {
        let rules = inherited.in_directory(tools, &directory, &mut budget)?;
        let opened = tools.directory(&directory)?;
        for entry in opened.entries()? {
            if !budget.visit() {
                return Ok((files, true));
            }
            let entry = entry?;
            let name = entry.file_name();
            let Some(name) = name.to_str() else {
                continue;
            };
            if tools.pruned(name) {
                continue;
            }
            let child = directory.join(name);
            let Some(path) = child.strip_prefix(".").unwrap_or(&child).to_str() else {
                continue;
            };
            if !tools.candidate(path, name) {
                continue;
            }
            let metadata = opened.symlink_metadata(name)?;
            if metadata.file_type().is_symlink()
                || !tools.permits(&child, &metadata)
                || rules.ignores(&child, metadata.is_dir())
            {
                continue;
            }
            if metadata.is_dir() {
                if scan.directories {
                    files.push((path.to_owned(), true));
                }
                if scan.recursive {
                    pending.push((child, rules.clone()));
                }
            } else if single_link_file(&metadata) {
                files.push((path.to_owned(), false));
            }
        }
    }
    Ok((files, false))
}
