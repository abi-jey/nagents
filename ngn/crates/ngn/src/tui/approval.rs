//! The approval panel: wording, colouring, sizing and keys.
//!
//! Everything here is presentation. The decision still binds to the exact
//! pending approval id in the engine, never to this text.

use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{
    layout::Rect,
    style::{Color, Modifier, Style},
    text::Line,
    widgets::{Block, Paragraph, Wrap},
};

use crate::{engine::Approval, view};

/// Plain `y` and `Enter` only count after this long without any keypress
/// since the panel appeared, so typing aimed at the composer cannot approve.
pub(super) const EARLY_KEY_GUARD: Duration = Duration::from_millis(300);

pub(super) const KEYS: &str = " y allow · n deny · Esc stop · PgUp/PgDn scroll ";

const MIN_HEIGHT: u16 = 5;
const MAX_WIDTH: u16 = 110;
const MAX_HEIGHT: u16 = 32;

/// When the current approval appeared, or the last key arrived after that.
pub(super) struct Since(Instant);

impl Default for Since {
    fn default() -> Self {
        Self(Instant::now())
    }
}

impl Since {
    pub(super) fn reset(&mut self) {
        self.0 = Instant::now();
    }

    pub(super) fn elapsed(&self) -> Duration {
        self.0.elapsed()
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum Action {
    Allow,
    Deny,
    /// Stop the whole run (the approval is cancelled with it).
    Cancel,
    ScrollUp,
    ScrollDown,
    /// Consumed on purpose: an early `y`/`Enter`.
    Ignore,
    /// Not an approval key; later handlers may see it.
    Pass,
}

pub(super) fn action(key: KeyEvent, shown_for: Duration) -> Action {
    let control = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        // An auto-repeating key is never a decision.
        KeyCode::Char('y' | 'n') | KeyCode::Enter if key.kind == KeyEventKind::Repeat => {
            Action::Ignore
        }
        // Deliberate chords kept for compatibility; they are not typed by accident.
        KeyCode::Char('y') if control => Action::Allow,
        KeyCode::Char('d') if control => Action::Deny,
        KeyCode::Char('y') | KeyCode::Enter if key.modifiers == KeyModifiers::NONE => {
            if shown_for < EARLY_KEY_GUARD {
                Action::Ignore
            } else {
                Action::Allow
            }
        }
        KeyCode::Char('n') if key.modifiers == KeyModifiers::NONE => Action::Deny,
        KeyCode::Esc => Action::Cancel,
        KeyCode::PageUp => Action::ScrollUp,
        KeyCode::PageDown => Action::ScrollDown,
        _ => Action::Pass,
    }
}

/// Characters that render invisibly or reorder text, so a command or path
/// could read differently from what runs.
fn is_invisible(c: char) -> bool {
    matches!(c,
        '\u{00AD}' | '\u{034F}' | '\u{061C}' | '\u{115F}' | '\u{1160}' | '\u{17B4}' | '\u{17B5}'
        | '\u{180B}'..='\u{180F}' | '\u{200B}'..='\u{200F}' | '\u{2028}'..='\u{202E}'
        | '\u{2060}'..='\u{206F}' | '\u{3164}' | '\u{FE00}'..='\u{FE0F}' | '\u{FEFF}'
        | '\u{FFA0}' | '\u{FFF0}'..='\u{FFFB}' | '\u{E0000}'..='\u{E0FFF}')
}

/// `view::plain`, plus invisible and bidirectional characters shown as `⟨U+XXXX⟩`.
pub(super) fn reveal(text: &str) -> String {
    let mut out = String::new();
    for c in view::plain(text).chars() {
        if is_invisible(c) {
            out.push_str(&format!("⟨U+{:04X}⟩", u32::from(c)));
        } else {
            out.push(c);
        }
    }
    out
}

pub(super) fn title(approval: &Approval) -> String {
    let name = reveal(&approval.call.name);
    let path = approval
        .call
        .arguments
        .get("path")
        .and_then(|value| value.as_str())
        .map(reveal);
    let mut text = match (name.as_str(), path) {
        ("shell", _) => "Run shell command?".to_owned(),
        (_, Some(path)) => format!("Allow {name} to {path}?"),
        _ => format!("Allow {name}?"),
    };
    if approval.depth > 0 {
        text.push_str(&format!(" · from {}", reveal(&approval.agent_title)));
    }
    format!(" {text} ")
}

pub(super) fn lines(approval: &Approval, no_color: bool) -> Vec<Line<'static>> {
    let preview = reveal(&approval.preview);
    let paint = |color: Color| {
        if no_color {
            Style::new()
        } else {
            Style::new().fg(color)
        }
    };
    let bold = Style::new().add_modifier(Modifier::BOLD);
    let dim = Style::new().add_modifier(Modifier::DIM);
    if approval.call.name == "shell"
        && let Some((command, notes)) = preview.rsplit_once("\n\n")
    {
        // The command is first and bold; the unsandboxed note and workspace
        // follow, dim. Splitting at the last blank line keeps any blank lines
        // inside the command itself in the command.
        return command
            .lines()
            .map(|line| Line::styled(line.to_owned(), bold))
            .chain(std::iter::once(Line::raw("")))
            .chain(notes.lines().map(|line| Line::styled(line.to_owned(), dim)))
            .collect();
    }
    let mut in_hunk = false;
    preview
        .lines()
        .map(|line| {
            let style = if line.starts_with("@@") {
                in_hunk = true;
                paint(Color::Cyan).add_modifier(Modifier::DIM)
            } else if !in_hunk && (line.starts_with("--- ") || line.starts_with("+++ ")) {
                bold
            } else if line.starts_with('+') {
                paint(Color::Green)
            } else if line.starts_with('-') {
                paint(Color::Red)
            } else if line.starts_with('\\') {
                dim
            } else {
                Style::new()
            };
            Line::styled(line.to_owned(), style)
        })
        .collect()
}

