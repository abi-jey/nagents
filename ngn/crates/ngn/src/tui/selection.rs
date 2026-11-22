//! Selection belongs to the last drawn text layout. Conversation and draft
//! text copy as logical lines; everything else on screen (borders, status
//! rows, panels and overlays) copies as the painted cells.

use std::sync::Arc;

use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    buffer::Buffer,
    layout::{Position, Rect},
    style::{Modifier, Style},
    text::Span,
};

use super::{clipboard::MAX_COPY_BYTES, tool_cards::Key};

#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct Pane {
    area: Rect,
    rows: Vec<Option<Row>>,
}

#[derive(Debug, Default, PartialEq, Eq)]
struct Row {
    text: String,
    glyphs: Vec<Glyph>,
    soft_wrap: bool,
}

#[derive(Debug, PartialEq, Eq)]
struct Glyph {
    left: u16,
    right: u16,
    end: usize,
}

impl Row {
    fn segment(&self, from: usize, to: Option<usize>) -> Option<(&[Glyph], &str)> {
        let glyphs = self.glyphs.get(from..to.unwrap_or(self.glyphs.len()))?;
        let start = from.checked_sub(1).map_or(0, |i| self.glyphs[i].end);
        let end = glyphs.last().map_or(start, |glyph| glyph.end);
        Some((glyphs, self.text.get(start..end)?))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct Point {
    row: usize,
    glyph: usize,
}

fn columns(row: usize, start: Point, end: Point) -> (usize, Option<usize>) {
    (
        if row == start.row { start.glyph } else { 0 },
        (row == end.row).then_some(end.glyph + 1),
    )
}

impl Pane {
    pub(super) fn new(area: Rect) -> Self {
        Self {
            area,
            rows: (0..area.height).map(|_| None).collect(),
        }
    }

    pub(super) fn line(&mut self, y: u16, soft_wrap: bool) {
        if let Some(row) = self.row_mut(y) {
            row.soft_wrap = soft_wrap;
        }
    }

    pub(super) fn glyph(&mut self, x: u16, y: u16, width: u16, text: &str) {
        let right = x.saturating_add(width);
        if width > 0
            && x >= self.area.left()
            && right <= self.area.right()
            && let Some(row) = self.row_mut(y)
        {
            row.text.push_str(text);
            row.glyphs.push(Glyph {
                left: x,
                right,
                end: row.text.len(),
            });
        }
    }

    /// Every painted cell of a finished frame, one hard line per terminal row.
    /// Trailing blank cells are left out so copies do not end in padding.
    pub(super) fn screen(buffer: &Buffer) -> Self {
        let area = buffer.area;
        let mut pane = Self::new(area);
        for y in area.top()..area.bottom() {
            pane.line(y, false);
            let mut cells = Vec::new();
            let mut x = area.left();
            while x < area.right() {
                let symbol = buffer[(x, y)].symbol();
                let width = u16::try_from(Span::raw(symbol).width()).unwrap_or(1);
                if width > 0 {
                    cells.push((x, width.min(area.right() - x), symbol));
                }
                x = x.saturating_add(width.max(1));
            }
            let used = cells
                .iter()
                .rposition(|(_, _, symbol)| !symbol.trim().is_empty())
                .map_or(0, |last| last + 1);
            for &(x, width, symbol) in &cells[..used] {
                pane.glyph(x, y, width, symbol);
            }
        }
        pane
    }

    fn row_mut(&mut self, y: u16) -> Option<&mut Row> {
        let index = usize::from(y.checked_sub(self.area.top())?);
        Some(self.rows.get_mut(index)?.get_or_insert_with(Row::default))
    }

    fn point(&self, row: usize, column: u16) -> Option<Point> {
        let cells = &self.rows.get(row)?.as_ref()?.glyphs;
        let last = cells.len().checked_sub(1)?;
        let glyph = cells.partition_point(|cell| cell.right <= column).min(last);
        Some(Point { row, glyph })
    }

    fn hit(&self, at: Position) -> Option<Point> {
        self.area
            .contains(at)
            .then(|| self.point(usize::from(at.y - self.area.top()), at.x))
            .flatten()
    }

    fn clamped_hit(&self, start: Point, at: Position) -> Point {
        let mut row = usize::from(at.y.saturating_sub(self.area.top())).min(self.rows.len() - 1);
        if let Some(point) = self.point(row, at.x) {
            return point;
        }
        let column = if row > start.row { u16::MAX } else { 0 };
        // On a blank row, select up to the closest text in the anchor's direction.
        while row != start.row {
            row = if row > start.row { row - 1 } else { row + 1 };
            if let Some(point) = self.point(row, column) {
                return point;
            }
        }
        start
    }

    fn matches(&self, other: &Self, a: Point, b: Point) -> bool {
        let (start, end) = (a.min(b), a.max(b));
        self.area == other.area
            && (start.row..=end.row).all(|index| {
                let (old, new) = match (&self.rows[index], &other.rows[index]) {
                    (Some(old), Some(new)) => (old, new),
                    (None, None) => return true,
                    _ => return false,
                };
                if index != end.row && old.soft_wrap != new.soft_wrap {
                    return false;
                }
                let (from, to) = columns(index, start, end);
                match (old.segment(from, to), new.segment(from, to)) {
                    (Some((before, old_text)), Some((after, new_text))) => {
                        old_text == new_text
                            && before.len() == after.len()
                            && before.iter().zip(after).all(|(left, right)| {
                                left.left == right.left && left.right == right.right
                            })
                    }
                    _ => false,
                }
            })
    }

    fn extract(&self, a: Point, b: Point) -> (Vec<Rect>, CopyText) {
        let (start, end) = (a.min(b), a.max(b));
        let mut ink = Vec::new();
        let mut text = Some(String::new());
        let mut previous_wrap = None;
        for index in start.row..=end.row {
            let Some(row) = &self.rows[index] else {
                continue;
            };
            if previous_wrap == Some(false) {
                append(&mut text, "\n");
            }
            previous_wrap = Some(row.soft_wrap);
            let (from, to) = columns(index, start, end);
            if let Some((cells, segment)) = row.segment(from, to)
                && let (Some(first), Some(last)) = (cells.first(), cells.last())
            {
                append(&mut text, segment);
                ink.push(Rect::new(
                    first.left,
                    self.area.top() + index as u16,
                    last.right - first.left,
                    1,
                ));
            }
        }
        (ink, text.map_or(CopyText::TooLarge, CopyText::Text))
    }
}

fn append(buffer: &mut Option<String>, text: &str) {
    if let Some(buffered) = buffer {
        if buffered.len() + text.len() > MAX_COPY_BYTES {
            *buffer = None;
        } else {
            buffered.push_str(text);
        }
    }
}

#[derive(Clone, Copy)]
enum Part {
    Transcript,
    Draft,
    Screen,
}

#[derive(Default)]
struct FrameText {
    transcript: Arc<Pane>,
    draft: Arc<Pane>,
    screen: Arc<Pane>,
}

impl FrameText {
    fn pane(&self, part: Part) -> &Arc<Pane> {
        match part {
            Part::Transcript => &self.transcript,
            Part::Draft => &self.draft,
            Part::Screen => &self.screen,
        }
    }

    fn anchor(&self, position: Position) -> Option<Anchor> {
        // Text panes own their whole area, blank cells included, so a press
        // there keeps logical copying. Anything else falls back to the screen.
        let part = [Part::Transcript, Part::Draft]
            .into_iter()
            .find(|&part| self.pane(part).area.contains(position))
            .unwrap_or(Part::Screen);
        let pane = self.pane(part);
        Some(Anchor {
            part,
            pane: Arc::clone(pane),
            point: pane.hit(position)?,
        })
    }
}

#[derive(Clone)]
struct Anchor {
    part: Part,
    pane: Arc<Pane>,
    point: Point,
}

impl Anchor {
    fn still_visible(&self, frame: &FrameText, end: Point) -> bool {
        let current = frame.pane(self.part);
        Arc::ptr_eq(&self.pane, current) || self.pane.matches(current, self.point, end)
    }
}

struct Press {
    origin: Position,
    card: Option<Key>,
    anchor: Option<Anchor>,
    last_drag: Option<Position>,
}

pub(super) enum CopyText {
    Text(String),
    TooLarge,
}

struct Mark {
    anchor: Anchor,
    end: Point,
    ink: Vec<Rect>,
    text: CopyText,
}

#[derive(Default)]
pub(super) struct Effect {
    pub(super) redraw: bool,
    pub(super) click: Option<Key>,
    pub(super) hold_transcript: bool,
    pub(super) selection_released: bool,
}

#[derive(Default)]
pub(super) struct Selection {
    frame: FrameText,
    press: Option<Press>,
    mark: Option<Mark>,
    feedback: Option<&'static str>,
}

impl Selection {
    pub(super) fn frame(&mut self, transcript: Pane, draft: Pane, screen: Pane) {
        let interactive =
            !transcript.area.is_empty() || !draft.area.is_empty() || !screen.area.is_empty();
        self.frame = FrameText {
            transcript: Arc::new(transcript),
            draft: Arc::new(draft),
            screen: Arc::new(screen),
        };
        if !interactive {
            self.clear();
            return;
        }
        // Keep frozen bytes only while they still correspond to the highlight.
        // Neither reflow nor streaming may silently move it onto different text.
        let selection_changed = self
            .mark
            .as_ref()
            .is_some_and(|mark| !mark.anchor.still_visible(&self.frame, mark.end));
        if selection_changed {
            self.mark = None;
            self.feedback = None;
        }
        if let Some(press) = &mut self.press
            && (selection_changed
                || press
                    .anchor
                    .as_ref()
                    .is_some_and(|anchor| !anchor.still_visible(&self.frame, anchor.point)))
        {
            // An elapsed timer can change the glyph without changing the
            // card target. Keep the action press, but not the text anchor.
            press.anchor = None;
        }
    }

    pub(super) fn clear(&mut self) -> bool {
        self.press = None;
        self.feedback = None;
        self.mark.take().is_some()
    }

    pub(super) fn copy_text(&self) -> Option<&CopyText> {
        self.press
            .is_none()
            .then(|| self.mark.as_ref().map(|mark| &mark.text))
            .flatten()
    }

    pub(super) fn feedback(&mut self, feedback: &'static str) {
        self.feedback = Some(feedback);
    }

    pub(super) fn hint(&self) -> Option<&'static str> {
        self.feedback
    }

    pub(super) fn paint(&self, buffer: &mut Buffer) {
        if let Some(mark) = &self.mark {
            for &area in &mark.ink {
                buffer.set_style(area, Style::new().add_modifier(Modifier::REVERSED));
            }
        }
    }

    fn select(&mut self, anchor: &Anchor, position: Position) -> Effect {
        if !anchor.still_visible(&self.frame, anchor.point) {
            return Effect {
                redraw: self.clear(),
                ..Effect::default()
            };
        }
        // Drag may extend into newly streamed text as long as what it already
        // highlighted (and the original anchor) has not shifted.
        let anchor = Anchor {
            pane: Arc::clone(self.frame.pane(anchor.part)),
            ..anchor.clone()
        };
        let end = anchor.pane.clamped_hit(anchor.point, position);
        let (ink, mut text) = anchor.pane.extract(anchor.point, end);
        if let (Part::Screen, CopyText::Text(copied)) = (anchor.part, &mut text) {
            // Painted rows end in padding up to the drag column; terminals drop it.
            *copied = copied
                .lines()
                .map(str::trim_end)
                .collect::<Vec<_>>()
                .join("\n");
        }
        let hold_transcript = matches!(anchor.part, Part::Transcript);
        self.mark = Some(Mark {
            anchor,
            end,
            ink,
            text,
        });
        Effect {
            redraw: true,
            hold_transcript,
            ..Effect::default()
        }
    }

    pub(super) fn mouse(&mut self, event: MouseEvent, card: Option<&Key>) -> Effect {
        let position = Position::new(event.column, event.row);
        if event.modifiers.is_empty() {
            match event.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    let redraw = self.clear();
                    self.press = Some(Press {
                        origin: position,
                        card: card.cloned(),
                        anchor: self.frame.anchor(position),
                        last_drag: None,
                    });
                    return Effect {
                        redraw,
                        ..Effect::default()
                    };
                }
                MouseEventKind::Drag(MouseButton::Left) => {
                    if let Some(press) = &mut self.press
                        && press.last_drag.unwrap_or(press.origin) != position
                    {
                        press.last_drag = Some(position);
                        if let Some(anchor) = press.anchor.clone() {
                            return self.select(&anchor, position);
                        }
                    }
                    return Effect::default();
                }
                MouseEventKind::Up(MouseButton::Left) => {
                    if let Some(press) = self.press.take() {
                        if press.last_drag.is_none() && press.origin == position {
                            return Effect {
                                click: press.card.filter(|key| Some(key) == card),
                                ..Effect::default()
                            };
                        }
                        if let Some(anchor) = press.anchor {
                            let mut effect = self.select(&anchor, position);
                            effect.selection_released = self.mark.is_some();
                            return effect;
                        }
                    }
                    return Effect::default();
                }
                MouseEventKind::Moved => {
                    // An unpressed move to a different cell indicates the
                    // terminal lost the original gesture, not a drag.
                    let lost = self
                        .press
                        .as_ref()
                        .is_some_and(|press| position != press.last_drag.unwrap_or(press.origin));
                    return Effect {
                        redraw: lost && self.clear(),
                        ..Effect::default()
                    };
                }
                _ => {}
            }
        }
        // Wheel, other buttons or an emulator override cannot complete a click
        // that started with an unmodified press.
        Effect {
            redraw: self.press.is_some() && self.clear(),
            ..Effect::default()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;

    fn send(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        }
    }

