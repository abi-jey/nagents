#![cfg(any(target_os = "linux", target_os = "macos"))]

use std::{
    fs,
    os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{Command, Output},
};

use rusqlite::Connection;
use sha2::{Digest, Sha256};

#[test]
fn upgrade_dispatch_is_explicit_and_independent_of_host_settings() {
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

struct Startup {
    root: tempfile::TempDir,
    workspace: PathBuf,
    xdg: PathBuf,
}

impl Startup {
    fn new() -> Self {
        let root = tempfile::Builder::new()
            .permissions(std::os::unix::fs::PermissionsExt::from_mode(0o700))
            .tempdir_in("/tmp")
            .unwrap();
        let workspace = root.path().join("workspace");
        fs::create_dir(&workspace).unwrap();
        let xdg = root.path().join("xdg");
        fs::DirBuilder::new().mode(0o700).create(&xdg).unwrap();
        Self {
            root,
            workspace,
            xdg,
        }
    }

    fn cli(&self, workspace: &Path) -> Command {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_ngn"));
        cmd.env_clear()
            .env("HOME", self.root.path())
            .env("XDG_DATA_HOME", &self.xdg)
            .env("XDG_CONFIG_HOME", self.root.path().join("config"))
            .current_dir(self.root.path())
            .arg("-C")
            .arg(workspace);
        cmd
    }

    fn native_data(&self) -> PathBuf {
        self.xdg.join("ngn-rust")
    }
}

fn scope(workspace: &Path) -> String {
    format!(
        "{:x}",
        Sha256::digest(workspace.canonicalize().unwrap().to_str().unwrap())
    )
}

fn python_path(data: &Path, workspace: &Path) -> PathBuf {
    let dir = data.join(&scope(workspace)[..32]);
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&dir)
        .unwrap();
    dir.join("sessions.db")
}

fn python_fixture(path: &Path, title: &str) -> Connection {
    let conn = Connection::open(path).unwrap();
    conn.execute_batch(
        "CREATE TABLE harness_sessions(id TEXT PRIMARY KEY, title TEXT);
         CREATE TABLE v2_sessions(id TEXT PRIMARY KEY, updated_at TIMESTAMP);
         CREATE TABLE v2_messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
             content TEXT, tool_calls TEXT, tool_call_id TEXT, name TEXT, created_at TIMESTAMP);
         CREATE TABLE credentials(secret TEXT);
         INSERT INTO credentials VALUES ('fixture-secret');
         INSERT INTO v2_sessions VALUES ('fixture', '2090-01-01');
         INSERT INTO v2_messages VALUES
             (1,'fixture','user','visible history',NULL,NULL,NULL,'2025-02-03'),
             (2,'fixture','assistant','fixture tool input','[{\"name\":\"shell\"}]',NULL,NULL,'2025-02-03'),
             (3,'fixture','tool','fixture tool output',NULL,'fake-id',NULL,'2025-02-03');"
    ).unwrap();
    conn.execute(
        "INSERT INTO harness_sessions VALUES ('fixture', ?)",
        [title],
    )
    .unwrap();
    conn
}

fn stdout(output: &Output) -> &str {
    assert!(output.status.success(), "{output:?}");
    std::str::from_utf8(&output.stdout).unwrap()
}

fn stderr(output: &Output) -> &str {
    std::str::from_utf8(&output.stderr).unwrap()
}