/// Content rows that must be visible before an approval can be given.
const MIN_VISIBLE: usize = 3;

/// Whether `panel` shows enough of `content` to decide on it.
pub(super) fn fits(panel: Rect, content: &Paragraph) -> bool {
    let rows = content.line_count(panel.width.saturating_sub(2));
    usize::from(panel.height.saturating_sub(2)) >= rows.min(MIN_VISIBLE)
}

/// The furthest useful scroll position.
pub(super) fn max_scroll(panel: Rect, content: &Paragraph) -> u16 {
    let rows = content.line_count(panel.width.saturating_sub(2));
    u16::try_from(rows)
        .unwrap_or(u16::MAX)
        .saturating_sub(panel.height.saturating_sub(2))
}

/// Anchor the panel at the bottom of `area`, as tall as its content needs.
pub(super) fn panel(area: Rect, content: &Paragraph) -> Rect {
    let width = area.width.min(MAX_WIDTH);
    let rows = content.line_count(width.saturating_sub(2));
    let rows = u16::try_from(rows).unwrap_or(u16::MAX);
    let max = area.height.min(MAX_HEIGHT);
    let height = rows.saturating_add(2).clamp(MIN_HEIGHT.min(max), max);
    Rect::new(area.x, area.y + area.height - height, width, height)
}

/// Build the content paragraph (no block, so sizing excludes the border).
pub(super) fn content(approval: &Approval, no_color: bool) -> Paragraph<'static> {
    Paragraph::new(lines(approval, no_color)).wrap(Wrap { trim: false })
}

pub(super) const TOO_SMALL: &str = " Enlarge the terminal to review · n deny · Esc stop ";
pub(super) const MORE: &str = " y allow · n deny · Esc stop · PgDn more below ";

