#[path = "../../ngn-core/src/test_support.rs"]
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
fn doctor_uses_explicit_config_without_starting_a_runtime() {
    let workspace = test_support::private_tempdir().unwrap();
    let config = workspace.path().join("rust.json");
    std::fs::write(
        &config,
        r#"{"backend":"nagents","runner":["unavailable-runtime"]}"#,
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
        .arg("-C")
        .arg(workspace.path())
        .arg("--config")
        .arg(&config)
        .arg("doctor")
        .env("PATH", workspace.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report = String::from_utf8(output.stdout).unwrap();
    assert!(
        report.contains(&format!("Config: {}", config.display())),
        "{report}"
    );
    assert!(report.contains("Backend: Nagents"), "{report}");
    assert!(report.contains("Provider: none"), "{report}");
    assert!(
        report.contains("Provider setup: No provider selected"),
        "{report}"
    );
    assert!(report.contains("Runner: unavailable-runtime"), "{report}");
    assert!(!report.contains("--demo"), "{report}");
}

#[test]
fn implicit_read_only_is_a_ceiling_and_native_state_stays_separate() {
    let root = test_support::private_tempdir().unwrap();
    let config_home = root.path().join("config");
    let data_home = root.path().join("data");
    let python_data = data_home.join("ngn");
    let python_settings = config_home.join("ngn/config.json");
    std::fs::create_dir_all(python_settings.parent().unwrap()).unwrap();
    let doctor = |options: &[&str], env_read_only: &str| {
        Command::new(env!("CARGO_BIN_EXE_ngn"))
            .env("HOME", root.path())
            .env("XDG_CONFIG_HOME", &config_home)
            .env("XDG_DATA_HOME", &data_home)
            .env("NGN_READ_ONLY", env_read_only)
            .env_remove("NGN_PROVIDER_ID")
            .args(options)
            .arg("doctor")
            .output()
            .unwrap()
    };
    for (file_read_only, env_read_only, arguments, expected) in [
        (true, "false", &[][..], true),
        (false, "true", &[][..], true),
        (false, "false", &["--read-only"], true),
        (false, "false", &[][..], false),
    ] {
        let settings = format!(r#"{{"read_only":{file_read_only}}}"#);
        std::fs::write(&python_settings, &settings).unwrap();
        let output = doctor(arguments, env_read_only);
        assert!(output.status.success(), "{output:?}");
        let report = String::from_utf8(output.stdout).unwrap();
        assert!(
            report.contains(&format!("Read only: {expected}")),
            "{report}"
        );
        assert_eq!(std::fs::read_to_string(&python_settings).unwrap(), settings);
    }
    let output = doctor(&["--data-dir", python_data.to_str().unwrap()], "false");
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("separate data directory"));
    assert!(!python_data.exists());
}

#[test]
fn rejects_demo_backend_in_config_even_in_the_test_build() {
    let workspace = test_support::private_tempdir().unwrap();
    let config = workspace.path().join("rust.json");
    std::fs::write(&config, r#"{"backend":"demo"}"#).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
        .arg("--config")
        .arg(&config)
        .arg("doctor")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    let error = String::from_utf8(output.stderr).unwrap();
    assert!(error.contains("Invalid Rust ngn configuration"), "{error}");
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