#[test]
fn automatic_import_is_scoped_read_only_idempotent_and_appends_to_native_history() {
    let startup = Startup::new();
    let workspace = &startup.workspace;
    let run = |args: &[&str]| startup.cli(workspace).args(args).output().unwrap();
    // Seed preexisting native history before there is a Python database.
    assert!(stdout(&run(&["sessions"])).is_empty());
    let native = startup
        .native_data()
        .join("workspaces")
        .join(scope(workspace));
    let db = Connection::open(native.join("host.sqlite3")).unwrap();
    db.execute(
        "INSERT INTO sessions VALUES ('native-fixture', 'Native fixture', 1)",
        [],
    )
    .unwrap();
    drop(db);

    let source = python_path(&startup.xdg.join("ngn"), workspace);
    drop(python_fixture(&source, "First workspace"));
    let other = startup.root.path().join("other");
    fs::create_dir(&other).unwrap();
    let other_source = python_path(&startup.xdg.join("ngn"), &other);
    drop(python_fixture(&other_source, "Second workspace"));
    let before = fs::read(&source).unwrap();
    let info = fs::metadata(&source).unwrap();

    let first = run(&["sessions"]);
    let listed = stdout(&first);
    assert!(
        listed.starts_with("native-fixture  Native fixture\n"),
        "{first:?}"
    );
    let archive = listed
        .lines()
        .nth(1)
        .unwrap_or_else(|| panic!("archive not listed: {first:?}"))
        .split_whitespace()
        .next()
        .unwrap();
    assert!(
        listed.contains("[Python archive] First workspace") && !listed.contains("Second workspace")
    );
    assert!(stderr(&first).contains("copied 1 read-only Python archives"));
    let second = run(&["sessions"]);
    assert_eq!(stdout(&second), listed);
    assert!(!stderr(&second).contains("copied"));
    let second_scope = startup.cli(&other).arg("sessions").output().unwrap();
    assert!(stdout(&second_scope).contains("[Python archive] Second workspace"));

    let denied = run(&["--resume", archive, "run", "never replay this"]);
    assert!(!denied.status.success());
    assert!(
        stderr(&denied).contains("Python archives are read-only"),
        "{denied:?}"
    );
    let after = fs::metadata(&source).unwrap();
    #[cfg(target_os = "linux")]
    assert_eq!(
        (info.atime(), info.atime_nsec()),
        (after.atime(), after.atime_nsec())
    );
    assert_eq!(fs::read(&source).unwrap(), before);
    assert_eq!(
        (
            info.mtime(),
            info.mtime_nsec(),
            info.ctime(),
            info.ctime_nsec()
        ),
        (
            after.mtime(),
            after.mtime_nsec(),
            after.ctime(),
            after.ctime_nsec()
        )
    );
    assert_eq!(fs::read_dir(source.parent().unwrap()).unwrap().count(), 1);
    assert!(!native.join("nagents.sqlite3").exists());

    let db = Connection::open(native.join("host.sqlite3")).unwrap();
    let count = |sql| {
        db.query_row::<u32, _, _>(sql, [], |row| row.get(0))
            .unwrap()
    };
    assert_eq!(count("SELECT count(*) FROM runs"), 0);
    assert_eq!(count("SELECT count(*) FROM session_queues"), 0);
    assert_eq!(count("SELECT count(*) FROM archived_sessions"), 1);
    assert_eq!(count("SELECT count(*) FROM events WHERE run_id IS NULL"), 2);
    assert_eq!(
        count("SELECT count(*) FROM events WHERE payload LIKE '%visible history%'"),
        1
    );
    assert_eq!(
        count(
            "SELECT count(*) FROM events WHERE payload LIKE '%fixture tool%' OR payload LIKE '%fixture-secret%'"
        ),
        0
    );
}