    fn paragraph(latter: &str) -> Pane {
        let mut pane = Pane::new(Rect::new(0, 0, 12, 4));
        pane.line(0, true);
        pane.glyph(1, 0, 2, "界");
        pane.glyph(3, 0, 1, " ");
        pane.line(1, false);
        pane.glyph(1, 1, 1, "e\u{301}");
        pane.line(2, false);
        pane.glyph(1, 2, 4, "\t");
        pane.glyph(5, 2, 1, "x");
        pane.glyph(1, 3, 1, latter);
        pane
    }

    #[test]
    fn drag_copies_whole_glyphs_and_only_logical_breaks() {
        let left = MouseButton::Left;
        let mut selection = Selection::default();
        selection.frame(paragraph("old"), Pane::default(), Pane::default());
        selection.mouse(send(MouseEventKind::Down(left), 2, 0), None);
        let drag = selection.mouse(send(MouseEventKind::Drag(left), 5, 2), None);
        assert!(!drag.selection_released);
        assert!(selection.copy_text().is_none());
        let release = selection.mouse(send(MouseEventKind::Up(left), 5, 2), None);
        assert!(release.selection_released);
        let duplicate = selection.mouse(send(MouseEventKind::Up(left), 5, 2), None);
        assert!(!duplicate.selection_released);
        let Some(CopyText::Text(text)) = selection.copy_text() else {
            panic!("drag did not create a selection");
        };
        assert_eq!(text, "界 e\u{301}\n\tx");
        let mut appended = paragraph("new");
        appended.glyph(6, 2, 1, "y");
        selection.frame(appended, Pane::default(), Pane::default());
        assert!(
            matches!(selection.copy_text(), Some(CopyText::Text(text)) if text == "界 e\u{301}\n\tx")
        );
        let mut changed = paragraph("new");
        changed.rows[1] = None;
        changed.glyph(1, 1, 1, "z");
        selection.frame(changed, Pane::default(), Pane::default());
        assert!(selection.copy_text().is_none());
    }

