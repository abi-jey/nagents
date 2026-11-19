use std::collections::HashSet;

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Wrap},
};

use crate::view::{ToolCard, TranscriptPart, duration};

type Key = (String, String);

#[derive(Default)]
pub(super) struct Cards {
    expanded: HashSet<Key>,
    selected: Option<Key>,
    focused: bool,
    reveal: bool,
    headers: Vec<(Key, usize)>,
    height: usize,
    bottom: usize,
    rendered_top: usize,
}

impl Cards {
    pub(super) fn focused(&self) -> bool {
        self.focused
    }

    pub(super) fn blur(&mut self) {
        self.focused = false;
        self.reveal = false;
    }

    pub(super) fn title(&self, interactive: bool) -> &'static str {
        if self.focused && interactive {
            " Tools · ↑/↓ select · Enter/Space toggle · Esc back "
        } else if interactive && !self.headers.is_empty() {
            " Tools · F4 focus "
        } else {
            ""
        }
    }

    // None follows the tail. Otherwise keep the same top row as new rows arrive
    // rather than maintaining a fixed distance from the moving tail.
    pub(super) fn page(&self, scroll: &mut Option<usize>, up: bool) {
        let current = scroll.unwrap_or(self.bottom);
        let top = if up {
            current.saturating_sub(10)
        } else {
            current.saturating_add(10)
        };
        *scroll = (top < self.bottom).then_some(top);
    }

    pub(super) fn key(&mut self, key: KeyEvent, scroll: &mut Option<usize>) -> bool {
        if key.code == KeyCode::F(4) && key.modifiers.is_empty() {
            if self.focused {
                self.blur();
            } else if !self.headers.is_empty() {
                self.focused = true;
                *scroll = Some(self.rendered_top);
                if !self.headers.iter().any(|(key, row)| {
                    Some(key) == self.selected.as_ref()
                        && (self.rendered_top..self.rendered_top + self.height).contains(row)
                }) {
                    self.selected = self.nearest().map(|index| self.headers[index].0.clone());
                }
                self.reveal = true;
            }
            return true;
        }
        if !self.focused || matches!(key.code, KeyCode::F(_)) {
            return false;
        }
        if key.modifiers.is_empty() {
            match key.code {
                KeyCode::Esc => self.blur(),
                KeyCode::Up | KeyCode::Down => {
                    let index = self
                        .headers
                        .iter()
                        .position(|(key, _)| Some(key) == self.selected.as_ref());
                    let next = index
                        .map(|index| {
                            if key.code == KeyCode::Up {
                                index.saturating_sub(1)
                            } else {
                                (index + 1).min(self.headers.len() - 1)
                            }
                        })
                        .or_else(|| self.nearest());
                    self.selected = next.map(|index| self.headers[index].0.clone());
                    self.reveal = true;
                }
                KeyCode::Enter | KeyCode::Char(' ') => {
                    if let Some(key) = &self.selected {
                        if !self.expanded.remove(key) {
                            self.expanded.insert(key.clone());
                        }
                        *scroll = Some(self.rendered_top);
                        self.reveal = true;
                    }
                }
                KeyCode::PageUp | KeyCode::PageDown => {
                    self.page(scroll, key.code == KeyCode::PageUp)
                }
                _ => (),
            }
        }
        // No text or submit/edit shortcuts reach the draft while inspecting.
        true
    }

    fn nearest(&self) -> Option<usize> {
        let end = self.rendered_top + self.height;
        self.headers
            .iter()
            .rposition(|(_, row)| *row < end)
            .or_else(|| (!self.headers.is_empty()).then_some(0))
    }

    pub(super) fn draw(
        &mut self,
        frame: &mut Frame,
        area: Rect,
        layout: Layout<'_>,
        scroll: &mut Option<usize>,
    ) {
        let height = usize::from(area.height);
        self.bottom = layout.rows.saturating_sub(height);
        let header = |headers: &[(Key, usize)]| {
            headers
                .iter()
                .find(|(key, _)| Some(key) == self.selected.as_ref())
                .map(|(_, row)| *row)
        };
        let mut top = scroll.unwrap_or(self.bottom);
        if let Some(new) = header(&layout.headers) {
            if self.reveal && height > 0 {
                // Only reveal the header, never scroll down to fit its body.
                top = top.clamp(new.saturating_sub(height - 1), new);
            } else if scroll.is_some()
                && self.focused
                && let Some(old) = header(&self.headers)
                && (self.rendered_top..self.rendered_top + self.height).contains(&old)
            {
                // An earlier expanded card can grow as its result arrives.
                top = top.saturating_add(new).saturating_sub(old);
            }
        } else {
            // Retention can drop the selected event. Don't pick a different
            // card implicitly, or send the draft on the next Enter.
            self.selected = None;
        }
        self.reveal &= height == 0;
        top = top.min(self.bottom);
        if scroll.is_some() {
            *scroll = Some(top);
        }
        self.rendered_top = top;
        self.height = height;
        layout.render(frame, area, top);
        if !self.expanded.is_empty() {
            let retained: HashSet<_> = layout.headers.iter().map(|(key, _)| key).collect();
            self.expanded.retain(|key| retained.contains(key));
        }
        self.headers = layout.headers;
    }
}

