use std::{fs, path::PathBuf};

use ngn_protocol::ToolCall;
use serde_json::{Value, json};

use super::super::{Action, MAX_FILE, MAX_OUTPUT};
use super::*;

struct Fixture {
    directory: tempfile::TempDir,
    tools: Tools,
}

impl Fixture {
    fn new() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let tools =
            Tools::open(directory.path(), &directory.path().join("runtime"), true, 1).unwrap();
        Self { directory, tools }
    }

    fn path(&self, path: &str) -> PathBuf {
        self.directory.path().join(path)
    }

    fn write(&self, path: &str, bytes: impl AsRef<[u8]>) {
        let path = self.path(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
    }

    fn result(&self, name: &str, arguments: Value) -> Value {
        let prepared = self.tools.prepare(&call(name, arguments)).unwrap();
        assert!(!prepared.needs_approval);
        assert!(prepared.preview.is_empty());
        let Action::Result(result) = prepared.action else {
            panic!("Discovery must only prepare a result");
        };
        result
    }

    fn paths(&self, path: &str) -> Vec<String> {
        let result = self.result("list_files", json!({"path":path, "limit":1000}));
        assert_eq!(result["truncated"], false);
        result["paths"]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_str().unwrap().to_owned())
            .collect()
    }

    fn search(&self, path: &str) -> Value {
        self.result(
            "search",
            json!({"path":path, "query":"needle", "limit":1000}),
        )
    }

    fn fails(&self, path: &str) {
        for (name, arguments) in [
            ("list_files", json!({"path":path})),
            ("search", json!({"path":path, "query":"needle"})),
        ] {
            assert!(
                self.tools.prepare(&call(name, arguments)).is_err(),
                "{path}"
            );
        }
    }
}

fn call(name: &str, arguments: Value) -> ToolCall {
    ToolCall {
        call_id: "discovery".into(),
        name: name.into(),
        arguments,
    }
}