    #[test]
    fn same_cell_report_is_a_click_but_dragging_back_never_toggles() {
        let left = MouseButton::Left;
        let mut selection = Selection::default();
        selection.frame(paragraph("tail"), Pane::default(), Pane::default());
        let card = ("run".into(), "call".into());
        selection.mouse(send(MouseEventKind::Down(left), 1, 0), Some(&card));
        selection.mouse(send(MouseEventKind::Drag(left), 1, 0), Some(&card));
        let click = selection.mouse(send(MouseEventKind::Up(left), 1, 0), Some(&card));
        assert_eq!(click.click, Some(card.clone()));
        assert!(!click.selection_released);
        selection.mouse(send(MouseEventKind::Down(left), 1, 0), Some(&card));
        selection.mouse(send(MouseEventKind::Drag(left), 3, 0), Some(&card));
        let release = selection.mouse(send(MouseEventKind::Up(left), 1, 0), Some(&card));
        assert!(release.click.is_none());
        assert!(release.selection_released);
        assert!(matches!(selection.copy_text(), Some(CopyText::Text(text)) if text == "界"));
    }

    #[test]
    fn changing_text_does_not_cancel_the_same_card_click() {
        let left = MouseButton::Left;
        let card = ("run".into(), "call".into());
        let mut selection = Selection::default();
        selection.frame(paragraph("1"), Pane::default(), Pane::default());
        selection.mouse(send(MouseEventKind::Down(left), 1, 3), Some(&card));
        selection.frame(paragraph("2"), Pane::default(), Pane::default());
        let click = selection.mouse(send(MouseEventKind::Up(left), 1, 3), Some(&card));
        assert_eq!(click.click, Some(card.clone()));

        selection.mouse(send(MouseEventKind::Down(left), 1, 3), Some(&card));
        selection.frame(Pane::default(), Pane::default(), Pane::default());
        selection.frame(paragraph("2"), Pane::default(), Pane::default());
        let click = selection.mouse(send(MouseEventKind::Up(left), 1, 3), Some(&card));
        assert!(click.click.is_none());
    }

