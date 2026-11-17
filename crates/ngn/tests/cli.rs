use std::process::Command;

#[test]
fn native_demo_requires_neither_python_nor_provider_credentials() {
    let workspace = tempfile::tempdir().unwrap();
    let state = tempfile::tempdir().unwrap();
    std::fs::write(workspace.path().join("hello.txt"), "hello").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
        .args(["--demo", "-C"])
        .arg(workspace.path())
        .arg("--data-dir")
        .arg(state.path())
        .args(["run", "--json", "hello"])
        .env("PATH", workspace.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let records: Vec<serde_json::Value> = String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(records[0]["schema_version"], 1);
    assert_eq!(records.last().unwrap()["event"], "run_finished");
    assert_eq!(records.last().unwrap()["status"], "completed");
    assert!(
        records
            .iter()
            .any(|record| record["event"] == "tool_completed")
    );
}

#[test]
fn version_is_the_requested_first_rust_release() {
    let output = Command::new(env!("CARGO_BIN_EXE_ngn"))
        .arg("--version")
        .output()
        .unwrap();
    assert_eq!(
        String::from_utf8(output.stdout).unwrap().trim(),
        "ngn 0.100.0"
    );
}
