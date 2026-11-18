use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ngn_core::store::{Session, now_ms};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::Line,
    widgets::{Block, Clear, List, ListItem, ListState, Paragraph},
};

use super::{center, view};

const NEW: &str = "+ New session";
const FILTER_LIMIT: usize = 32_000;

#[derive(Clone, Copy)]
enum Row {
    New,
    Existing(usize),
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum Choice {
    Close,
    New,
    Existing(String),
}

pub(super) struct Picker {
    sessions: Vec<Session>,
    filter: String,
    rows: Vec<Row>,
    selected: usize,
}

impl Picker {
    pub(super) fn new(sessions: Vec<Session>) -> Self {
        let mut picker = Self {
            sessions,
            filter: String::new(),
            rows: Vec::new(),
            selected: 0,
        };
        picker.refilter();
        picker
    }

    fn refilter(&mut self) {
        let query = fold(&self.filter);
        self.rows.clear();
        if NEW.to_ascii_lowercase().contains(&query) {
            self.rows.push(Row::New);
        }
        self.rows.extend(
            self.sessions
                .iter()
                .enumerate()
                .filter(|(_, session)| {
                    fold(&one_line(title(session))).contains(&query)
                        || fold(&session.id).contains(&query)
                })
                .map(|(index, _)| Row::Existing(index)),
        );
        // Like Python's picker, an edit resets selection to the first match.
        self.selected = 0;
    }

    fn append(&mut self, text: &str) {
        if !text.is_empty() && self.filter.len() + text.len() <= FILTER_LIMIT {
            self.filter.push_str(text);
            self.refilter();
        }
    }

    pub(super) fn paste(&mut self, text: &str) {
        let text = text.replace("\r\n", "\n").replace(['\r', '\n', '\t'], " ");
        self.append(&view::plain(&text));
    }

    pub(super) fn key(&mut self, key: KeyEvent) -> Option<Choice> {
        match key.code {
            KeyCode::Esc => return Some(Choice::Close),
            KeyCode::Up => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down => {
                self.selected = (self.selected + 1).min(self.rows.len().saturating_sub(1))
            }
            KeyCode::Enter => {
                return self.rows.get(self.selected).map(|row| match *row {
                    Row::New => Choice::New,
                    Row::Existing(index) => Choice::Existing(self.sessions[index].id.clone()),
                });
            }
            KeyCode::Backspace => {
                if self.filter.pop().is_some() {
                    self.refilter();
                }
            }
            KeyCode::Char(character)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                    && !character.is_control() =>
            {
                self.append(character.encode_utf8(&mut [0; 4]));
            }
            _ => (),
        }
        None
    }

    pub(super) fn draw(&self, frame: &mut Frame, accent: Color, muted: Color) {
        let area = center(frame.area(), 62, 18);
        let block = Block::bordered().title(" Conversations ");
        let inner = block.inner(area);
        frame.render_widget(Clear, area);
        frame.render_widget(block, area);
        if inner.is_empty() {
            return;
        }

        let field = Rect::new(inner.x, inner.y, inner.width, 1);
        let prefix = if field.width >= 10 { " Filter: " } else { "/" };
        let prefix_width = (prefix.len() as u16).min(field.width - 1);
        frame.render_widget(
            Line::styled(prefix, Style::new().fg(accent)),
            Rect::new(field.x, field.y, prefix_width, 1),
        );
        let field = Rect::new(
            field.x + prefix_width,
            field.y,
            field.width - prefix_width,
            1,
        );
        let cursor = if self.filter.is_empty() {
            frame.render_widget(
                Line::styled("type to search", Style::new().fg(muted)),
                field,
            );
            0
        } else {
            let query = Line::raw(&self.filter);
            let width = query.width() as u16;
            let offset = width.saturating_sub(field.width.saturating_sub(1));
            frame.render_widget(Paragraph::new(query).scroll((0, offset)), field);
            width - offset
        };
        frame.set_cursor_position((field.x + cursor, field.y));

        let hint = u16::from(inner.height >= 3);
        let list_area = Rect::new(inner.x, inner.y + 1, inner.width, inner.height - 1 - hint);
        if self.rows.is_empty() {
            frame.render_widget(
                Paragraph::new(" No matching sessions").style(Style::new().fg(muted)),
                list_area,
            );
        } else {
            let now = now_ms();
            let items = self.rows.iter().map(|row| match *row {
                Row::New => ListItem::new(NEW),
                Row::Existing(index) => {
                    let session = &self.sessions[index];
                    let id: String = session.id.chars().take(12).collect();
                    let id = one_line(&id);
                    let age = if inner.width >= 46 && session.updated_at_ms != 0 {
                        format!("{} · ", age(now.saturating_sub(session.updated_at_ms)))
                    } else {
                        String::new()
                    };
                    ListItem::new(format!("{id} · {age}{}", one_line(title(session))))
                }
            });
            let mut state = ListState::default().with_selected(Some(self.selected));
            frame.render_stateful_widget(
                List::new(items)
                    .highlight_symbol("› ")
                    .highlight_style(Style::new().fg(accent).add_modifier(Modifier::BOLD)),
                list_area,
                &mut state,
            );
        }
        if hint != 0 {
            frame.render_widget(
                Paragraph::new(" ↑/↓ select · Enter open · Esc back").style(Style::new().fg(muted)),
                Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
            );
        }
    }
}

fn title(session: &Session) -> &str {
    if session.title.trim().is_empty() {
        "Untitled"
    } else {
        &session.title
    }
}

fn one_line(text: &str) -> String {
    view::plain(text).replace(['\n', '\t'], " ")
}

fn fold(text: &str) -> String {
    // Use full, per-character casing (no contextual final sigma). Default
    // Unicode folding keeps dotless i distinct, expands capital sharp S, and
    // uses uppercase for Cherokee.
    let mut folded = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            'ı' => folded.push(ch),
            'ẞ' => folded.push_str("ss"),
            '\u{13a0}'..='\u{13ff}' | '\u{ab70}'..='\u{abbf}' => folded.extend(ch.to_uppercase()),
            _ => folded.extend(ch.to_uppercase().flat_map(char::to_lowercase)),
        }
    }
    folded
}

