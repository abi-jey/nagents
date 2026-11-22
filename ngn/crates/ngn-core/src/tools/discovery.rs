//! Legacy path policy for the shared workspace discovery implementation.

use std::path::{Path, PathBuf};

use anyhow::Result;
use cap_std::fs::Dir;
use nagents_native::discovery::{self, Access, Scan};

use super::{Tools, denied_component};

impl Access for Tools {
    fn relative(&self, path: &str) -> Result<PathBuf> {
        self.relative(path, true)
    }

    fn directory(&self, path: &Path) -> Result<Dir> {
        self.directory(path)
    }

    fn read_ignore(&self, path: &str) -> Result<Vec<u8>> {
        self.read(path)
    }

    fn pruned(&self, name: &str) -> bool {
        denied_component(name) || discovery::dependency_directory(name)
    }
}

pub(super) fn walk(tools: &Tools, path: &str, allow_file: bool) -> Result<(Vec<String>, bool)> {
    let (entries, truncated) = discovery::walk(
        tools,
        path,
        Scan {
            recursive: true,
            directories: false,
            allow_file,
        },
    )?;
    let mut files = entries
        .into_iter()
        .map(|(path, _)| path)
        .collect::<Vec<_>>();
    files.sort();
    Ok((files, truncated))
}

#[cfg(test)]
mod tests;