fn clip(text: &str, cells: usize) -> String {
    let span = Span::raw(text);
    if span.width() <= cells {
        return text.to_owned();
    }
    if cells == 0 {
        return String::new();
    }
    let mut remaining = cells - 1;
    let mut clipped = String::new();
    for grapheme in span.styled_graphemes(Style::default()) {
        let width = Span::raw(grapheme.symbol).width();
        if width > remaining {
            break;
        }
        clipped.push_str(grapheme.symbol);
        remaining -= width;
    }
    clipped.push('…');
    clipped
}

fn summary(card: &ToolCard<'_>, marker: &str, width: u16) -> String {
    let cells = usize::from(width);
    let mut prefix = if marker.is_empty() {
        String::new()
    } else if width > 1 {
        format!("{marker} ")
    } else {
        return clip(marker, cells);
    };
    let available = cells.saturating_sub(prefix.len());
    let elapsed = card.elapsed.map(duration);
    let suffix = |status: &str| match elapsed.as_deref() {
        Some(elapsed) => format!("{status} · {elapsed}"),
        _ => status.to_owned(),
    };
    let mut status = if card.status == "Approval waiting" && available >= 36 {
        "Approval waiting · active"
    } else {
        card.status
    };
    let mut outcome = suffix(status);
    if Span::raw(&outcome).width() > available && status == "Approval waiting · active" {
        status = card.status;
        outcome = suffix(status);
    }
    if Span::raw(&outcome).width() > available && status.starts_with("Approval") {
        status = "waiting";
        outcome = suffix(status);
    }
    if Span::raw(&outcome).width() > available
        && let Some(elapsed) = elapsed.as_deref()
        && status.len() + 1 + elapsed.len() <= available
    {
        outcome = format!("{status} {elapsed}");
    }
    let outcome_width = Span::raw(&outcome).width();
    if outcome_width > available {
        prefix.push_str(&clip(status, available));
    } else {
        let label_width = available.saturating_sub(outcome_width + 3);
        if label_width > 0 && !card.label.is_empty() {
            prefix.push_str(&clip(&card.label, label_width));
            prefix.push_str(" · ");
        }
        prefix.push_str(&outcome);
    }
    prefix
}

pub(super) struct Layout<'a> {
    width: u16,
    rows: usize,
    lines: Vec<(usize, Line<'a>)>,
    headers: Vec<(Key, usize)>,
}