    #[test]
    fn dragging_outside_the_origin_pane_never_copies_a_neighbor() {
        let left = MouseButton::Left;
        let mut draft = Pane::new(Rect::new(0, 4, 12, 1));
        draft.glyph(1, 4, 1, "private draft");
        let mut selection = Selection::default();
        selection.frame(paragraph("b"), draft, Pane::default());
        selection.mouse(send(MouseEventKind::Down(left), 5, 2), None);
        selection.mouse(send(MouseEventKind::Up(left), 1, 4), None);
        assert!(matches!(selection.copy_text(), Some(CopyText::Text(text)) if text == "x\nb"));
    }

    #[test]
    fn selection_over_copy_limit_is_not_partially_copied() {
        let left = MouseButton::Left;
        let mut pane = Pane::new(Rect::new(0, 0, 4, 1));
        pane.glyph(0, 0, 1, &"a".repeat(MAX_COPY_BYTES));
        pane.glyph(1, 0, 1, "b");
        let mut selection = Selection::default();
        selection.frame(pane, Pane::default(), Pane::default());
        selection.mouse(send(MouseEventKind::Down(left), 0, 0), None);
        selection.mouse(send(MouseEventKind::Up(left), 1, 0), None);
        assert!(matches!(selection.copy_text(), Some(CopyText::TooLarge)));
        assert!(selection.clear());
    }

