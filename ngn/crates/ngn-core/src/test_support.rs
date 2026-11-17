//! Private fixture roots shared by core, binary and integration tests only.

/// Request owner-only access at creation, without changing the process umask or
/// permissions on existing paths. In particular, umask 0002 must not create the
/// group-writable ancestors correctly rejected by the production identity guard.
pub(crate) fn private_tempdir() -> std::io::Result<tempfile::TempDir> {
    let mut builder = tempfile::Builder::new();
    builder.prefix("ngn-private-");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(std::fs::Permissions::from_mode(0o700));
    }
    builder.tempdir()
}