impl<'a> Layout<'a> {
    pub(super) fn new(
        parts: &'a [TranscriptPart<'a>],
        width: u16,
        cards: Option<&Cards>,
        interactive: bool,
    ) -> Self {
        let mut layout = Self {
            width,
            rows: 0,
            lines: Vec::new(),
            headers: Vec::new(),
        };
        for part in parts {
            match part {
                TranscriptPart::Text(text) => layout.text(text),
                TranscriptPart::Card(card) => {
                    let key = (card.key.0.to_owned(), card.key.1.to_owned());
                    let expanded = cards.is_some_and(|cards| cards.expanded.contains(&key));
                    let selected = interactive
                        && cards.is_some_and(|cards| {
                            cards.focused && cards.selected.as_ref() == Some(&key)
                        });
                    let marker = if cards.is_none() {
                        ""
                    } else if expanded {
                        "v"
                    } else {
                        ">"
                    };
                    if cards.is_some() {
                        layout.headers.push((key, layout.rows));
                    }
                    let mut line = Line::raw(summary(card, marker, width));
                    if selected {
                        line = line.style(Style::default().add_modifier(Modifier::REVERSED));
                    }
                    layout.push(line);
                    if expanded {
                        for detail in card.details() {
                            for line in detail.lines() {
                                layout.push(Line::raw(line.to_owned()));
                            }
                        }
                    }
                }
            }
        }
        layout
    }

    pub(super) fn text(&mut self, text: &'a str) {
        for mut line in text.lines() {
            // Keep any one logical line below Paragraph's u16 offset even in
            // a one-column viewport. Ordinary prose uses native word wrapping.
            while line.len() > 24_000 {
                let mut end = 24_000;
                while !line.is_char_boundary(end) {
                    end -= 1;
                }
                let (chunk, rest) = line.split_at(end);
                self.push(Line::raw(chunk));
                line = rest;
            }
            self.push(Line::raw(line));
        }
    }

    pub(super) fn push(&mut self, line: Line<'a>) {
        if self.width == 0 {
            return;
        }
        let height = if line.width() <= usize::from(self.width) {
            1
        } else {
            paragraph(line.clone()).line_count(self.width)
        };
        self.lines.push((self.rows, line));
        self.rows += height;
    }

    pub(super) fn draw_from_bottom(&self, frame: &mut Frame, area: Rect, scroll: u16) {
        self.render(
            frame,
            area,
            self.rows
                .saturating_sub(usize::from(area.height) + usize::from(scroll)),
        );
    }

    fn render(&self, frame: &mut Frame, area: Rect, top: usize) {
        if area.is_empty() || self.lines.is_empty() {
            return;
        }
        let start = self
            .lines
            .partition_point(|(row, _)| *row <= top)
            .saturating_sub(1);
        let end = self
            .lines
            .partition_point(|(row, _)| *row < top + usize::from(area.height));
        let (first_row, _) = &self.lines[start];
        let skip = (top - first_row) as u16;
        let visible = self.lines[start..end]
            .iter()
            .map(|(_, line)| line.clone())
            .collect::<Vec<_>>();
        let area = Rect {
            height: area.height.min(u16::MAX - skip),
            ..area
        };
        frame.render_widget(paragraph(visible).scroll((skip, 0)), area);
    }
}

fn paragraph<'a>(text: impl Into<ratatui::text::Text<'a>>) -> Paragraph<'a> {
    Paragraph::new(text).wrap(Wrap { trim: false })
}

#[cfg(test)]
mod tests {
    use crossterm::event::KeyModifiers;
    use ngn_core::host::Approval;
    use ngn_protocol::{Event, EventRecord, ToolCall, ToolResult};
    use ratatui::{Terminal, backend::TestBackend};
    use serde_json::json;

    use super::*;
    use crate::{tui::App, view};

    fn event(run: &str, event: Event) -> EventRecord {
        EventRecord {
            schema_version: 1,
            sequence: 1,
            session_id: "test".into(),
            run_id: run.into(),
            timestamp_ms: 100,
            event,
        }
    }

    fn call(id: &str, command: &str) -> ToolCall {
        ToolCall {
            call_id: id.into(),
            name: "shell".into(),
            arguments: json!({"command": command}),
        }
    }

