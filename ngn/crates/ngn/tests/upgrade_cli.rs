#[cfg(any(target_os = "linux", target_os = "macos"))]
#[test]
fn upgrade_dispatch_is_explicit_and_independent_of_host_settings() {
    use std::process::Command;

    // CI's configured temporary directory can be group-writable.
    let root = tempfile::Builder::new()
        .permissions(std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .tempdir_in("/tmp")
        .unwrap();
    let backup = root.path().join("fixture.backup.db");
    std::fs::write(&backup, b"not sqlite").unwrap();
    let run = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_ngn"))
            .env_clear()
            .env("HOME", root.path())
            .env("NGN_WIRE_LOG", "invalid-for-a-running-host")
            .arg("-C")
            .arg(root.path())
            .args(args)
            .output()
            .unwrap()
    };
    let help = run(&["upgrade", "--help"]);
    assert!(help.status.success(), "{help:?}");
    let help = String::from_utf8(help.stdout).unwrap();
    assert!(help.contains("read-only history") && help.contains("--from <BACKUP>"));
    assert!(!root.path().join(".local").exists());

    let missing = run(&["upgrade"]);
    assert_eq!(missing.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&missing.stderr).contains("--from <BACKUP>"));
    let invalid = run(&["upgrade", "--from", backup.to_str().unwrap()]);
    assert_eq!(invalid.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&invalid.stderr).contains("regular SQLite file"));
    assert!(!root.path().join(".local/share/ngn-rust/.identity").exists());
    assert_eq!(
        std::fs::read_dir(root.path().join(".local/share/ngn-rust/workspaces"))
            .unwrap()
            .count(),
        0
    );

    for args in [
        vec!["--config", "does-not-exist", "upgrade", "--from"],
        vec!["upgrade", "--config", "does-not-exist", "--from"],
    ] {
        let mut args = args;
        args.push(backup.to_str().unwrap());
        let conflict = run(&args);
        assert_eq!(conflict.status.code(), Some(2));
        assert!(String::from_utf8_lossy(&conflict.stderr).contains("cannot be used with"));
    }
}
