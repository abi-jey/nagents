#![cfg(unix)]

#[path = "../../ngn-core/src/test_support.rs"]
mod test_support;

use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::Command};

use ngn_core::{Config, Host, auth::IdentityGuard};
use serde_json::json;

fn setup(directory: &Path, body: &str) -> (Config, std::path::PathBuf) {
    let runner = directory.join("runner");
    fs::write(&runner, format!("#!/bin/sh\n{body}\n")).unwrap();
    fs::set_permissions(&runner, fs::Permissions::from_mode(0o700)).unwrap();
    let value = json!({
        "workspace": directory, "data_dir":directory.join("state"), "runner":[runner], "provider":"subscription",
        "providers":{"subscription":{"kind":"openai", "auth":"chatgpt", "model":"test-model"}}
    });
    let path = directory.join("config.json");
    fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
    (
        serde_json::from_value::<Config>(value)
            .unwrap()
            .resolve()
            .unwrap(),
        path,
    )
}

#[test]
fn native_auth_executes_trusted_argv_and_only_presents_validated_status() {
    let directory = test_support::private_tempdir().unwrap();
    let store = directory.path().join("state/auth/openai.json");
    let body = format!(
        "test \"$#\" -eq 4 && test \"$1\" = auth && test \"$2\" = status && test \"$3\" = --store && test \"$4\" = '{}' || exit 4\nprintf '%s\\n' '{{\"auth_protocol\":1,\"message\":{{\"type\":\"status\",\"status\":\"signed_out\"}}}}'",
        store.display()
    );
    let (_, path) = setup(directory.path(), &body);
    let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
        .arg("--config")
        .arg(path)
        .args(["auth", "status"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap().trim(),
        "Not signed in to ChatGPT."
    );
    assert!(!store.exists());
    assert!(!directory.path().join("state/workspaces").exists());
}

#[test]
fn secret_auth_frames_and_unknown_errors_are_never_presented() {
    for message in [
        json!({"type":"status", "status":"signed_out", "access_token":"PRIVATE-SECRET"}),
        json!({"type":"error", "code":"PRIVATE-SECRET"}),
        json!({"type":"challenge", "verification_url":"https://evil.invalid/PRIVATE-SECRET", "user_code":"TEST"}),
    ] {
        let directory = test_support::private_tempdir().unwrap();
        let frame = json!({"auth_protocol":1, "message":message});
        let (_, path) = setup(directory.path(), &format!("printf '%s\\n' '{frame}'"));
        let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
            .arg("--config")
            .arg(path)
            .args(["auth", "status"])
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
        let error = String::from_utf8(output.stderr).unwrap();
        assert!(error.contains("contents redacted"));
        assert!(!error.contains("PRIVATE-SECRET"));
    }
}

#[test]
fn native_mutations_fail_before_exec_while_any_using_host_is_alive() {
    let directory = test_support::private_tempdir().unwrap();
    let called = directory.path().join("called");
    let (config, path) = setup(directory.path(), &format!("touch '{}'", called.display()));
    let host = Host::open(config.clone()).unwrap();
    for operation in ["login", "logout"] {
        let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
            .arg("--config")
            .arg(&path)
            .args(["auth", operation])
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(
            String::from_utf8(output.stderr)
                .unwrap()
                .contains("Stop all ngn hosts")
        );
        assert!(!called.exists());
    }
    drop(host);
    assert!(IdentityGuard::mutation(&config.data_dir).is_ok());
    let guard = IdentityGuard::mutation(&config.data_dir).unwrap();
    assert!(Host::open(config).is_err());
    drop(guard);
}

#[test]
fn old_v1_runtime_never_receives_a_subscription_extension_without_capability() {
    let directory = test_support::private_tempdir().unwrap();
    let received = directory.path().join("received");
    let body = format!(
        "printf '%s\\n' '{{\"protocol_version\":1,\"run_id\":\"\",\"message\":{{\"type\":\"ready\",\"runtime\":\"old-v1\",\"capabilities\":[\"host_tools\",\"cancel\"]}}}}'\n/bin/cat > '{}'",
        received.display()
    );
    let (_, path) = setup(directory.path(), &body);
    let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
        .arg("--config")
        .arg(path)
        .args(["run", "--json", "hello"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(
        String::from_utf8(output.stdout)
            .unwrap()
            .contains("chatgpt_auth")
    );
    assert!(!received.exists() || fs::read(received).unwrap().is_empty());
    assert!(!directory.path().join("state/auth/openai.json").exists());
}