    fn started(run: &str, id: &str, command: &str) -> EventRecord {
        event(
            run,
            Event::ToolStarted {
                call: call(id, command),
            },
        )
    }

    fn finished(run: &str, id: &str, result: serde_json::Value, error: &str) -> EventRecord {
        event(
            run,
            Event::ToolCompleted {
                result: ToolResult {
                    call_id: id.into(),
                    name: "shell".into(),
                    result,
                    error: error.into(),
                },
                duration_ms: 1250,
            },
        )
    }

    fn key(app: &mut App, code: KeyCode) -> bool {
        app.tool_card_key(KeyEvent::new(code, KeyModifiers::NONE))
    }

    fn draw(app: &mut App, terminal: &mut Terminal<TestBackend>) -> Vec<String> {
        terminal
            .draw(|frame| {
                let area = frame.area();
                let parts = view::transcript(&app.records, 1500);
                let layout =
                    Layout::new(&parts, area.width, Some(&app.tools), app.tools_available());
                app.tools.draw(frame, area, layout, &mut app.scroll);
            })
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content
            .chunks(terminal.size().unwrap().width as usize)
            .map(|row| row.iter().map(|cell| cell.symbol()).collect())
            .collect()
    }

    #[test]
    fn card_focus_and_disclosure_are_independent_by_run_and_call() {
        let mut app = App {
            records: vec![
                started("r1", "shared", "first"),
                started("r1", "other", "neighbor"),
                started("r2", "shared", "second"),
                finished("r2", "shared", json!("output-second"), ""),
            ],
            input: "/se unsent draft".into(),
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(60, 20)).unwrap();
        assert_eq!(
            draw(&mut app, &mut terminal)
                .iter()
                .filter(|row| row.starts_with("> "))
                .count(),
            3
        );
        assert!(!app.tools.focused());
        assert!(!key(&mut app, KeyCode::Enter)); // Ordinary Enter still reaches the composer.
        assert!(key(&mut app, KeyCode::F(4)));
        assert!(key(&mut app, KeyCode::Enter)); // This Enter cannot submit.
        let rows = draw(&mut app, &mut terminal).join("\n");
        assert!(rows.contains("> shell · first"));
        assert!(rows.contains("> shell · neighbor"));
        assert!(rows.contains("v shell · second · complete · 1.25s"));
        assert!(rows.contains("Result: \"output-second\""));
        assert_eq!(rows.matches("Arguments:").count(), 1);
        let selected = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .find(|cell| cell.symbol() == "v")
            .unwrap();
        assert!(selected.modifier.contains(Modifier::REVERSED));

        app.records.push(started("r3", "new", "latest"));
        draw(&mut app, &mut terminal);
        assert_eq!(
            app.tools.selected.as_ref().map(|key| key.0.as_str()),
            Some("r2")
        );
        assert!(key(&mut app, KeyCode::Up));
        assert!(key(&mut app, KeyCode::Up));
        assert!(key(&mut app, KeyCode::Char(' ')));
        let rows = draw(&mut app, &mut terminal).join("\n");
        assert!(rows.contains("v shell · first"));
        assert!(rows.contains("v shell · second"));
        assert!(rows.contains("> shell · neighbor"));
        assert!(key(&mut app, KeyCode::Enter));
        assert!(key(&mut app, KeyCode::Esc));
        assert!(!key(&mut app, KeyCode::Char(' ')));
        let rows = draw(&mut app, &mut terminal).join("\n");
        assert!(rows.contains("> shell · first"));
        assert!(rows.contains("v shell · second"));
        assert!(
            !terminal
                .backend()
                .buffer()
                .content
                .iter()
                .any(|cell| cell.modifier.contains(Modifier::REVERSED))
        );
        assert_eq!(app.input, "/se unsent draft");
    }

