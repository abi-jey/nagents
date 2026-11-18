#![cfg(unix)]

#[path = "../../ngn-core/src/test_support.rs"]
mod test_support;

use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    process::Command,
};

use serde_json::{Value, json};

#[test]
fn wire_log_requires_an_explicit_opt_in_and_runtime_capability() {
    let root = test_support::private_tempdir().unwrap();
    let runner = root.path().join("runner.sh");
    fs::write(
        &runner,
        r#"
printf '%s\n' 'private-runner-stderr' >&2
printf '{"protocol_version":1,"run_id":"","message":{"type":"ready","runtime":"private-runtime-name","capabilities":["host_tools","cancel"%s]}}\n' "$CAPABILITY"
IFS= read -r start || exit 0
case "$start" in
  *'"wire_log":true'*) [ "$EXPECT_WIRE" = 1 ] && [ -f "$NGN_HOST_WIRE_LOG_PATH" ] && [ -n "$NGN_HOST_WIRE_LOG_ID" ] || exit 3 ;;
  *'"wire_log"'*) exit 4 ;;
  *) [ "$EXPECT_WIRE" = 0 ] && [ -z "$NGN_HOST_WIRE_LOG_PATH$NGN_HOST_WIRE_LOG_ID" ] || exit 5 ;;
esac
run_id=${start#*'"run_id":"'}
run_id=${run_id%%'"'*}
printf '{"protocol_version":1,"run_id":"%s","message":{"type":"tool_request","call":{"call_id":"private-call-id","name":"read_file","arguments":{"path":"private-file-name"}}}}\n' "$run_id"
IFS= read -r result || exit 6
case "$result" in *private-tool-payload*) ;; *) exit 7 ;; esac
printf '{"protocol_version":1,"run_id":"%s","message":{"type":"text_completed","text":"private-model-output"}}\n' "$run_id"
printf '{"protocol_version":1,"run_id":"%s","message":{"type":"finished","status":"completed","error":""}}\n' "$run_id"
"#,
    )
    .unwrap();
    fs::write(
        root.path().join("private-file-name"),
        "private-tool-payload",
    )
    .unwrap();
    let config = root.path().join("config.json");
    fs::write(
        &config,
        json!({
            "runner": ["/bin/sh", runner],
            "provider": "test",
            "providers": {
                "test": {"kind": "openai_compatible", "model": "private-model-id", "api_key_env": "FIXTURE_KEY"}
            }
        })
        .to_string(),
    )
    .unwrap();
    for (index, (args, env, modern, enabled)) in [
        (&["run"][..], None, false, false),
        (&["run"][..], Some("TrUe"), true, true),
        (&["run", "--no-wire-log"][..], Some("1"), false, false),
        (
            &["--no-wire-log", "run", "--wire-log"][..],
            Some("0"),
            true,
            true,
        ),
        (
            &["--wire-log", "run", "--no-wire-log"][..],
            None,
            false,
            false,
        ),
        (&["run", "--wire-log"][..], None, false, true),
    ]
    .into_iter()
    .enumerate()
    {
        let data = root.path().join(index.to_string());
        let mut command = Command::new(env!("CARGO_BIN_EXE_ngn"));
        command
            .args(["-C", root.path().to_str().unwrap(), "--config"])
            .arg(&config)
            .arg("--data-dir")
            .arg(&data)
            .args(args)
            .args(["--json", "private-user-prompt"])
            .env("FIXTURE_KEY", "private-credential-value")
            .env("EXPECT_WIRE", if enabled { "1" } else { "0" })
            .env("NGN_HOST_WIRE_LOG_PATH", "/ignored")
            .env("NGN_HOST_WIRE_LOG_ID", "ignored")
            .env(
                "CAPABILITY",
                if modern {
                    r#","provider_wire_log""#
                } else {
                    ""
                },
            )
            .env_remove("NGN_WIRE_LOG");
        if let Some(value) = env {
            command.env("NGN_WIRE_LOG", value);
        }
        let output = command.output().unwrap();
        assert_eq!(
            output.status.code(),
            Some(i32::from(enabled && !modern)),
            "{args:?}: {} / {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let mut wire = 0;
        let mut runtime = 0;
        for entry in fs::read_dir(data.join("logs")).unwrap() {
            let entry = entry.unwrap();
            assert_eq!(
                entry.metadata().unwrap().permissions().mode() & 0o777,
                0o600
            );
            let name = entry.file_name();
            let name = name.to_str().unwrap();
            if name.starts_with("wire-") {
                wire += 1;
            } else {
                assert!(name.starts_with("tui-"));
                runtime += 1;
                let text = fs::read_to_string(entry.path()).unwrap();
                assert!(!text.contains("private-"), "{text}");
                let events: Vec<String> = text
                    .lines()
                    .map(|line| {
                        let entry: Value = serde_json::from_str(line).unwrap();
                        assert_eq!(entry.as_object().unwrap().len(), 3);
                        entry["event"].as_str().unwrap().to_owned()
                    })
                    .collect();
                assert_eq!(events.first().unwrap(), "host_started");
                assert_eq!(events.last().unwrap(), "host_stopped");
                assert!(events.contains(&"runner_starting".into()));
                if enabled && !modern {
                    assert!(events.contains(&"runner_failed".into()));
                    assert!(
                        String::from_utf8_lossy(&output.stdout).contains("lacks provider_wire_log")
                    );
                } else {
                    assert!(events.contains(&"tool_completed".into()));
                    assert!(events.contains(&"run_completed".into()));
                }
            }
        }
        assert_eq!((wire, runtime), (usize::from(enabled), 1));
    }

    let data = root.path().join("unavailable-logs");
    fs::create_dir(&data).unwrap();
    let blocked = data.join("logs");
    fs::write(&blocked, "private existing data").unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_ngn"));
    command
        .arg("-C")
        .arg(root.path())
        .arg("--config")
        .arg(&config)
        .arg("--data-dir")
        .arg(&data)
        .env_remove("NGN_WIRE_LOG")
        .env("EXPECT_WIRE", "0")
        .env("CAPABILITY", "")
        .args(["run", "--json", "private-user-prompt"]);
    let output = command.output().unwrap();
    assert!(output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stdout).contains("private-model-output"));
    assert_eq!(
        output.stderr,
        b"ngn: runtime log unavailable; continuing without it\n"
    );
    let output = command.arg("--wire-log").output().unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(
        fs::read_to_string(&blocked).unwrap(),
        "private existing data"
    );
}

