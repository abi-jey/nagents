#[path = "../src/test_support.rs"]
mod test_support;

use std::process::Command;

#[test]
fn rejects_removed_demo_flag() {
    let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
        .args(["--demo", "run", "--json", "hello"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    let error = String::from_utf8(output.stderr).unwrap();
    assert!(error.contains("unexpected argument '--demo'"), "{error}");
}

#[test]
fn help_does_not_advertise_demo() {
    for args in [&["--help"][..], &["run", "--help"]] {
        let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
            .args(args)
            .output()
            .unwrap();
        assert!(output.status.success());
        let help = String::from_utf8(output.stdout).unwrap();
        assert!(help.contains("Usage:"));
        assert!(!help.contains("--demo"), "{help}");
    }
}

#[test]
fn version_is_the_requested_first_rust_release() {
    let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
        .arg("--version")
        .output()
        .unwrap();
    assert_eq!(
        String::from_utf8(output.stdout).unwrap().trim(),
        format!("ngn {}", env!("CARGO_PKG_VERSION"))
    );
}

#[test]
fn there_is_no_engine_choice_or_rust_host_command() {
    for args in [
        &["--engine", "legacy", "sessions"][..],
        &["--engine=python", "sessions"],
        &["upgrade", "--from", "backup.db"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
            .args(args)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2), "{args:?}");
    }
    let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
        .arg("--help")
        .output()
        .unwrap();
    let help = String::from_utf8(output.stdout).unwrap();
    assert!(help.contains("Python engine"), "{help}");
    for removed in ["--engine ", "legacy", "upgrade"] {
        assert!(!help.contains(removed), "{removed}: {help}");
    }
}

#[cfg(unix)]
#[test]
fn auth_and_doctor_run_in_the_engine_python_with_this_workspace() {
    use std::os::unix::fs::PermissionsExt;

    let root = test_support::private_tempdir().unwrap();
    let python = root.path().join("python");
    let log = root.path().join("argv");
    std::fs::write(
        &python,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{}'\nexit 3\n",
            log.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&python, std::fs::Permissions::from_mode(0o700)).unwrap();
    for (args, expected) in [
        (
            &["auth", "login"][..],
            &["login", "chatgpt", "--device-auth"][..],
        ),
        (
            &["auth", "login", "openrouter", "--key-stdin"],
            &["login", "openrouter", "--key-stdin"],
        ),
        (&["auth", "status"], &["login", "--status"]),
        (&["auth", "logout"], &["logout"]),
        (&["doctor"], &["doctor"]),
    ] {
        std::fs::write(&log, "").unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
            .arg("-C")
            .arg(root.path())
            .args(args)
            .env("NGN_ENGINE_PYTHON", &python)
            .output()
            .unwrap();
        // The Python command's exit status is ngn's.
        assert_eq!(output.status.code(), Some(3), "{args:?}");
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert_eq!(
            stdout.contains(&format!("Engine Python: {}", python.display())),
            args == ["doctor"],
            "{args:?}: {stdout}"
        );
        let argv = std::fs::read_to_string(&log).unwrap();
        let mut want = vec!["-I", "-B", "-m", "nagents.cli"];
        want.extend_from_slice(expected);
        let workspace = root.path().to_string_lossy();
        want.extend(["--workspace", workspace.as_ref()]);
        assert_eq!(argv.lines().collect::<Vec<_>>(), want, "{args:?}");
    }
}
