//! Shared rules for walking a private directory's ancestry.

use std::{
    fs::{self, Metadata},
    path::{Path, PathBuf},
};

/// Resolve an operating-system path alias such as macOS `/var -> /private/var` or
/// `/tmp -> /private/tmp`, which put every temporary directory behind a symlink.
///
/// Only a root-owned symlink whose parent directory is root-owned and neither
/// group- nor world-writable qualifies, and its target must be a root-owned
/// directory. Only root could create or replace such a link, so following it
/// cannot redirect a write. Every other symlink returns `None` and stays refused.
#[cfg(unix)]
pub fn trusted_system_alias(path: &Path, metadata: &Metadata) -> Option<PathBuf> {
    use std::os::unix::fs::MetadataExt;
    if !metadata.file_type().is_symlink() || metadata.uid() != 0 {
        return None;
    }
    let parent = path.parent()?.symlink_metadata().ok()?;
    if !parent.is_dir() || parent.uid() != 0 || parent.mode() & 0o022 != 0 {
        return None;
    }
    let target = fs::canonicalize(path).ok()?;
    let resolved = target.symlink_metadata().ok()?;
    (resolved.is_dir() && resolved.uid() == 0).then_some(target)
}

#[cfg(not(unix))]
pub fn trusted_system_alias(_path: &Path, _metadata: &Metadata) -> Option<PathBuf> {
    None
}

/// Spell `path` without OS aliases (macOS `/var` becomes `/private/var`) so a
/// recorded location matches the canonical path of the running executable.
/// Untrusted symlinks are left in place for the ancestry checks to refuse.
pub fn without_system_aliases(path: &Path) -> PathBuf {
    let mut resolved = PathBuf::new();
    for component in path.components() {
        resolved.push(component);
        if let Ok(metadata) = resolved.symlink_metadata()
            && let Some(target) = trusted_system_alias(&resolved, &metadata)
        {
            resolved = target;
        }
    }
    resolved
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn user_owned_symlinks_are_never_trusted() {
        let directory = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(directory.path()).unwrap();
        fs::create_dir(root.join("real")).unwrap();
        std::os::unix::fs::symlink(root.join("real"), root.join("alias")).unwrap();
        let metadata = root.join("alias").symlink_metadata().unwrap();
        assert_eq!(trusted_system_alias(&root.join("alias"), &metadata), None);
        let real = root.join("real").symlink_metadata().unwrap();
        assert_eq!(trusted_system_alias(&root.join("real"), &real), None);
    }

    #[test]
    fn spelling_without_aliases_keeps_untrusted_symlinks() {
        let directory = tempfile::tempdir().unwrap();
        let root = without_system_aliases(directory.path());
        assert_eq!(root, fs::canonicalize(directory.path()).unwrap());
        fs::create_dir(root.join("real")).unwrap();
        std::os::unix::fs::symlink(root.join("real"), root.join("alias")).unwrap();
        assert_eq!(
            without_system_aliases(&root.join("alias/prefix")),
            root.join("alias/prefix")
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_private_aliases_resolve() {
        for alias in ["/var", "/tmp"] {
            let path = Path::new(alias);
            let metadata = path.symlink_metadata().unwrap();
            assert_eq!(
                trusted_system_alias(path, &metadata),
                Some(PathBuf::from(format!("/private{alias}")))
            );
        }
    }
}