fn age(elapsed_ms: u64) -> String {
    match elapsed_ms / 60_000 {
        0 => "now".into(),
        minutes @ 1..60 => format!("{minutes}m ago"),
        minutes @ 60..1_440 => format!("{}h ago", minutes / 60),
        minutes => format!("{}d ago", minutes / 1_440),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};

    fn press(picker: &mut Picker, code: KeyCode) -> Option<Choice> {
        picker.key(KeyEvent::new(code, KeyModifiers::NONE))
    }

    fn render(picker: &Picker, terminal: &mut Terminal<TestBackend>) -> String {
        terminal
            .draw(|frame| picker.draw(frame, Color::Green, Color::Gray))
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect()
    }

    #[test]
    fn resize_keeps_filter_and_selected_row_visible_then_edits_reset_selection() {
        let mut picker = Picker::new(
            (0..30)
                .map(|index| Session {
                    id: format!("ngn-{index:08x}-full-id"),
                    title: format!("Straße {index}"),
                    updated_at_ms: now_ms().saturating_sub(7_500_000),
                })
                .collect(),
        );
        picker.paste("STRASSE");
        for _ in 0..29 {
            assert_eq!(press(&mut picker, KeyCode::Down), None);
        }
        let mut terminal = Terminal::new(TestBackend::new(120, 36)).unwrap();
        let wide = render(&picker, &mut terminal);
        assert!(wide.contains("Filter: STRASSE"));
        assert!(wide.contains("› ngn-0000001d · 2h ago · Straße 29"));
        // The desktop picker is compact rather than spanning the full terminal.
        assert_eq!(terminal.backend().buffer()[(29, 9)].symbol(), "┌");

        terminal.backend_mut().resize(40, 12);
        let narrow = render(&picker, &mut terminal);
        assert!(narrow.contains("Filter: STRASSE"));
        assert!(narrow.contains("› ngn-0000001d · Straße 29"));
        assert_eq!(
            press(&mut picker, KeyCode::Enter),
            Some(Choice::Existing("ngn-0000001d-full-id".into()))
        );

        picker.paste(" 1");
        assert!(render(&picker, &mut terminal).contains("› ngn-00000001 · Straße 1"));
        assert_eq!(press(&mut picker, KeyCode::Char('x')), None);
        assert!(render(&picker, &mut terminal).contains("No matching sessions"));
        assert_eq!(press(&mut picker, KeyCode::Enter), None);
        press(&mut picker, KeyCode::Backspace);
        assert_eq!(
            press(&mut picker, KeyCode::Enter),
            Some(Choice::Existing("ngn-00000001-full-id".into()))
        );
    }

    #[test]
    fn empty_store_offers_new_and_no_match_is_inert() {
        let mut picker = Picker::new(Vec::new());
        let mut terminal = Terminal::new(TestBackend::new(60, 20)).unwrap();
        assert!(render(&picker, &mut terminal).contains("› + New session"));
        assert_eq!(press(&mut picker, KeyCode::Enter), Some(Choice::New));
        picker.paste("other");
        assert!(render(&picker, &mut terminal).contains("No matching sessions"));
        press(&mut picker, KeyCode::Down);
        assert_eq!(press(&mut picker, KeyCode::Enter), None);
        terminal.backend_mut().resize(1, 1);
        render(&picker, &mut terminal);
    }

    #[test]
    fn unicode_search_folds_expansions_and_sigma_but_not_dotless_i() {
        assert_eq!(
            fold("Straße ẞ Σσς Ꮟꮟ ﬁ İı"),
            "strasse ss σσσ ᏏᏏ fi i\u{307}ı"
        );
    }
}