#[test]
fn log_creation_preserves_python_files_and_rejects_uncontrolled_directories() {
    let root = test_support::private_tempdir().unwrap();
    let config = root.path().join("config.json");
    fs::write(&config, "{}").unwrap();
    let data = root.path().join("state");
    let logs = data.join("logs");
    fs::create_dir_all(&logs).unwrap();
    fs::set_permissions(&logs, fs::Permissions::from_mode(0o700)).unwrap();
    let existing = logs.join("wire-20261118T200000-python.jsonl");
    fs::write(&existing, "existing Python diagnostic").unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_ngn"));
    command
        .arg("-C")
        .arg(root.path())
        .arg("--config")
        .arg(config)
        .arg("--data-dir")
        .arg(data)
        .args(["sessions", "--wire-log"]);
    let output = command.output().unwrap();
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        fs::read_to_string(&existing).unwrap(),
        "existing Python diagnostic"
    );
    assert_eq!(fs::read_dir(&logs).unwrap().count(), 3);

    fs::set_permissions(&logs, fs::Permissions::from_mode(0o770)).unwrap();
    let output = command.output().unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("owner-controlled"));
    let moved = root.path().join("moved");
    fs::rename(&logs, &moved).unwrap();
    fs::set_permissions(&moved, fs::Permissions::from_mode(0o700)).unwrap();
    symlink(&moved, &logs).unwrap();
    let output = command.output().unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("not a symlink"));
    let output = command.arg("--no-wire-log").output().unwrap();
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        output.stderr,
        b"ngn: runtime log unavailable; continuing without it\n"
    );
    assert_eq!(fs::read_dir(&moved).unwrap().count(), 3);
}