    #[test]
    fn overlays_and_approval_suspend_card_keys() {
        let mut app = App {
            records: vec![started("r", "c", "test")],
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(60, 20)).unwrap();
        draw(&mut app, &mut terminal);
        app.help = true;
        assert!(key(&mut app, KeyCode::F(4)));
        assert!(!app.tools.focused());
        app.help = false;
        assert!(key(&mut app, KeyCode::F(4)));
        app.approval = Some(Approval {
            id: "approval".into(),
            run_id: "r".into(),
            session_id: "test".into(),
            root_session_id: "test".into(),
            agent_title: "Main".into(),
            depth: 0,
            call: call("c", "test"),
            preview: String::new(),
        });
        assert!(key(&mut app, KeyCode::F(4)));
        assert!(key(&mut app, KeyCode::Enter));
        assert!(app.tools.focused());
        assert!(app.tools.expanded.is_empty());
        app.approval = None;
        assert!(key(&mut app, KeyCode::Enter));
        assert_eq!(app.tools.expanded.len(), 1);
    }

    #[test]
    fn narrow_headers_preserve_outcome_and_time_and_details_are_bounded() {
        let mut app = App {
            records: vec![
                started("r", "c", &"工作".repeat(3000)),
                finished(
                    "r",
                    "c",
                    json!({"output": "果".repeat(5000)}),
                    &format!("bad\x1b[2J{}\nEND", "錯".repeat(5000)),
                ),
            ],
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(30, 6)).unwrap();
        let rows = draw(&mut app, &mut terminal);
        assert!(rows[1].starts_with("> shell"));
        assert!(rows[1].contains("工"));
        assert!(rows[1].contains("failed · 1.25s"));
        assert!(rows[2].trim().is_empty()); // Header never wraps.
        assert!(key(&mut app, KeyCode::F(4)));
        assert!(key(&mut app, KeyCode::Enter));
        let rows = draw(&mut app, &mut terminal);
        assert!(rows[1].starts_with("v shell"));
        assert!(rows[2].contains("Arguments:"));
        assert_eq!(app.tools.rendered_top, 0);
        let parts = view::transcript(&app.records, 0);
        let TranscriptPart::Card(card) = &parts[1] else {
            panic!("expected card")
        };
        let details: Vec<_> = card.details().collect();
        assert_eq!(details.len(), 3);
        assert!(
            details
                .iter()
                .all(|detail| detail.len() < 4128 && detail.ends_with("[truncated]"))
        );
        assert!(details[2].contains("bad[2J"));
        assert!(!details[2].contains('\x1b'));
        assert!(!details[2].contains("END"));
        let mut tiny = Terminal::new(TestBackend::new(2, 4)).unwrap();
        draw(&mut app, &mut tiny);
    }

    #[test]
    fn paging_clamps_and_append_does_not_move_the_reading_position() {
        let mut app = App {
            records: vec![
                event(
                    "r",
                    Event::TextDelta {
                        text: (0..40).map(|row| format!("history {row}\n")).collect(),
                    },
                ),
                started("r", "c", "tail"),
            ],
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(60, 6)).unwrap();
        draw(&mut app, &mut terminal);
        let bottom = app.tools.bottom;
        app.tools.page(&mut app.scroll, true);
        let before = draw(&mut app, &mut terminal);
        assert_eq!(app.scroll, Some(bottom - 10));
        app.records.push(event(
            "r",
            Event::TextDelta {
                text: "live output\n".repeat(20),
            },
        ));
        assert_eq!(draw(&mut app, &mut terminal), before);
        for _ in 0..20 {
            app.tools.page(&mut app.scroll, true);
        }
        assert!(draw(&mut app, &mut terminal)[0].starts_with("history 0"));
        assert_eq!(app.scroll, Some(0));
        for _ in 0..20 {
            app.tools.page(&mut app.scroll, false);
        }
        assert_eq!(app.scroll, None);
        assert!(draw(&mut app, &mut terminal)[5].starts_with("live output"));
    }
}
