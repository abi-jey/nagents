use std::{
    collections::{HashMap, HashSet},
    time::{Duration, Instant},
};

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    Frame,
    buffer::Buffer,
    layout::{Position, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Wrap},
};

use crate::view::{FenceKey, ToolCard, ToolKey, TranscriptPart, duration};

use super::{
    code_fences,
    shimmer::{COMPLETE, COMPLETION_DURATION, Sweep, Wipe},
};

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
    code: code_fences::Pan,
    sweep: Option<(Position, Key, HeaderCells, Duration, Instant)>,
    sweep_painted_at: Option<Instant>,
    animated: bool,
    seen: i64,
    completions: HashMap<Key, Completion>,
    wipes: Vec<(Position, HeaderCells, Completion)>,
}

#[derive(Clone, Copy)]
struct Completion {
    started: Instant,
    source: Option<(Sweep, usize)>,
}

struct HeaderCells {
    count: usize,
    // (terminal column, character index, width). A wide or combining grapheme is
    // painted once, but the wave follows legacy character indices.
    cells: Vec<(u16, usize, u16)>,
}

impl HeaderCells {
    fn new(title: &str) -> Self {
        let mut column = 0;
        let mut count = 0;
        let mut cells = Vec::new();
        for grapheme in Span::raw(title).styled_graphemes(Style::default()) {
            let width = Span::raw(grapheme.symbol).width() as u16;
            if width != 0 {
                cells.push((column, count, width));
                column += width;
            }
            count += grapheme.symbol.chars().count();
        }
        Self { count, cells }
    }

    fn mask(&mut self, origin: Position, area: Rect) {
        if !area.is_empty() && (area.top()..area.bottom()).contains(&origin.y) {
            // Don't repaint a wide glyph straddling the dialog border. Keep
            // the original character indices and count for the remaining ink.
            self.cells.retain(|&(column, _, width)| {
                let left = origin.x + column;
                left >= area.right() || left + width <= area.left()
            });
        }
    }
}

impl Cards {
    pub(super) fn new(animated: bool) -> Self {
        Self {
            animated,
            ..Self::default()
        }
    }

    pub(super) fn reset(&mut self) {
        *self = Self::new(self.animated);
    }

    pub(super) fn skip_completions(&mut self, after: i64) {
        self.seen = after;
    }

    pub(super) fn observe_completions(
        &mut self,
        parts: &[TranscriptPart<'_>],
        after: i64,
        now: Instant,
    ) {
        if !self.animated {
            return;
        }
        self.completions
            .retain(|_, completion| now < completion.started + COMPLETION_DURATION);
        if after <= self.seen {
            return;
        }
        for part in parts {
            if let TranscriptPart::Card(card) = part
                && card.success_transition.is_some_and(|seq| seq > self.seen)
            {
                let key = (card.key.0.to_owned(), card.key.1.to_owned());
                let source = self.sweep.as_ref().and_then(|(_, active, cells, age, at)| {
                    let painted_at = self.sweep_painted_at?;
                    (active == &key).then(|| {
                        (
                            Sweep::new(
                                cells.count,
                                *age + painted_at.saturating_duration_since(*at),
                            ),
                            cells.count,
                        )
                    })
                });
                self.completions.insert(
                    key,
                    Completion {
                        started: now,
                        source,
                    },
                );
            }
        }
        self.seen = after;
    }

    pub(super) fn needs_frame(&self, now: Instant) -> bool {
        self.wipes
            .iter()
            .any(|(_, _, completion)| now < completion.started + COMPLETION_DURATION)
    }

    pub(super) fn focused(&self) -> bool {
        self.focused || self.code.focused()
    }

    pub(super) fn blur(&mut self) {
        self.focused = false;
        self.reveal = false;
        self.code.blur();
    }

    pub(super) fn title(&self, interactive: bool) -> &'static str {
        if self.focused && interactive {
            " Tools · ↑/↓ select · Enter/Space toggle · Esc back "
        } else if self.code.focused() && interactive {
            " Code · ←/→ pan · ↑/↓ select · Esc back "
        } else if interactive && !self.headers.is_empty() && self.code.can_pan() {
            " Tools F4 · Code F8 "
        } else if interactive && !self.headers.is_empty() {
            " Tools · F4 focus "
        } else if interactive && self.code.can_pan() {
            " Code · F8 pan "
        } else {
            ""
        }
    }

    pub(super) fn page(&self, scroll: &mut Option<usize>, up: bool) {
        self.scroll_rows(scroll, up, 10);
    }

    // None follows the tail. Otherwise keep the same top row as new rows arrive
    // rather than maintaining a fixed distance from the moving tail.
    pub(super) fn scroll_rows(&self, scroll: &mut Option<usize>, up: bool, rows: usize) {
        let current = scroll.unwrap_or(self.bottom);
        let top = if up {
            current.saturating_sub(rows)
        } else {
            current.saturating_add(rows)
        };
        *scroll = (top < self.bottom).then_some(top);
    }