    #[test]
    fn chrome_outside_text_panes_copies_as_painted_rows() {
        let mut buffer = Buffer::empty(Rect::new(0, 0, 12, 4));
        buffer.set_string(0, 0, "transcript", Style::new());
        buffer.set_string(0, 2, "┌ Message ┐", Style::new());
        buffer.set_string(0, 3, " 漢 status", Style::new());
        // The transcript pane owns row 0-1, including its blank row.
        let mut transcript = Pane::new(Rect::new(0, 0, 12, 2));
        for (x, letter) in (0..).zip(["t", "r"]) {
            transcript.glyph(x, 0, 1, letter);
        }
        let mut selection = Selection::default();
        selection.frame(transcript, Pane::default(), Pane::screen(&buffer));

        // Padding after the status text is dropped, not copied.
        selection.mouse(send(MouseEventKind::Down(MouseButton::Left), 0, 3), None);
        selection.mouse(send(MouseEventKind::Up(MouseButton::Left), 11, 3), None);
        assert!(
            matches!(selection.copy_text(), Some(CopyText::Text(text)) if text == " 漢 status")
        );
        let mut buffer = buffer;
        buffer.set_string(0, 1, "a   ", Style::new());
        let transcript = Pane::new(Rect::new(0, 0, 12, 1));
        selection.frame(transcript, Pane::default(), Pane::screen(&buffer));
        selection.mouse(send(MouseEventKind::Down(MouseButton::Left), 0, 1), None);
        selection.mouse(send(MouseEventKind::Up(MouseButton::Left), 11, 2), None);
        assert!(
            matches!(selection.copy_text(), Some(CopyText::Text(text)) if text == "a\n┌ Message ┐")
        );
        selection.frame(
            Pane::new(Rect::new(0, 0, 12, 2)),
            Pane::default(),
            Pane::screen(&buffer),
        );

        selection.mouse(send(MouseEventKind::Down(MouseButton::Left), 0, 2), None);
        let effect = selection.mouse(send(MouseEventKind::Up(MouseButton::Left), 11, 3), None);
        assert!(effect.selection_released);
        assert!(!effect.hold_transcript);
        assert!(
            matches!(selection.copy_text(), Some(CopyText::Text(text)) if text == "┌ Message ┐\n 漢 status")
        );

        // A blank transcript cell never starts a raw screen selection.
        selection.mouse(send(MouseEventKind::Down(MouseButton::Left), 5, 1), None);
        selection.mouse(send(MouseEventKind::Up(MouseButton::Left), 5, 3), None);
        assert!(selection.copy_text().is_none());
    }
}