#[test]
fn busy_wal_and_unsupported_sources_do_not_stop_startup_and_remain_retryable() {
    let startup = Startup::new();
    let source = python_path(&startup.xdg.join("ngn"), &startup.workspace);
    let sessions = || {
        startup
            .cli(&startup.workspace)
            .arg("sessions")
            .output()
            .unwrap()
    };
    let writer = python_fixture(&source, "After retry");
    writer.execute_batch("BEGIN IMMEDIATE;").unwrap();
    let busy = sessions();
    assert!(
        stdout(&busy).is_empty() && stderr(&busy).contains("busy or cannot be safely locked"),
        "{busy:?}"
    );
    assert_eq!(fs::read_dir(source.parent().unwrap()).unwrap().count(), 1);

    writer.execute_batch("ROLLBACK; PRAGMA journal_mode=WAL; INSERT INTO harness_sessions VALUES ('wal', 'WAL');").unwrap();
    let content = |suffix| fs::read(format!("{}{suffix}", source.display())).unwrap();
    let (main, wal, shm) = (content(""), content("-wal"), content("-shm"));
    let with_wal = sessions();
    assert!(
        stdout(&with_wal).is_empty() && stderr(&with_wal).contains("sidecars"),
        "{with_wal:?}"
    );
    assert_eq!(
        (content(""), content("-wal"), content("-shm")),
        (main, wal, shm)
    );
    drop(writer);
    let checkpointed_wal = sessions();
    assert!(stdout(&checkpointed_wal).is_empty() && stderr(&checkpointed_wal).contains("not WAL"));

    let writer = Connection::open(&source).unwrap();
    writer
        .execute_batch("PRAGMA journal_mode=DELETE; ALTER TABLE v2_messages RENAME TO unsupported;")
        .unwrap();
    drop(writer);
    let unsupported = sessions();
    assert!(
        stdout(&unsupported).is_empty()
            && stderr(&unsupported).contains("Unsupported Python backup schema")
    );
    assert!(stderr(&unsupported).contains("upgrade --from BACKUP"));
    let writer = Connection::open(&source).unwrap();
    writer
        .execute_batch("ALTER TABLE unsupported RENAME TO v2_messages;")
        .unwrap();
    drop(writer);
    assert!(stdout(&sessions()).contains("[Python archive] After retry"));
}

#[test]
fn only_state_opening_commands_copy_using_pythons_existing_path_precedence() {
    let startup = Startup::new();
    let workspace = &startup.workspace;
    let home_source = python_path(&startup.root.path().join(".local/share/ngn"), workspace);
    drop(python_fixture(&home_source, "Home fallback"));
    let run = |args: &[&str]| {
        startup
            .cli(workspace)
            .env_remove("XDG_DATA_HOME")
            .args(args)
            .output()
            .unwrap()
    };
    for args in [
        &["doctor"][..],
        &["--help"],
        &["--version"],
        &["auth", "--help"],
        &["install", "--help"],
        &["update", "--help"],
    ] {
        let output = run(args);
        assert!(!stdout(&output).contains("[Python archive]"));
        assert!(!stderr(&output).contains("Python history was not copied"));
        assert!(
            !startup.root.path().join(".local/share/ngn-rust").exists(),
            "{output:?}"
        );
    }
    assert!(stdout(&run(&["sessions"])).contains("[Python archive] Home fallback"));

    let env_source = python_path(&startup.root.path().join("env-state"), workspace);
    drop(python_fixture(&env_source, "Environment"));
    let env_run = || {
        startup
            .cli(workspace)
            .env("NGN_DATA_DIR", "env-state")
            .arg("sessions")
            .output()
            .unwrap()
    };
    assert!(stdout(&env_run()).contains("[Python archive] Environment"));

    let personal = startup.root.path().join("config/ngn");
    let personal_source = python_path(&personal.join("personal-state"), workspace);
    drop(python_fixture(&personal_source, "Personal override"));
    fs::write(
        personal.join("config.json"),
        r#"{"data_dir":"personal-state","plugins":["do-not-load.py:setup"]}"#,
    )
    .unwrap();
    fs::set_permissions(
        personal.join("config.json"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    fs::create_dir(workspace.join(".ngn")).unwrap();
    fs::write(
        workspace.join(".ngn/config.json"),
        r#"{"data_dir":"../fixture-do-not-read"}"#,
    )
    .unwrap();
    let custom_native = startup.root.path().join("custom-rust");
    let personal_run = startup
        .cli(workspace)
        .env("NGN_DATA_DIR", "env-state")
        .arg("--data-dir")
        .arg(&custom_native)
        .arg("sessions")
        .output()
        .unwrap();
    assert!(stdout(&personal_run).contains("[Python archive] Personal override"));
    assert!(!stdout(&personal_run).contains("Environment"));

    let overlap = startup
        .cli(workspace)
        .arg("--data-dir")
        .arg(personal_source.parent().unwrap())
        .arg("sessions")
        .output()
        .unwrap();
    assert!(
        !overlap.status.success() && stderr(&overlap).contains("separate Rust --data-dir"),
        "{overlap:?}"
    );
    assert_eq!(
        fs::read_dir(personal_source.parent().unwrap())
            .unwrap()
            .count(),
        1
    );
}