#[test]
fn origins_anchoring_negation_and_nested_start_paths() {
    let fixture = Fixture::new();
    fixture.write(
        ".gitignore",
        "*.log\n!keep.log\n/root.py\nsrc/hidden.py\ncache/\n",
    );
    fixture.write("src/.gitignore", "/local.py\ndeep/hidden.py\n!keep.log\n");
    let visible = [
        "keep.log",
        "src/keep.log",
        "src/root.py",
        "src/deep/local.py",
        "src/deep/visible.py",
    ];
    let hidden = [
        "root.py",
        "src/hidden.py",
        "src/local.py",
        "src/deep/hidden.py",
        "src/drop.log",
        "src/cache/file.py",
    ];
    for path in visible.iter().chain(hidden.iter()) {
        fixture.write(path, "needle\n");
    }
    for path in [".", "src", "src/deep", "./src/deep/."] {
        let prefix = path.trim_start_matches("./").trim_end_matches("/.");
        let mut expected: Vec<_> = visible
            .iter()
            .filter(|name| path == "." || name.starts_with(&format!("{prefix}/")))
            .copied()
            .collect();
        expected.sort();
        let found: Vec<_> = fixture
            .paths(path)
            .into_iter()
            .filter(|path| !path.ends_with(".gitignore"))
            .collect();
        assert_eq!(found, expected, "{path}");
        let expected: Vec<_> = expected
            .iter()
            .map(|path| json!({"path":path, "line":1, "text":"needle"}))
            .collect();
        let result = fixture.search(path);
        assert_eq!(result["matches"], json!(expected), "{path}");
        assert_eq!(result["truncated"], false);
    }
    for path in hidden {
        assert_eq!(fixture.search(path)["matches"], json!([]), "{path}");
    }
    assert_eq!(
        fixture.search("src/root.py")["matches"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn last_match_and_child_precedence_do_not_leak_between_siblings() {
    let fixture = Fixture::new();
    fixture.write(".gitignore", "*.log\n!keep.log\nsrc/keep.log\n");
    fixture.write(
        "src/.gitignore",
        "!keep.log\nkeep.log\n!keep.log\n*.txt\n!deep/keep.txt\n",
    );
    fixture.write("src/deep/.gitignore", "!restore.log\n/keep.txt\n");
    let visible = [
        "keep.log",
        "src/keep.log",
        "src/deep/restore.log",
        "sibling/file.txt",
    ];
    let hidden = [
        "src/drop.log",
        "src/file.txt",
        "src/deep/keep.txt",
        "sibling/drop.log",
    ];
    for path in visible.iter().chain(hidden.iter()) {
        fixture.write(path, "needle\n");
    }
    let mut expected = visible.to_vec();
    expected.sort();
    let found: Vec<_> = fixture
        .paths(".")
        .into_iter()
        .filter(|path| !path.ends_with(".gitignore"))
        .collect();
    assert_eq!(found, expected);
    assert_eq!(fixture.search("src/deep/keep.txt")["matches"], json!([]));
}

#[test]
fn excluded_directories_cannot_be_bypassed_by_explicit_paths_or_child_rules() {
    let fixture = Fixture::new();
    fixture.write(
        ".gitignore",
        "cache/\n!cache/deep/file.py\nblocked/\n!blocked/\nblocked/*\n!blocked/keep/\n",
    );
    fixture.write("cache/.gitignore", b"\xff");
    fixture.write("cache/deep/file.py", "needle\n");
    fixture.write("blocked/keep/file.py", "needle\n");
    fixture.write("blocked/drop/file.py", "needle\n");
    for path in [
        "cache",
        "cache/deep",
        "cache/deep/file.py",
        "blocked/drop/file.py",
    ] {
        assert_eq!(fixture.search(path)["matches"], json!([]), "{path}");
    }
    assert!(fixture.paths("cache/deep").is_empty());
    assert_eq!(fixture.paths("blocked"), ["blocked/keep/file.py"]);
    assert_eq!(fixture.paths("blocked/keep"), ["blocked/keep/file.py"]);
}

#[test]
fn matcher_handles_directory_only_patterns_wildcards_escapes_and_bom() {
    let fixture = Fixture::new();
    fixture.write(
        ".gitignore",
        "\u{feff}# comment\r\n**/generated/*.rs\r\n\\#literal\r\n\\!literal\r\nonly-directory/\r\n",
    );
    for path in [
        "#literal",
        "!literal",
        "src/generated/file.rs",
        "only-directory/file",
    ] {
        fixture.write(path, "needle\n");
    }
    for path in ["nested/only-directory", "src/generated/file.txt", "visible"] {
        fixture.write(path, "needle\n");
    }
    assert_eq!(
        fixture.paths("."),
        [
            ".gitignore",
            "nested/only-directory",
            "src/generated/file.txt",
            "visible"
        ]
    );
}

#[test]
fn only_workspace_gitignore_files_are_sources_of_rules() {
    let parent = tempfile::tempdir().unwrap();
    fs::write(parent.path().join(".gitignore"), "*\n").unwrap();
    let root = parent.path().join("workspace");
    fs::create_dir_all(root.join(".git/info")).unwrap();
    fs::write(root.join(".git/info/exclude"), "*\n").unwrap();
    fs::write(root.join(".ignore"), "*\n").unwrap();
    fs::write(root.join("visible"), "needle\n").unwrap();
    let tools = Tools::open(&root, &root.join("runtime"), true, 1).unwrap();
    assert_eq!(
        tools.walk(".").unwrap(),
        (vec![".ignore".into(), "visible".into()], false)
    );
}

#[test]
fn discovery_never_attaches_or_automatically_loads_instruction_payloads() {
    let fixture = Fixture::new();
    let marker = "INSTRUCTION_PAYLOAD_DO_NOT_ATTACH";
    fixture.write("AGENTS.md", marker.repeat(MAX_FILE / marker.len() + 1));
    fixture.write("nested/AGENTS.md", b"\xff\x00");
    fixture.write("nested/wire.py", "needle\n");
    for path in [".", "nested"] {
        let listing = fixture.result("list_files", json!({"path":path}));
        assert_eq!(listing.as_object().unwrap().len(), 2);
        assert!(listing.get("paths").is_some());
        assert!(listing.get("truncated").is_some());
        assert!(!listing.to_string().contains(marker));
        let search = fixture.search(path);
        assert_eq!(
            search,
            json!({"matches":[{"path":"nested/wire.py", "line":1, "text":"needle"}], "truncated":false})
        );
        assert!(!search.to_string().contains(marker));
    }
    assert!(!fixture.path("runtime").exists());
}

#[test]
fn credentials_protected_state_and_build_exclusions_cannot_be_negated() {
    let fixture = Fixture::new();
    fixture.write(".gitignore", "!*\n!**/*\n!**/\n");
    let blocked = [
        ".env",
        "nested/.env.local",
        ".aws/credentials",
        "nested/private.pem",
        "nested/private.key",
        ".ngn/host.sqlite3",
        "runtime/host.sqlite3",
        "runtime/deep/file",
        "src/credentials.json",
        "src/target/file",
        "node_modules/package/file",
        ".venv/file",
    ];
    for path in blocked {
        fixture.write(path, "needle synthetic fixture\n");
    }
    fixture.write("src/visible", "needle\n");
    assert_eq!(fixture.paths("."), [".gitignore", "src/visible"]);
    assert_eq!(
        fixture.search(".")["matches"],
        json!([{"path":"src/visible", "line":1, "text":"needle"}])
    );
    for path in blocked {
        let result = fixture
            .tools
            .prepare(&call("search", json!({"path":path, "query":"needle"})));
        if let Ok(prepared) = result {
            let Action::Result(value) = prepared.action else {
                panic!("Unexpected discovery action");
            };
            assert_eq!(value["matches"], json!([]), "{path}");
        }
    }
    for path in [
        "../outside",
        "/outside",
        "nested/../../outside",
        "src\\visible",
    ] {
        fixture.fails(path);
    }
}

#[cfg(unix)]
#[test]
fn symlinks_and_hardlinks_are_not_discovered_or_searched() {
    use std::os::unix::fs::symlink;

    let fixture = Fixture::new();
    fixture.write(".gitignore", "!*\n!**/*\n");
    fixture.write("single", "needle\n");
    fixture.write("linked", "needle\n");
    fs::hard_link(fixture.path("linked"), fixture.path("hardlink")).unwrap();
    fixture.write("real/file", "needle\n");
    symlink("single", fixture.path("symlink")).unwrap();
    symlink("real", fixture.path("directory-link")).unwrap();
    symlink(".env", fixture.path("credential-link")).unwrap();
    symlink("runtime", fixture.path("state-link")).unwrap();
    fixture.write(".env", "needle synthetic fixture\n");
    fixture.write("runtime/private", "needle synthetic fixture\n");
    fs::hard_link(fixture.path(".env"), fixture.path("credential-hardlink")).unwrap();
    fs::hard_link(
        fixture.path("runtime/private"),
        fixture.path("state-hardlink"),
    )
    .unwrap();
    assert_eq!(fixture.paths("."), [".gitignore", "real/file", "single"]);
    for path in [
        "linked",
        "hardlink",
        "credential-hardlink",
        "state-hardlink",
    ] {
        assert_eq!(fixture.search(path)["matches"], json!([]));
    }
    for path in [
        "symlink",
        "directory-link",
        "directory-link/file",
        "credential-link",
        "state-link/private",
    ] {
        fixture.fails(path);
    }
}

#[cfg(unix)]
#[test]
fn unsafe_pattern_files_fail_closed_without_following_links_or_opening_special_files() {
    use std::os::unix::{fs::symlink, net::UnixListener};

    let fixture = Fixture::new();
    fixture.write("visible", "needle\n");
    fixture.write("pattern-source", "visible\n");
    symlink("pattern-source", fixture.path(".gitignore")).unwrap();
    fixture.fails(".");
    fs::remove_file(fixture.path(".gitignore")).unwrap();
    fs::hard_link(fixture.path("pattern-source"), fixture.path(".gitignore")).unwrap();
    fixture.fails(".");
    fs::remove_file(fixture.path(".gitignore")).unwrap();
    let _socket = UnixListener::bind(fixture.path(".gitignore")).unwrap();
    fixture.fails(".");
    fs::remove_file(fixture.path(".gitignore")).unwrap();
    fs::create_dir(fixture.path(".gitignore")).unwrap();
    fixture.fails(".");
}

#[cfg(unix)]
#[test]
fn unreadable_pattern_files_fail_closed_when_permissions_are_enforced() {
    use std::os::unix::fs::PermissionsExt;

    let fixture = Fixture::new();
    fixture.write(".gitignore", "visible\n");
    fixture.write("visible", "needle\n");
    fs::set_permissions(fixture.path(".gitignore"), fs::Permissions::from_mode(0o0)).unwrap();
    // Privileged test runners may legitimately bypass mode bits.
    if fs::File::open(fixture.path(".gitignore")).is_err() {
        fixture.fails(".");
    }
    fs::set_permissions(
        fixture.path(".gitignore"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
}

#[test]
fn invalid_pattern_data_and_individual_file_line_limits_fail_closed() {
    let fixture = Fixture::new();
    fixture.write("visible", "needle\n");
    for bytes in [
        vec![0xff],
        b"visible\0".to_vec(),
        b"[z-a]".to_vec(),
        vec![b'a'; MAX_IGNORE_LINE + 1],
        vec![b'\n'; MAX_IGNORE_FILE + 1],
        vec![b'\n'; MAX_IGNORE_LINES + 1],
        vec![b'\n'; MAX_FILE + 1],
    ] {
        fixture.write(".gitignore", bytes);
        fixture.fails(".");
        fixture.fails("visible");
    }
}

#[test]
fn nested_unsafe_ignore_files_abort_instead_of_returning_partial_results() {
    let fixture = Fixture::new();
    fixture.write("visible", "needle\n");
    fixture.write("src/visible", "needle\n");
    fixture.write("src/.gitignore", b"\xff");
    for path in [".", "src", "src/visible"] {
        fixture.fails(path);
    }
}

#[test]
fn pattern_parse_errors_do_not_return_pattern_payloads() {
    let fixture = Fixture::new();
    let marker = "PATTERN_PAYLOAD_DO_NOT_ATTACH";
    fixture.write(".gitignore", format!("[z-a]{marker}"));
    let error = fixture
        .tools
        .prepare(&call("list_files", json!({})))
        .err()
        .unwrap();
    assert!(!format!("{error:#}").contains(marker));
}

#[test]
fn cumulative_ignore_byte_line_and_file_budgets_fail_closed() {
    let fixture = Fixture::new();
    let comment = format!("#{}\n", "x".repeat(1022)).repeat(64);
    assert_eq!(comment.len(), MAX_IGNORE_FILE);
    for path in [
        ".gitignore",
        "a/.gitignore",
        "a/b/.gitignore",
        "a/b/c/.gitignore",
        "a/b/c/d/.gitignore",
    ] {
        fixture.write(path, &comment);
    }
    fixture.fails(".");
    fixture.fails("a/b/c/d");

    let fixture = Fixture::new();
    fixture.write(".gitignore", "\n".repeat(MAX_IGNORE_LINES / 2));
    fixture.write("a/.gitignore", "\n".repeat(MAX_IGNORE_LINES / 2 + 1));
    fixture.fails(".");
    fixture.fails("a");

    let fixture = Fixture::new();
    for index in 0..=MAX_IGNORE_FILES {
        fixture.write(&format!("d{index:03}/.gitignore"), "");
    }
    fixture.fails(".");
}

#[test]
fn discovery_stops_at_max_paths_and_marks_results_truncated() {
    let fixture = Fixture::new();
    for index in 0..MAX_PATHS + 5 {
        fixture.write(&format!("file{index:04}"), "");
    }
    let (files, truncated) = fixture.tools.walk(".").unwrap();
    assert!(truncated);
    assert_eq!(files.len(), MAX_PATHS);
    let result = fixture.search(".");
    assert_eq!(result["matches"], json!([]));
    assert_eq!(result["truncated"], true);
}

#[test]
fn listing_and_search_cap_serialized_output_not_just_result_count() {
    let fixture = Fixture::new();
    for index in 0..300 {
        fixture.write(&format!("{index:03}-{}", "x".repeat(200)), "");
    }
    let result = fixture.result("list_files", json!({"limit":1000}));
    assert_eq!(result["truncated"], true);
    assert!(serde_json::to_vec(&result).unwrap().len() <= MAX_OUTPUT);
    assert!(result["paths"].as_array().unwrap().len() < 300);

    fixture.write(
        "matches",
        format!("{}\n", "needle \\\"".repeat(50)).repeat(300),
    );
    let result = fixture.search("matches");
    assert_eq!(result["truncated"], true);
    assert!(serde_json::to_vec(&result).unwrap().len() <= MAX_OUTPUT);
    assert!(result["matches"].as_array().unwrap().len() < 300);
}

#[test]
fn search_remains_literal_and_respects_file_and_cumulative_read_limits() {
    let fixture = Fixture::new();
    fixture.write("literal", "a.b\naxb\nA.B\n");
    let result = fixture.result("search", json!({"path":"literal", "query":"a.b"}));
    assert_eq!(
        result["matches"],
        json!([{"path":"literal", "line":1, "text":"a.b"}])
    );
    fixture.write("oversized", format!("needle{}", "x".repeat(MAX_FILE)));
    assert_eq!(fixture.search("oversized")["matches"], json!([]));

    let bytes = format!("needle\n{}", "x".repeat(MAX_FILE - 7));
    for index in 0..34 {
        fixture.write(&format!("many/file{index:02}"), &bytes);
    }
    let result = fixture.search("many");
    assert_eq!(result["truncated"], true);
    assert_eq!(result["matches"].as_array().unwrap().len(), 32);
}