    pub(super) fn key(&mut self, key: KeyEvent, scroll: &mut Option<usize>) -> bool {
        if key.code == KeyCode::F(4) && key.modifiers.is_empty() {
            if self.focused {
                self.blur();
            } else if !self.headers.is_empty() {
                self.code.blur();
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
        if !self.focused
            && self
                .code
                .key(key, self.rendered_top..self.rendered_top + self.height)
        {
            if self.code.focused() && key.code == KeyCode::F(8) {
                *scroll = Some(self.rendered_top);
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
        mut layout: Layout<'_>,
        scroll: &mut Option<usize>,
        now: Instant,
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
        self.code.observe(std::mem::take(&mut layout.fences));
        top = self.code.reveal(top, height).min(self.bottom);
        if scroll.is_some() {
            *scroll = Some(top);
        }
        self.rendered_top = top;
        self.height = height;
        layout.render(frame, area, top, Some(&self.code), layout.interactive);
        let origin = |row, column| {
            ((top..top + height).contains(&row) && !area.is_empty())
                .then(|| Position::new(area.x + column, area.y + (row - top) as u16))
        };
        self.sweep = layout.sweep.and_then(|(row, key, cells, age)| {
            origin(row, 0).map(|origin| (origin, key, cells, age, now))
        });
        self.wipes = layout
            .wipes
            .into_iter()
            .filter_map(|(row, mut cells, completion)| {
                let origin = origin(row, DISCLOSURE_WIDTH as u16)?;
                if let Some(area) = layout.wipe_mask {
                    cells.mask(origin, area);
                }
                (!cells.cells.is_empty()).then_some((origin, cells, completion))
            })
            .collect();
        self.paint_motion(frame.buffer_mut(), now);
        if !self.expanded.is_empty() {
            let retained: HashSet<_> = layout.headers.iter().map(|(key, _)| key).collect();
            self.expanded.retain(|key| retained.contains(key));
        }
        self.headers = layout.headers;
    }

    pub(super) fn paint_motion(&mut self, buffer: &mut Buffer, now: Instant) {
        self.sweep_painted_at = None;
        if let Some((origin, _, header, age, observed_at)) = &self.sweep {
            let age = age.saturating_add(now.saturating_duration_since(*observed_at));
            let sweep = Sweep::new(header.count, age);
            for &(column, index, _) in &header.cells {
                buffer[(origin.x + column, origin.y)].fg = sweep.color(index);
            }
            self.sweep_painted_at = Some(now);
        }
        for (origin, header, completion) in &self.wipes {
            let wipe = Wipe::new(
                header.count,
                now.saturating_duration_since(completion.started),
            );
            for &(column, index, _) in &header.cells {
                let prior_index = index + DISCLOSURE_WIDTH;
                let source = completion
                    .source
                    .filter(|(_, count)| prior_index < *count)
                    .map_or(Color::Reset, |(sweep, _)| sweep.color(prior_index));
                buffer[(origin.x + column, origin.y)].fg = wipe.color(index, source);
            }
        }
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

// The disclosure glyph and its following space are not part of the Python label.
const DISCLOSURE_WIDTH: usize = 2;

pub(super) struct Layout<'a> {
    width: u16,
    rows: usize,
    lines: Vec<(usize, Row<'a>)>,
    headers: Vec<(Key, usize)>,
    sweep: Option<(usize, Key, HeaderCells, Duration)>,
    wipes: Vec<(usize, HeaderCells, Completion)>,
    wipe_mask: Option<Rect>,
    fences: Vec<code_fences::Fence>,
    interactive: bool,
}

enum Row<'a> {
    Plain(Line<'a>),
    Code(FenceKey, &'a str),
}

impl<'a> Layout<'a> {
    pub(super) fn new(
        parts: &'a [TranscriptPart<'a>],
        width: u16,
        cards: Option<&Cards>,
        interactive: bool,
        active: Option<ToolKey<'_>>,
    ) -> Self {
        let mut layout = Self {
            width,
            rows: 0,
            lines: Vec::new(),
            headers: Vec::new(),
            sweep: None,
            fences: Vec::new(),
            interactive,
            wipes: Vec::new(),
            wipe_mask: None,
        };
        for part in parts {
            match part {
                TranscriptPart::Text(text) => layout.text(text),
                TranscriptPart::Code(key, text) => layout.code(*key, text, cards.is_some()),
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
                    let mut style = Style::default();
                    if cards.is_some() {
                        style = match card.status {
                            "failed" | "denied" => style.fg(Color::Red),
                            "cancelled" => style.fg(Color::Magenta),
                            _ => style,
                        };
                        if selected {
                            style = style.add_modifier(Modifier::UNDERLINED);
                        }
                    }
                    let title = summary(card, marker, width);
                    if cards.is_some()
                        && Some(card.key) == active
                        && card.status == "running"
                        && let Some(elapsed) = card.elapsed
                    {
                        layout.sweep = Some((
                            layout.rows,
                            key.clone(),
                            HeaderCells::new(&title),
                            Duration::from_millis(elapsed),
                        ));
                    }
                    let line = if let Some(cards) = cards
                        && card.success
                        && let Some(label) = title.get(DISCLOSURE_WIDTH..).filter(|s| !s.is_empty())
                    {
                        if let Some(completion) = cards.completions.get(&key) {
                            layout
                                .wipes
                                .push((layout.rows, HeaderCells::new(label), *completion));
                        }
                        Line::from(vec![
                            Span::raw(title[..DISCLOSURE_WIDTH].to_owned()),
                            Span::styled(label.to_owned(), Style::new().fg(COMPLETE)),
                        ])
                    } else {
                        Line::raw(title)
                    };
                    if cards.is_some() {
                        layout.headers.push((key, layout.rows));
                    }
                    layout.push(line.style(style));
                    if expanded {
                        for detail in card.details() {
                            layout.detail(&detail);
                        }
                    } else if cards.is_none()
                        && let Some(error) = card.error_detail()
                    {
                        // The read-only agent inspector cannot disclose this card.
                        layout.detail(&error);
                    }
                }
            }
        }
        layout
    }

    pub(super) fn hide_wipes(&mut self) {
        self.wipes.clear();
    }

    pub(super) fn mask_wipes(&mut self, area: Rect) {
        // Cached motion frames reuse the already composited dialog. Never
        // repaint its cells, but keep the same clock on the exposed label.
        self.wipe_mask = Some(area);
    }

    fn detail(&mut self, text: &str) {
        for line in text.lines() {
            self.push(Line::raw(line.to_owned()));
        }
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

    fn code(&mut self, key: FenceKey, text: &'a str, track: bool) {
        if self.width == 0 {
            return;
        }
        let start = self.rows;
        let mut width = 0;
        for line in text
            .split_terminator('\n')
            .chain(text.is_empty().then_some(""))
        {
            self.lines.push((self.rows, Row::Code(key, line)));
            self.rows += 1;
            if track && self.width > 1 {
                width = width.max(code_fences::width(line));
            }
        }
        if track {
            self.fences.push(code_fences::Fence {
                key,
                rows: start..self.rows,
                max_offset: width.saturating_sub(usize::from(self.width - 1)),
            });
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
        self.lines.push((self.rows, Row::Plain(line)));
        self.rows += height;
    }

    pub(super) fn draw_from_bottom(&self, frame: &mut Frame, area: Rect, scroll: u16) {
        self.render(
            frame,
            area,
            self.rows
                .saturating_sub(usize::from(area.height) + usize::from(scroll)),
            None,
            false,
        );
    }

    fn render(
        &self,
        frame: &mut Frame,
        area: Rect,
        top: usize,
        pan: Option<&code_fences::Pan>,
        interactive: bool,
    ) {
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
        let bottom = top + usize::from(area.height);
        let mut first = start;
        // Keep native word wrapping for plain runs; code has one row per line.
        for group in self.lines[start..end]
            .chunk_by(|(_, a), (_, b)| matches!(a, Row::Plain(_)) == matches!(b, Row::Plain(_)))
        {
            match &group[0].1 {
                Row::Plain(_) => {
                    let first_row = group[0].0;
                    let visible_start = first_row.max(top);
                    let skip = (visible_start - first_row) as u16;
                    let last = first + group.len();
                    let visible_end = self
                        .lines
                        .get(last)
                        .map_or(self.rows, |line| line.0)
                        .min(bottom);
                    let height = ((visible_end - visible_start) as u16).min(u16::MAX - skip);
                    let lines = group
                        .iter()
                        .filter_map(|(_, row)| match row {
                            Row::Plain(line) => Some(line.clone()),
                            Row::Code(..) => None,
                        })
                        .collect::<Vec<_>>();
                    let y = area.y + (visible_start - top) as u16;
                    frame.render_widget(
                        paragraph(lines).scroll((skip, 0)),
                        Rect::new(area.x, y, area.width, height),
                    );
                }
                Row::Code(..) => {
                    for (index, row) in group {
                        if let Row::Code(key, text) = row {
                            code_fences::draw(
                                frame.buffer_mut(),
                                area,
                                (*index - top) as u16,
                                text,
                                pan.map_or(0, |pan| pan.offset(*key)),
                                interactive && pan.is_some_and(|pan| pan.selected(*key)),
                            );
                        }
                    }
                }
            }
            first += group.len();
        }
    }
}

fn paragraph<'a>(text: impl Into<ratatui::text::Text<'a>>) -> Paragraph<'a> {
    Paragraph::new(text).wrap(Wrap { trim: false })
}

#[cfg(test)]
mod tests {
    use crossterm::event::KeyModifiers;
    use ngn_core::host::Approval;
    use ngn_protocol::{Event, EventRecord, RunStatus, ToolCall, ToolResult};
    use ratatui::{Terminal, backend::TestBackend};
    use serde_json::json;

    use super::*;
    use crate::tui::footer::Activity;
    use crate::tui::{App, main_transcript};

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
        app.transcript_key(KeyEvent::new(code, KeyModifiers::NONE))
    }

    fn render(
        app: &mut App,
        terminal: &mut Terminal<TestBackend>,
        active: Option<ToolKey<'_>>,
        now: Instant,
    ) {
        render_masked(app, terminal, active, now, None);
    }

    fn render_masked<'a>(
        app: &mut App,
        terminal: &'a mut Terminal<TestBackend>,
        active: Option<ToolKey<'_>>,
        now: Instant,
        dialog: Option<Rect>,
    ) -> &'a Buffer {
        terminal
            .draw(|frame| {
                let area = frame.area();
                frame
                    .buffer_mut()
                    .set_style(area, Style::new().bg(Color::Blue));
                let parts = main_transcript(&app.records, app.approval.as_ref(), 1500);
                app.tools.observe_completions(&parts, app.after, now);
                let mut layout = Layout::new(
                    &parts,
                    area.width,
                    Some(&app.tools),
                    app.tools_available(),
                    active,
                );
                if app.shows_overlay() {
                    layout.hide_wipes();
                } else if let Some(area) = dialog {
                    layout.mask_wipes(area);
                }
                app.tools.draw(frame, area, layout, &mut app.scroll, now);
                if let Some(area) = dialog {
                    frame.render_widget(ratatui::widgets::Clear, area);
                    frame.render_widget(
                        Paragraph::new("Approval").style(
                            Style::new()
                                .fg(Color::Yellow)
                                .bg(Color::Red)
                                .add_modifier(Modifier::BOLD),
                        ),
                        area,
                    );
                }
            })
            .unwrap()
            .buffer
    }

    fn draw(app: &mut App, terminal: &mut Terminal<TestBackend>) -> Vec<String> {
        render(app, terminal, None, Instant::now());
        terminal
            .backend()
            .buffer()
            .content
            .chunks(terminal.size().unwrap().width as usize)
            .map(|row| row.iter().map(|cell| cell.symbol()).collect())
            .collect()
    }

    fn push(app: &mut App, record: EventRecord) {
        app.after += 1;
        app.records.push(EventRecord {
            sequence: app.after,
            ..record
        });
    }

    #[test]
    fn approval_masks_only_covered_glyphs_without_retiming_the_wipe() {
        let start = Instant::now();
        let middle = start + Duration::from_millis(625);
        let end = start + COMPLETION_DURATION;
        let mut app = App {
            tools: Cards::new(true),
            ..App::default()
        };
        for (id, label) in [
            ("above", "above the dialog"),
            ("across", "工作 across the dialog"),
        ] {
            push(&mut app, started(id, "shared", label));
            push(&mut app, finished(id, "shared", json!("ok"), ""));
        }
        let mut terminal = Terminal::new(TestBackend::new(60, 12)).unwrap();
        render(&mut app, &mut terminal, None, start);
        let above = app.tools.headers[0].1 as u16;
        let across = app.tools.headers[1].1 as u16;
        let full = terminal.backend().buffer().area;
        assert_eq!(terminal.backend().buffer()[(10, across)].symbol(), "工");

        // Start the dialog on the continuation column of a two-column glyph.
        let dialog = Rect::new(11, across, 12, 2);
        let initial = render_masked(&mut app, &mut terminal, None, start, Some(dialog)).clone();
        assert_eq!(initial[(11, across)].symbol(), "A");
        assert_eq!(initial[(11, across)].fg, Color::Yellow);
        assert_eq!(initial[(11, across)].bg, Color::Red);
        assert!(initial[(11, across)].modifier.contains(Modifier::BOLD));
        assert!(app.tools.needs_frame(middle));
        let mut cached = initial.clone();
        app.tools.paint_motion(&mut cached, middle);
        assert_ne!(cached[(2, above)].fg, initial[(2, above)].fg);
        assert_ne!(cached[(2, across)].fg, initial[(2, across)].fg);
        assert_ne!(cached[(23, across)].fg, initial[(23, across)].fg);

        // A completely covered transcript must not schedule motion of its own.
        let fully_covered =
            render_masked(&mut app, &mut terminal, None, middle, Some(full)).clone();
        assert!(!app.tools.needs_frame(middle));
        let mut covered_tick = fully_covered.clone();
        app.tools.paint_motion(&mut covered_tick, middle);
        assert_eq!(covered_tick, fully_covered);

        // Dismissal at the same timestamp produces exactly the unmasked phase.
        render(&mut app, &mut terminal, None, middle);
        let unmasked = terminal.backend().buffer();
        let mut expected = initial.clone();
        for pos in full.positions() {
            if !dialog.contains(pos) && pos != Position::new(10, across) {
                expected[pos].fg = unmasked[pos].fg;
            }
        }
        assert_eq!(cached, expected);
        assert_eq!(app.tools.completions.len(), 2);
        assert!(
            app.tools
                .completions
                .values()
                .all(|wipe| wipe.started == start)
        );
        render_masked(&mut app, &mut terminal, None, end, Some(dialog));
        render(&mut app, &mut terminal, None, end);
        assert!(app.tools.completions.is_empty());
        assert_eq!(terminal.backend().buffer()[(2, above)].fg, COMPLETE);
        assert_eq!(terminal.backend().buffer()[(2, across)].fg, COMPLETE);
    }

    #[test]
    fn completion_uses_exact_focused_call_source_and_only_changes_foreground() {
        let now = Instant::now();
        let mut app = App {
            tools: Cards::new(true),
            input: "keep the draft".into(),
            ..App::default()
        };
        push(&mut app, started("old", "shared", "archive"));
        push(&mut app, finished("old", "shared", json!("ok"), ""));
        app.tools.skip_completions(app.after);
        push(&mut app, started("live", "shared", "工作 e\u{301}"));
        let mut term = Terminal::new(TestBackend::new(60, 14)).unwrap();
        render(&mut app, &mut term, Some(("live", "shared")), now);
        assert!(key(&mut app, KeyCode::F(4)));
        assert!(key(&mut app, KeyCode::Enter));
        render(&mut app, &mut term, Some(("live", "shared")), now);
        let start = now + Duration::from_millis(100);
        let row = app.tools.headers[1].1 as u16;
        let old_row = app.tools.headers[0].1 as u16;
        let running = term.backend().buffer().clone();
        assert_eq!(running[(2, old_row)].fg, COMPLETE);
        assert!(running[(2, row)].modifier.contains(Modifier::UNDERLINED));
        assert_eq!(running[(2, row)].bg, Color::Blue);
        assert_eq!(running[(0, row)].symbol(), "v");
        push(&mut app, finished("live", "shared", json!("ok"), ""));
        render(&mut app, &mut term, None, start);
        assert!(app.tools.needs_frame(start));
        let first = term.backend().buffer().clone();
        assert_eq!(first[(2, row)].fg, running[(2, row)].fg);
        assert_eq!(first[(0, row)].fg, Color::Reset);

        let mut middle = first.clone();
        app.tools
            .paint_motion(&mut middle, start + Duration::from_millis(625));
        let mut final_frame = first.clone();
        app.tools
            .paint_motion(&mut final_frame, start + COMPLETION_DURATION);
        assert!(!app.tools.needs_frame(start + COMPLETION_DURATION));
        assert_ne!(first, middle);
        assert_ne!(middle, final_frame);
        assert_eq!(final_frame[(2, row)].fg, COMPLETE);
        let lines = |buffer: &Buffer| {
            (0..60)
                .map(|column| buffer[(column, row)].symbol())
                .collect::<String>()
        };
        assert!(lines(&final_frame).contains("complete · 1.25s"));
        for mut frame in [first, middle] {
            for column in 2..60 {
                frame[(column, row)].fg = final_frame[(column, row)].fg;
            }
            assert_eq!(frame, final_frame);
        }
        app.help = true;
        let covered = start + Duration::from_millis(650);
        render(&mut app, &mut term, None, covered);
        assert!(!app.tools.needs_frame(covered));
        let overlay = term.backend().buffer().clone();
        let mut cached_overlay = overlay.clone();
        app.tools.paint_motion(&mut cached_overlay, covered);
        assert_eq!(overlay, cached_overlay);
        app.help = false;
        // Even a new durable record for the same call cannot restart the wipe.
        push(&mut app, finished("live", "shared", json!("duplicate"), ""));
        render(&mut app, &mut term, None, start + COMPLETION_DURATION);
        assert!(!app.tools.needs_frame(start + COMPLETION_DURATION));
        assert_eq!(term.backend().buffer(), &final_frame);
        assert!(key(&mut app, KeyCode::Char('x')));
        assert_eq!(app.input, "keep the draft");
        assert!(key(&mut app, KeyCode::Esc));
        assert!(!key(&mut app, KeyCode::Char('x')));
    }

    #[test]
    fn completion_does_not_replay_history_or_animate_unsuccessful_calls() {
        let now = Instant::now();
        let mut app = App {
            tools: Cards::new(true),
            ..App::default()
        };
        push(&mut app, started("old", "shared", "old"));
        push(&mut app, finished("old", "shared", json!("ok"), ""));
        app.tools.skip_completions(app.after);
        push(&mut app, finished("orphan", "shared", json!("ok"), ""));
        push(&mut app, started("failed", "shared", "failed"));
        push(
            &mut app,
            finished("failed", "shared", json!(null), "failed"),
        );
        push(&mut app, started("nonzero", "shared", "nonzero"));
        push(
            &mut app,
            finished("nonzero", "shared", json!({"exit_code": 7}), ""),
        );
        push(&mut app, started("cancelled", "shared", "cancelled"));
        push(
            &mut app,
            event(
                "cancelled",
                Event::RunFinished {
                    status: RunStatus::Cancelled,
                    error: String::new(),
                },
            ),
        );
        push(&mut app, started("denied", "shared", "denied"));
        push(
            &mut app,
            event(
                "denied",
                Event::ApprovalRequested {
                    approval_id: "approval".into(),
                    call: call("shared", "denied"),
                    preview: String::new(),
                },
            ),
        );
        push(
            &mut app,
            event(
                "denied",
                Event::ApprovalResolved {
                    approval_id: "approval".into(),
                    allowed: false,
                },
            ),
        );
        push(
            &mut app,
            finished("denied", "shared", json!(null), "denied"),
        );
        let mut term = Terminal::new(TestBackend::new(60, 30)).unwrap();
        render(&mut app, &mut term, None, now);
        assert!(!app.tools.needs_frame(now));
        let colors: Vec<_> = app
            .tools
            .headers
            .iter()
            .map(|(_, row)| term.backend().buffer()[(2, *row as u16)].fg)
            .collect();
        assert_eq!(
            colors,
            [
                COMPLETE,
                COMPLETE,
                Color::Red,
                Color::Reset,
                Color::Magenta,
                Color::Red
            ]
        );
        app.tools.reset();
        app.tools.skip_completions(app.after);
        render(&mut app, &mut term, None, now);
        assert!(!app.tools.needs_frame(now));
    }

    #[test]
    fn same_batch_successes_animate_independently_or_snap_with_reduced_motion() {
        let now = Instant::now();
        for animated in [false, true] {
            let mut app = App {
                tools: Cards::new(animated),
                ..App::default()
            };
            for id in ["a", "b"] {
                push(&mut app, started("live", id, id));
                push(&mut app, finished("live", id, json!("ok"), ""));
            }
            let mut term = Terminal::new(TestBackend::new(35, 10)).unwrap();
            render(&mut app, &mut term, None, now);
            assert_eq!(app.tools.needs_frame(now), animated);
            assert_eq!(app.tools.wipes.len(), if animated { 2 } else { 0 });
            let start = term.backend().buffer();
            for (_, row) in &app.tools.headers {
                assert_eq!(
                    start[(2, *row as u16)].fg,
                    if animated {
                        Color::Rgb(136, 136, 136)
                    } else {
                        COMPLETE
                    }
                );
            }
        }
    }

    #[test]
    fn fenced_rows_use_default_panel_and_green_border_without_wrapping() {
        let mut app = App {
            records: vec![event(
                "r",
                Event::TextCompleted {
                    text: "before\n```example\n\t界e\u{301} *\n\n  [x]\n```\nafter".into(),
                },
            )],
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(7, 8)).unwrap();
        let rows = draw(&mut app, &mut terminal);
        assert_eq!(
            &rows[..5],
            &["before ", "│    界 ", "│      ", "│  [x] ", "after  "]
        );
        let buffer = terminal.backend().buffer();
        for row in 1..4 {
            assert_eq!(buffer[(0, row)].fg, Color::Green);
            for x in 0..7 {
                assert_eq!(buffer[(x, row)].bg, Color::Reset);
                if x > 0 {
                    assert_eq!(buffer[(x, row)].fg, Color::Reset);
                }
            }
        }
        assert_eq!(buffer[(0, 0)].bg, Color::Blue);
        assert_eq!(buffer[(0, 4)].bg, Color::Blue);
        assert!(key(&mut app, KeyCode::F(8)));
        assert!(key(&mut app, KeyCode::Right));
        assert_eq!(draw(&mut app, &mut terminal)[1], "│ 界 e\u{301} *");
    }

    #[test]
    fn code_tail_scrolls_past_u16_rows_and_tiny_widths_are_safe() {
        let mut app = App {
            records: vec![event(
                "r",
                Event::TextDelta {
                    text: format!("~~~\n{}{}\n界Z", "a\n".repeat(70_000), "b".repeat(140_000)),
                },
            )],
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(3, 3)).unwrap();
        assert_eq!(draw(&mut app, &mut terminal), ["│a ", "│bb", "│界 "]);
        assert!(app.tools.rendered_top > usize::from(u16::MAX));
        let mut terminal = Terminal::new(TestBackend::new(2, 3)).unwrap();
        assert_eq!(draw(&mut app, &mut terminal), ["│a", "│b", "│ "]);
        assert!(key(&mut app, KeyCode::F(8)));
        assert!(key(&mut app, KeyCode::End));
        assert_eq!(draw(&mut app, &mut terminal), ["│ ", "│b", "│ "]);
        let mut terminal = Terminal::new(TestBackend::new(1, 3)).unwrap();
        assert_eq!(draw(&mut app, &mut terminal), ["│", "│", "│"]);
    }

    #[test]
    fn pan_is_per_fence_clamps_across_width_and_final_snapshot_and_preserves_focus_routing() {
        let mut app = App {
            input: "/unsent".into(),
            records: vec![
                started("r", "c", "argument"),
                finished("r", "c", json!({}), ""),
                EventRecord {
                    sequence: 3,
                    ..event(
                        "r",
                        Event::TextDelta {
                            text: "```\n1234567890AB\n1234567890ab\n```\n~~~\nabcdefghijkl".into(),
                        },
                    )
                },
            ],
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(9, 18)).unwrap();
        draw(&mut app, &mut terminal);
        assert!(app.tools.title(true).contains("Code F8"));
        assert!(!key(&mut app, KeyCode::Tab));
        assert!(!key(&mut app, KeyCode::Left));
        assert!(key(&mut app, KeyCode::F(4)));
        draw(&mut app, &mut terminal);
        assert!(!key(&mut app, KeyCode::F(8)));
        assert!(key(&mut app, KeyCode::Enter));
        assert!(key(&mut app, KeyCode::Esc));
        let before = draw(&mut app, &mut terminal);
        assert!(before.iter().any(|row| row.starts_with("v ")));

        assert!(key(&mut app, KeyCode::F(8)));
        assert!(key(&mut app, KeyCode::End));
        let rows = draw(&mut app, &mut terminal);
        assert!(rows.contains(&"│efghijkl".into()));
        assert!(rows.contains(&"│12345678".into()));
        let top = app.scroll;
        assert!(key(&mut app, KeyCode::Up));
        assert!(key(&mut app, KeyCode::Right));
        let rows = draw(&mut app, &mut terminal);
        assert!(rows.contains(&"│567890AB".into()));
        assert!(rows.contains(&"│567890ab".into()));
        assert!(rows.contains(&"│efghijkl".into()));
        assert_eq!(app.scroll, top);
        assert!(key(&mut app, KeyCode::Enter));
        assert!(key(&mut app, KeyCode::Char('x')));
        assert!(!app.composer_available());
        assert_eq!(app.input, "/unsent");

        let mut wide = Terminal::new(TestBackend::new(11, 18)).unwrap();
        let rows = draw(&mut app, &mut wide);
        assert!(rows.contains(&"│34567890AB".into()));
        assert!(rows.contains(&"│cdefghijkl".into()));
        let rows = draw(&mut app, &mut terminal);
        assert!(rows.contains(&"│34567890".into())); // no latent out-of-bounds offset
        app.records.push(EventRecord {
            sequence: 4,
            ..event(
                "r",
                Event::TextDelta {
                    text: "\n~~".into(),
                },
            )
        });
        assert!(draw(&mut app, &mut terminal).contains(&"│cdefghij".into()));
        app.records.push(EventRecord {
            sequence: 5,
            ..event(
                "r",
                Event::TextCompleted {
                    text: "```\n1234567890Z\n1234567890z\n```\n~~~\nnew-final\n~~~".into(),
                },
            )
        });
        let rows = draw(&mut app, &mut terminal);
        assert!(rows.contains(&"│34567890".into()));
        assert!(rows.contains(&"│ew-final".into()));
        assert!(!rows.iter().any(|row| row.contains("cdefghij")));
        assert!(key(&mut app, KeyCode::Esc));
        assert!(app.composer_available());
        assert!(app.tools.expanded.contains(&("r".into(), "c".into())));
    }

    #[test]
    fn streaming_fence_keeps_the_reading_anchor_and_expanded_tool() {
        let delta = |text: String| event("r", Event::TextDelta { text });
        let mut app = App {
            records: vec![
                started("r", "c", "plain arguments"),
                finished("r", "c", json!({}), "~~~\nplain tool error\n~~~"),
            ],
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(40, 8)).unwrap();
        draw(&mut app, &mut terminal);
        assert!(key(&mut app, KeyCode::F(4)));
        assert!(key(&mut app, KeyCode::Enter));
        let before = draw(&mut app, &mut terminal);
        assert!(before[1].starts_with("v shell"));
        assert!(before.iter().any(|row| row.starts_with("plain tool error")));
        assert!(before.iter().all(|row| !row.starts_with('│')));
        assert!(key(&mut app, KeyCode::Esc));

        app.records
            .push(delta(format!("~~~\n{}", "streaming 界\n".repeat(30))));
        assert_eq!(draw(&mut app, &mut terminal), before);
        app.tools.page(&mut app.scroll, false);
        let anchor = draw(&mut app, &mut terminal);
        let top = app.scroll;
        assert!(anchor.iter().all(|row| row.starts_with('│')));
        app.records.push(delta("last code\n~~".into()));
        assert_eq!(draw(&mut app, &mut terminal), anchor);
        app.records.push(delta("~\nafter".into()));
        assert_eq!(draw(&mut app, &mut terminal), anchor);
        assert_eq!(app.scroll, top);
        assert!(app.tools.expanded.contains(&("r".into(), "c".into())));
        while app.scroll.is_some() {
            app.tools.page(&mut app.scroll, false);
            draw(&mut app, &mut terminal);
        }
        let tail = draw(&mut app, &mut terminal);
        assert!(tail[6].starts_with("│last code"));
        assert!(tail[7].starts_with("after"));
    }

    #[test]
    fn sweep_repaints_only_the_visible_active_header_foreground() {
        let now = Instant::now();
        let active = Some(("live", "shared"));
        for width in [1, 30, 60] {
            let mut terminal = Terminal::new(TestBackend::new(width, 12)).unwrap();
            let mut app = App {
                records: vec![
                    started("history", "shared", "unrelated"),
                    started("live", "shared", "工作工作 e\u{301}"),
                ],
                ..App::default()
            };
            render(&mut app, &mut terminal, None, now);
            assert!(key(&mut app, KeyCode::F(4)));
            render(&mut app, &mut terminal, None, now);
            let baseline = terminal.backend().buffer().clone();
            let row = app.tools.headers[1].1 as u16;
            assert!(baseline[(0, row)].modifier.contains(Modifier::UNDERLINED));
            assert_eq!(baseline[(0, row)].bg, Color::Blue);

            render(&mut app, &mut terminal, active, now);
            let painted = terminal.backend().buffer().clone();
            let mut later = painted.clone(); // The motion tick starts from the cached frame.
            app.tools
                .paint_motion(&mut later, now + Duration::from_millis(700));
            assert_ne!(painted, later);
            assert_ne!(painted, baseline);
            for mut frame in [painted, later] {
                for column in 0..width {
                    frame[(column, row)].fg = baseline[(column, row)].fg;
                }
                assert_eq!(frame, baseline); // Text, background, modifiers and other rows.
            }
            if width == 30 {
                assert_eq!(baseline[(10, row)].symbol(), "工");
                assert_eq!(baseline[(11, row)].symbol(), " "); // continuation cell
            }
        }
    }

    #[test]
    fn resumed_sweep_uses_host_active_time_and_stays_out_of_the_inspector() {
        let now = Instant::now();
        let mut app = App {
            records: vec![started("r", "c", "active")],
            ..App::default()
        };
        for (timestamp_ms, transition) in [
            (
                200,
                Event::ApprovalRequested {
                    approval_id: "approval".into(),
                    call: call("c", "active"),
                    preview: String::new(),
                },
            ),
            (
                201,
                Event::ToolApprovalTiming {
                    call_id: "c".into(),
                    elapsed_ms: 56,
                    waiting: true,
                },
            ),
            (
                1400,
                Event::ToolApprovalTiming {
                    call_id: "c".into(),
                    elapsed_ms: 56,
                    waiting: false,
                },
            ),
            (
                1450,
                Event::ApprovalResolved {
                    approval_id: "approval".into(),
                    allowed: true,
                },
            ),
        ] {
            app.records.push(EventRecord {
                timestamp_ms,
                ..event("r", transition)
            });
        }
        let active = Some(("r", "c"));
        let mut terminal = Terminal::new(TestBackend::new(60, 8)).unwrap();
        render(&mut app, &mut terminal, active, now);
        let (_, _, _, age, _) = app.tools.sweep.as_ref().unwrap();
        assert_eq!(*age, Duration::from_millis(156));
        let parts = main_transcript(&app.records, None, 1500);
        assert!(Layout::new(&parts, 60, None, false, active).sweep.is_none());
    }

    #[test]
    fn sweep_stops_when_waiting_or_terminal_and_reduced_motion_is_static() {
        let now = Instant::now();
        let mut app = App {
            records: vec![started("r", "c", "active")],
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(60, 8)).unwrap();
        let mut activity = Activity::new(now, false);
        activity.sync(Some("r"), false, false, &app.records, now, 1500);
        assert_eq!(activity.shimmering_tool(), None);
        render(&mut app, &mut terminal, activity.shimmering_tool(), now);
        assert_eq!(terminal.backend().buffer()[(0, 1)].fg, Color::Reset);

        let mut activity = Activity::new(now, true);
        activity.sync(Some("r"), false, false, &app.records, now, 1500);
        assert_eq!(activity.shimmering_tool(), Some(("r", "c")));
        activity.sync(Some("r"), true, false, &app.records, now, 1500);
        assert_eq!(activity.shimmering_tool(), None);

        for (event, outcome, fg) in [
            (
                Event::ApprovalRequested {
                    approval_id: "approval".into(),
                    call: call("c", "active"),
                    preview: String::new(),
                },
                "Approval waiting",
                Color::Reset,
            ),
            (
                Event::RunFinished {
                    status: RunStatus::Cancelled,
                    error: String::new(),
                },
                "cancelled",
                Color::Magenta,
            ),
            (
                finished("r", "c", json!(null), "failed").event,
                "failed",
                Color::Red,
            ),
            (
                finished("r", "c", json!("ok"), "").event,
                "complete",
                Color::Reset,
            ),
        ] {
            app.records.truncate(1);
            render(&mut app, &mut terminal, Some(("r", "c")), now);
            assert!(app.tools.sweep.is_some());
            app.records.push(self::event("r", event));
            // Even if the active key hasn't changed, projected state must veto the sweep.
            render(&mut app, &mut terminal, Some(("r", "c")), now);
            let baseline = terminal.backend().buffer().clone();
            let mut later = baseline.clone();
            app.tools
                .paint_motion(&mut later, now + Duration::from_millis(700));
            assert!(app.tools.sweep.is_none());
            assert_eq!(baseline, later);
            assert_eq!(baseline[(0, 1)].fg, fg);
            assert!(draw(&mut app, &mut terminal)[1].contains(outcome));
        }
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
        let collapsed = draw(&mut app, &mut terminal);
        let row = collapsed
            .iter()
            .position(|row| row.starts_with("> shell · second"))
            .unwrap();
        let marker = &terminal.backend().buffer()[(0, row as u16)];
        assert!(marker.modifier.contains(Modifier::UNDERLINED));
        assert!(!marker.modifier.contains(Modifier::REVERSED));
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
        assert!(selected.modifier.contains(Modifier::UNDERLINED));

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
                .any(|cell| cell.modifier.contains(Modifier::UNDERLINED))
        );
        assert_eq!(app.input, "/se unsent draft");
    }

    #[test]
    fn overlays_and_live_approval_suspend_card_keys() {
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
        // The modal is visible even though the durable request has not caught up.
        render(&mut app, &mut terminal, Some(("r", "c")), Instant::now());
        assert!(app.tools.sweep.is_none());
        let rows = draw(&mut app, &mut terminal);
        assert!(rows[1].contains("Approval waiting"));
        assert!(!rows[1].contains("running"));
        app.approval = None;
        assert!(key(&mut app, KeyCode::Enter));
        assert_eq!(app.tools.expanded.len(), 1);
    }

    #[test]
    fn approval_and_overlays_suspend_code_focus_without_losing_offset_or_draft() {
        let mut app = App {
            input: "unsent".into(),
            records: vec![event(
                "r",
                Event::TextCompleted {
                    text: "```\n12345678SUFFIX\n```".into(),
                },
            )],
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(7, 8)).unwrap();
        draw(&mut app, &mut terminal);
        assert!(key(&mut app, KeyCode::F(8)));
        assert!(key(&mut app, KeyCode::Right));
        let panned = draw(&mut app, &mut terminal);
        assert_eq!(panned[0], "│5678SU");

        app.help = true;
        assert!(key(&mut app, KeyCode::F(8)));
        app.help = false;
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
        assert!(key(&mut app, KeyCode::F(8)));
        assert!(key(&mut app, KeyCode::End));
        assert!(key(&mut app, KeyCode::Enter));
        assert!(!app.composer_available());
        app.approval = None;
        assert_eq!(draw(&mut app, &mut terminal), panned);
        assert!(key(&mut app, KeyCode::F(8)));
        assert!(app.composer_available());
        assert_eq!(app.input, "unsent");
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
        assert_eq!(terminal.backend().buffer()[(0, 1)].fg, Color::Red);
        assert!(key(&mut app, KeyCode::F(4)));
        assert!(key(&mut app, KeyCode::Enter));
        let rows = draw(&mut app, &mut terminal);
        assert!(rows[1].starts_with("v shell"));
        assert!(rows[2].contains("Arguments:"));
        assert_eq!(app.tools.rendered_top, 0);
        let parts = crate::view::transcript(&app.records, 0);
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
