//! Human-readable change previews. The preview is display-only: approval
//! binds to the exact call, never to this text.

use similar::TextDiff;

/// Unified diff (3 lines of context) between `old` and `new`.
///
/// `old` is `None` for a file that does not exist yet; every line is then an
/// addition under a header that says so.
pub(super) fn change(path: &str, old: Option<&str>, new: &str) -> String {
    let Some(old) = old else {
        return new_file(path, new);
    };
    if old == new {
        return format!("--- a/{path}\n+++ b/{path}\n(no changes)\n");
    }
    TextDiff::from_lines(old, new)
        .unified_diff()
        .context_radius(3)
        .header(&format!("a/{path}"), &format!("b/{path}"))
        .to_string()
}

fn new_file(path: &str, content: &str) -> String {
    let lines: Vec<&str> = content.split_inclusive('\n').collect();
    let mut text = format!("--- /dev/null\n+++ b/{path} (new file)\n");
    if lines.is_empty() {
        text.push_str("(empty file)\n");
        return text;
    }
    text.push_str(&format!("@@ -0,0 +1,{} @@\n", lines.len()));
    for line in &lines {
        text.push('+');
        text.push_str(line);
        if !line.ends_with('\n') {
            text.push_str("\n\\ No newline at end of file\n");
        }
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    fn numbered(count: usize) -> String {
        (1..=count).map(|n| format!("line {n}\n")).collect()
    }

    #[test]
    fn edit_in_the_middle_shows_hunk_with_three_lines_of_context() {
        let old = numbered(20);
        let new = old.replace("line 10\n", "ten\n");
        let diff = change("src/a.txt", Some(&old), &new);
        assert_eq!(
            diff,
            "--- a/src/a.txt\n+++ b/src/a.txt\n@@ -7,7 +7,7 @@\n line 7\n line 8\n line 9\n-line 10\n+ten\n line 11\n line 12\n line 13\n"
        );
    }

    #[test]
    fn distant_edits_make_separate_hunks() {
        let old = numbered(40);
        let new = old.replace("line 2\n", "two\n").replace("line 38\n", "x\n");
        let diff = change("f", Some(&old), &new);
        assert_eq!(diff.matches("\n@@ ").count(), 2, "{diff}");
    }

    #[test]
    fn new_file_is_all_additions_with_a_header() {
        let diff = change("notes.md", None, "a\nb\n");
        assert_eq!(
            diff,
            "--- /dev/null\n+++ b/notes.md (new file)\n@@ -0,0 +1,2 @@\n+a\n+b\n"
        );
    }

    #[test]
    fn empty_new_file_is_explained() {
        assert!(change("e", None, "").contains("(empty file)"));
    }

    #[test]
    fn missing_trailing_newline_is_marked() {
        let diff = change("f", Some("a\nb"), "a\nc");
        assert!(
            diff.contains("-b\n\\ No newline at end of file\n"),
            "{diff}"
        );
        assert!(
            diff.contains("+c\n\\ No newline at end of file\n"),
            "{diff}"
        );
        let created = change("f", None, "a\nb");
        assert!(
            created.ends_with("+b\n\\ No newline at end of file\n"),
            "{created}"
        );
    }

    #[test]
    fn adding_only_a_trailing_newline_is_visible() {
        let diff = change("f", Some("a"), "a\n");
        assert!(
            diff.contains("-a\n\\ No newline at end of file\n+a\n"),
            "{diff}"
        );
    }

    #[test]
    fn crlf_files_diff_by_line() {
        let diff = change("f", Some("a\r\nb\r\nc\r\n"), "a\r\nB\r\nc\r\n");
        assert!(diff.contains("-b\r\n+B\r\n"), "{diff:?}");
        assert!(
            diff.contains(" a\r\n") && diff.contains(" c\r\n"),
            "{diff:?}"
        );
    }

    #[test]
    fn unicode_is_preserved() {
        let diff = change("日本.txt", Some("héllo 🌍\nzwei\n"), "héllo 🌎\nzwei\n");
        assert!(diff.starts_with("--- a/日本.txt\n+++ b/日本.txt\n"));
        assert!(diff.contains("-héllo 🌍\n+héllo 🌎\n"), "{diff}");
    }

    #[test]
    fn identical_content_reports_no_changes() {
        assert!(change("f", Some("x\n"), "x\n").contains("(no changes)"));
    }

    #[test]
    fn small_edit_in_a_large_file_stays_small() {
        let old = numbered(5_000);
        let new = old.replace("line 2500\n", "changed\n");
        let diff = change("big", Some(&old), &new);
        assert!(diff.len() < 200, "{} bytes", diff.len());
    }
}