/// `fits` and whether more lines are below pick the footer.
pub(super) fn block(approval: &Approval, fits: bool, more: bool) -> Block<'static> {
    Block::bordered()
        .border_style(Style::new().fg(Color::Yellow))
        .title(title(approval))
        .title_bottom(match (fits, more) {
            (false, _) => TOO_SMALL,
            (true, true) => MORE,
            (true, false) => KEYS,
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn approval(name: &str, arguments: serde_json::Value, preview: &str, depth: u8) -> Approval {
        Approval {
            id: "a".into(),
            run_id: "r".into(),
            session_id: "s".into(),
            root_session_id: "s".into(),
            agent_title: "Reviewer".into(),
            depth,
            call: ngn_protocol::ToolCall {
                call_id: "c".into(),
                name: name.into(),
                arguments,
            },
            preview: preview.into(),
        }
    }

    fn key(code: KeyCode, modifiers: KeyModifiers) -> KeyEvent {
        KeyEvent::new(code, modifiers)
    }

    const LATE: Duration = Duration::from_millis(400);
    const EARLY: Duration = Duration::from_millis(50);

    #[test]
    fn titles_use_plain_language() {
        let edit = approval("edit", serde_json::json!({"path":"hello.txt"}), "", 0);
        assert_eq!(title(&edit), " Allow edit to hello.txt? ");
        let write = approval("write", serde_json::json!({"path":"notes.md"}), "", 0);
        assert_eq!(title(&write), " Allow write to notes.md? ");
        let shell = approval("shell", serde_json::json!({"command":"ls"}), "", 0);
        assert_eq!(title(&shell), " Run shell command? ");
        let other = approval("demo_preview", serde_json::json!({}), "", 0);
        assert_eq!(title(&other), " Allow demo_preview? ");
    }

    #[test]
    fn subagent_titles_name_the_agent_not_the_depth() {
        let child = approval("shell", serde_json::json!({}), "", 2);
        assert_eq!(title(&child), " Run shell command? · from Reviewer ");
        assert!(!title(&child).contains("depth"));
    }

    #[test]
    fn titles_strip_terminal_control_sequences() {
        let hostile = approval("edit", serde_json::json!({"path":"a\u{1b}[31mb"}), "", 0);
        assert!(!title(&hostile).contains('\u{1b}'));
    }

    #[test]
    fn y_enter_n_and_the_old_chords_decide() {
        let none = KeyModifiers::NONE;
        let ctrl = KeyModifiers::CONTROL;
        assert_eq!(action(key(KeyCode::Char('y'), none), LATE), Action::Allow);
        assert_eq!(action(key(KeyCode::Enter, none), LATE), Action::Allow);
        assert_eq!(action(key(KeyCode::Char('n'), none), LATE), Action::Deny);
        assert_eq!(action(key(KeyCode::Char('y'), ctrl), LATE), Action::Allow);
        assert_eq!(action(key(KeyCode::Char('d'), ctrl), LATE), Action::Deny);
        assert_eq!(action(key(KeyCode::Esc, none), LATE), Action::Cancel);
        assert_eq!(action(key(KeyCode::PageUp, none), LATE), Action::ScrollUp);
        assert_eq!(
            action(key(KeyCode::PageDown, none), LATE),
            Action::ScrollDown
        );
    }

    #[test]
    fn other_keys_never_decide() {
        let none = KeyModifiers::NONE;
        for code in [
            KeyCode::Char('Y'),
            KeyCode::Char('x'),
            KeyCode::Char(' '),
            KeyCode::Tab,
        ] {
            assert_eq!(action(key(code, none), LATE), Action::Pass, "{code:?}");
        }
        for modifiers in [
            KeyModifiers::SHIFT,
            KeyModifiers::ALT,
            KeyModifiers::CONTROL,
        ] {
            assert_eq!(action(key(KeyCode::Enter, modifiers), LATE), Action::Pass);
        }
        for modifiers in [KeyModifiers::SHIFT, KeyModifiers::ALT] {
            assert_eq!(
                action(key(KeyCode::Char('y'), modifiers), LATE),
                Action::Pass
            );
        }
    }

    #[test]
    fn early_y_and_enter_are_swallowed_but_not_denials_or_chords() {
        let none = KeyModifiers::NONE;
        assert_eq!(action(key(KeyCode::Char('y'), none), EARLY), Action::Ignore);
        assert_eq!(action(key(KeyCode::Enter, none), EARLY), Action::Ignore);
        assert_eq!(action(key(KeyCode::Char('n'), none), EARLY), Action::Deny);
        assert_eq!(action(key(KeyCode::Esc, none), EARLY), Action::Cancel);
        assert_eq!(
            action(key(KeyCode::Char('y'), KeyModifiers::CONTROL), EARLY),
            Action::Allow
        );
        assert_eq!(
            action(key(KeyCode::Enter, none), EARLY_KEY_GUARD),
            Action::Allow
        );
    }

    fn colours(lines: &[Line]) -> Vec<(String, Option<Color>)> {
        lines
            .iter()
            .map(|line| {
                (
                    line.spans
                        .iter()
                        .map(|span| span.content.as_ref())
                        .collect(),
                    line.style.fg,
                )
            })
            .collect()
    }

    const DIFF: &str = "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n ctx\n--- removed dashes\n+added\n\\ No newline at end of file\n";

    #[test]
    fn diff_lines_are_coloured_and_headers_stay_headers() {
        let item = approval("edit", serde_json::json!({"path":"f"}), DIFF, 0);
        let painted = colours(&lines(&item, false));
        let fg = |index: usize| painted[index].1;
        assert_eq!(fg(0), None); // header: bold, not red
        assert!(
            lines(&item, false)[0]
                .style
                .add_modifier
                .contains(Modifier::BOLD)
        );
        assert_eq!(fg(1), None);
        assert_eq!(fg(2), Some(Color::Cyan));
        assert_eq!(fg(3), None);
        assert_eq!(
            fg(4),
            Some(Color::Red),
            "a removed line starting with dashes"
        );
        assert_eq!(fg(5), Some(Color::Green));
        assert_eq!(fg(6), None);
    }

    #[test]
    fn no_color_keeps_the_markers_but_not_the_colours() {
        let item = approval("edit", serde_json::json!({"path":"f"}), DIFF, 0);
        let painted = colours(&lines(&item, true));
        assert!(painted.iter().all(|(_, colour)| colour.is_none()));
        assert!(painted.iter().any(|(text, _)| text == "+added"));
        assert!(painted.iter().any(|(text, _)| text == "--- removed dashes"));
    }

    #[test]
    fn shell_shows_the_command_first_and_the_warning_dim() {
        let preview =
            "$ ls -la && echo done\n\nRuns unsandboxed as your user · timeout 120s\nWorkspace: /w";
        let item = approval(
            "shell",
            serde_json::json!({"command":"ls -la && echo done"}),
            preview,
            0,
        );
        let rendered = lines(&item, false);
        assert_eq!(rendered[0].to_string(), "$ ls -la && echo done");
        assert!(rendered[0].style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(
            rendered[2].to_string(),
            "Runs unsandboxed as your user · timeout 120s"
        );
        assert!(rendered[2].style.add_modifier.contains(Modifier::DIM));
        assert!(rendered[3].style.add_modifier.contains(Modifier::DIM));
    }

    #[test]
    fn panel_fits_short_previews_at_the_bottom_and_caps_long_ones() {
        let area = Rect::new(0, 0, 100, 40);
        let short = approval(
            "shell",
            serde_json::json!({}),
            "$ ls\n\nnote\nWorkspace: /w",
            0,
        );
        let rect = panel(area, &content(&short, false));
        assert_eq!(rect.height, MIN_HEIGHT.max(4 + 2));
        assert_eq!(rect.y + rect.height, area.height);
        let long = approval("edit", serde_json::json!({}), &"+x\n".repeat(500), 0);
        let rect = panel(area, &content(&long, false));
        assert_eq!(rect.height, MAX_HEIGHT);
        let tiny = panel(Rect::new(0, 0, 50, 3), &content(&short, false));
        assert_eq!(tiny.height, 3);
    }

    #[test]
    fn invisible_and_bidi_characters_are_shown_not_applied() {
        let spoof = approval(
            "shell",
            serde_json::json!({}),
            "$ echo safe\u{202E}fr- mr\u{200B}\n\nRuns unsandboxed",
            0,
        );
        let text: String = lines(&spoof, true)
            .iter()
            .map(ToString::to_string)
            .collect();
        assert!(
            text.contains("⟨U+202E⟩") && text.contains("⟨U+200B⟩"),
            "{text}"
        );
        assert!(!text.contains('\u{202E}'));
        let path = approval("write", serde_json::json!({"path":"a\u{2066}b"}), "", 0);
        assert!(title(&path).contains("⟨U+2066⟩"));
        // Ordinary text, including wide and accented characters, is untouched.
        assert_eq!(reveal("café 漢字 ✓"), "café 漢字 ✓");
    }

    #[test]
    fn repeated_keys_never_decide() {
        let mut key = KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE);
        key.kind = KeyEventKind::Repeat;
        assert_eq!(action(key, Duration::MAX), Action::Ignore);
        key.code = KeyCode::Enter;
        assert_eq!(action(key, Duration::MAX), Action::Ignore);
    }

    #[test]
    fn a_panel_too_small_to_read_does_not_fit_and_scroll_is_bounded() {
        let long = approval("edit", serde_json::json!({"path":"f"}), DIFF, 0);
        let body = content(&long, false);
        assert!(!fits(Rect::new(0, 0, 60, 3), &body));
        assert!(fits(Rect::new(0, 0, 60, 5), &body));
        let rows = u16::try_from(body.line_count(58)).unwrap();
        assert_eq!(max_scroll(Rect::new(0, 0, 60, 5), &body), rows - 3);
        assert_eq!(max_scroll(Rect::new(0, 0, 60, 40), &body), 0);
    }
}
