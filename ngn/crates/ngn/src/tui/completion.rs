use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
};

#[derive(Clone, Copy)]
pub(super) enum Command {
    Exit,
    Help,
    New,
    Queue,
    Resume,
    Sessions,
    Steer,
    Steering,
    Tasks,
}

// Also used by submit: don't suggest commands the native TUI cannot run.
const COMMANDS: &[(&str, Command, &str)] = &[
    ("/help", Command::Help, "Show keyboard shortcuts"),
    ("/new", Command::New, "New conversation"),
    (
        "/queue",
        Command::Queue,
        "[pause|resume|clear|retry|verify] Input queue",
    ),
    ("/resume", Command::Resume, "[ID] Resume a conversation"),
    ("/sessions", Command::Sessions, "Find a conversation"),
    ("/steer", Command::Steer, "TEXT  Steer the active turn"),
    ("/steering", Command::Steering, "Inspect steering receipts"),
    ("/tasks", Command::Tasks, "Inspect subagents"),
    ("/quit", Command::Exit, "Quit"),
    ("/exit", Command::Exit, "Quit (alias)"),
];

impl Command {
    pub(super) fn lookup(name: &str) -> Option<Self> {
        COMMANDS
            .iter()
            .find_map(|(token, command, _)| (*token == name).then_some(*command))
    }
}

#[derive(Default)]
pub(super) struct Completion {
    // Keep the same command selected through filtering when it still matches.
    selected: Option<&'static str>,
    dismissed: bool,
}

pub(super) struct Menu {
    rows: Vec<(&'static str, &'static str)>,
    selected: usize,
}

impl Completion {
    pub(super) fn menu(&self, draft: &str) -> Option<Menu> {
        // The native composer only appends, so its cursor is always at the end.
        // Like Python, don't offer commands on a later line of a pasted draft.
        let token = draft.trim_start_matches(|c: char| c != '\n' && c.is_whitespace());
        if self.dismissed || !token.starts_with('/') || token.contains(char::is_whitespace) {
            return None;
        }
        // The native command names are ASCII, with exact spelling on submit.
        let token = token.to_ascii_lowercase();
        let rows: Vec<_> = COMMANDS
            .iter()
            .filter(|(name, _, _)| name.starts_with(&token))
            .map(|&(name, _, description)| (name, description))
            .collect();
        let selected = rows
            .iter()
            .position(|(name, _)| Some(*name) == self.selected)
            .unwrap_or(0);
        Some(Menu { rows, selected })
    }

    pub(super) fn edited(&mut self, draft: &str) {
        self.dismissed = false;
        self.selected = self
            .menu(draft)
            .and_then(|menu| menu.rows.get(menu.selected).map(|(name, _)| *name));
    }

    pub(super) fn key(&mut self, draft: &mut String, key: KeyEvent) -> bool {
        if !key.modifiers.is_empty()
            || !matches!(
                key.code,
                KeyCode::Esc | KeyCode::Up | KeyCode::Down | KeyCode::Tab | KeyCode::Enter
            )
        {
            return false;
        }
        let Some(menu) = self.menu(draft) else {
            return false;
        };
        match key.code {
            KeyCode::Esc => self.dismissed = true,
            KeyCode::Up | KeyCode::Down => {
                let index = if key.code == KeyCode::Up {
                    menu.selected.saturating_sub(1)
                } else {
                    (menu.selected + 1).min(menu.rows.len().saturating_sub(1))
                };
                self.selected = menu.rows.get(index).map(|(name, _)| *name);
            }
            KeyCode::Tab | KeyCode::Enter => {
                let Some((name, _)) = menu.rows.get(menu.selected) else {
                    return false;
                };
                draft.clear();
                draft.push_str(name);
                draft.push(' ');
                self.edited(draft);
            }
            _ => return false,
        }
        true
    }
}

impl Menu {
    pub(super) fn height(&self) -> u16 {
        1 + self.rows.len().min(4) as u16
    }

    pub(super) fn draw(&self, frame: &mut Frame, area: Rect, accent: Color, muted: Color) {
        if area.height == 0 {
            return;
        }
        let hint = if self.rows.is_empty() {
            " No matching commands · Esc close"
        } else {
            " ↑↓ select · Tab/Enter fill · Esc close"
        };
        let count = usize::from(area.height - 1);
        let start = self.selected.saturating_sub(count.saturating_sub(1));
        let mut lines = vec![Line::styled(hint, Style::new().fg(muted))];
        for (index, &(name, description)) in self.rows.iter().enumerate().skip(start).take(count) {
            let selected = index == self.selected;
            let line = Line::from(vec![
                Span::styled(
                    format!("{} {name}", if selected { "›" } else { " " }),
                    Style::new().fg(accent).add_modifier(Modifier::BOLD),
                ),
                Span::raw(format!("  {description}")),
            ]);
            lines.push(if selected {
                line.style(Style::new().add_modifier(Modifier::REVERSED))
            } else {
                line
            });
        }
        frame.render_widget(Paragraph::new(lines), area);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;

    #[test]
    fn duplicate_prefix_navigation_and_acceptance() {
        let mut completion = Completion::default();
        let mut draft = "/st".to_owned();
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let menu = completion.menu(&draft).unwrap();
        let names: Vec<_> = menu.rows.iter().map(|(name, _)| *name).collect();
        assert_eq!(names, ["/steer", "/steering"]);
        assert_eq!(menu.selected, 0);
        assert!(completion.key(&mut draft, key(KeyCode::Down)));
        assert_eq!(draft, "/st");
        draft.push('e');
        completion.edited(&draft);
        assert_eq!(completion.menu(&draft).unwrap().selected, 1);
        assert!(completion.key(&mut draft, key(KeyCode::Enter)));
        assert_eq!(draft, "/steering ");
        assert!(!completion.key(&mut draft, key(KeyCode::Enter)));

        draft = "/st".into();
        completion.edited(&draft);
        assert!(completion.key(&mut draft, key(KeyCode::Up)));
        assert!(completion.key(&mut draft, key(KeyCode::Tab)));
        assert_eq!(draft, "/steer ");
    }

    #[test]
    fn no_match_dismissal_and_slash_token_eligibility() {
        let mut completion = Completion::default();
        let mut draft = "/xyz".to_owned();
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        assert!(completion.menu(&draft).unwrap().rows.is_empty());
        assert!(!completion.key(&mut draft, key(KeyCode::Tab)));
        assert!(!completion.key(&mut draft, key(KeyCode::Enter)));
        assert!(completion.key(&mut draft, key(KeyCode::Esc)));
        assert_eq!(draft, "/xyz");
        assert!(completion.menu(&draft).is_none());
        assert!(!completion.key(&mut draft, key(KeyCode::Esc)));

        draft = "  /SE".into();
        completion.edited(&draft);
        assert!(completion.key(&mut draft, key(KeyCode::Tab)));
        assert_eq!(draft, "/sessions ");
        for draft in [
            "ordinary prompt",
            r"\/se",
            "/resume id",
            "/steer\ntext",
            "\n/se",
        ] {
            assert!(completion.menu(draft).is_none(), "{draft}");
        }
    }
}
