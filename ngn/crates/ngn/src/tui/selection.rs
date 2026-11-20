use std::io::{self, Write};

use base64::{Engine, engine::general_purpose::STANDARD};
use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    buffer::Buffer,
    layout::{Position, Rect},
    style::Modifier,
    text::Span,
};

// Keep the encoded OSC 52 payload below the common 100 KiB terminal limit.
const MAX_BYTES: usize = 64 * 1024;

#[derive(Default)]
pub(super) struct Selection {
    gesture: Option<Gesture>,
    text: String,
}

struct Gesture {
    from: Position,
    to: Position,
    viewport: (Rect, usize),
    dragging: bool,
    extended: bool,
}

impl Selection {
    pub(super) fn clear(&mut self) -> bool {
        self.text.clear();
        self.gesture.take().is_some_and(|gesture| gesture.extended)
    }

    pub(super) fn mouse(&mut self, mouse: MouseEvent, area: Rect, top: usize) -> bool {
        let point = Position::new(mouse.column, mouse.row);
        if !mouse.modifiers.is_empty() {
            return self.clear();
        }
        match mouse.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                let changed = self.clear();
                if area.contains(point) {
                    self.gesture = Some(Gesture {
                        from: point,
                        to: point,
                        viewport: (area, top),
                        dragging: true,
                        extended: false,
                    });
                }
                changed
            }
            MouseEventKind::Drag(MouseButton::Left) | MouseEventKind::Up(MouseButton::Left) => {
                let Some(g) = self.gesture.as_mut().filter(|g| g.dragging) else {
                    return false;
                };
                if g.viewport != (area, top) {
                    return self.clear();
                }
                g.dragging = matches!(mouse.kind, MouseEventKind::Drag(_));
                let next = Position::new(
                    point.x.clamp(area.left(), area.right() - 1),
                    point.y.clamp(area.top(), area.bottom() - 1),
                );
                let previous = g.to;
                g.to = next;
                g.extended |= g.dragging || next != g.from;
                if !g.extended {
                    self.gesture = None;
                    return false;
                }
                previous != next || !g.dragging
            }
            MouseEventKind::Moved => false,
            _ => self.clear(),
        }
    }

    pub(super) fn draw(&mut self, buffer: &mut Buffer, area: Rect, top: usize) {
        if self
            .gesture
            .as_ref()
            .is_some_and(|g| g.viewport != (area, top))
        {
            self.clear();
            return;
        }
        let Some(g) = self.gesture.as_ref().filter(|g| g.extended) else {
            return;
        };
        let mut from = g.from;
        let mut to = g.to;
        if (from.y, from.x) > (to.y, to.x) {
            std::mem::swap(&mut from, &mut to);
        }
        self.text.clear();
        let mut limited = false;
        for y in from.y..=to.y {
            let left = if y == from.y { from.x } else { area.left() };
            let right = if y == to.y { to.x } else { area.right() - 1 };
            if y != from.y && !limited {
                if self.text.len() < MAX_BYTES {
                    self.text.push('\n');
                } else {
                    limited = true;
                }
            }
            let first = self.text.len();
            let mut x = area.left();
            while x <= right {
                let cell = &buffer[(x, y)];
                let symbol = cell.symbol();
                let next = x.saturating_add(Span::raw(symbol).width().max(1) as u16);
                if next > left {
                    if !limited {
                        if symbol.len() <= MAX_BYTES - self.text.len() {
                            self.text.push_str(symbol);
                        } else {
                            limited = true;
                        }
                    }
                    for col in x..next.min(area.right()) {
                        buffer[(col, y)].modifier.insert(Modifier::REVERSED);
                    }
                }
                if next <= x {
                    break;
                }
                x = next;
            }
            let trimmed = self.text[first..].trim_end_matches(' ').len();
            self.text.truncate(first + trimmed);
        }
    }

    pub(super) fn copy(&self, mut output: impl Write) -> io::Result<bool> {
        if self.text.is_empty() {
            return Ok(false);
        }
        write!(output, "\x1b]52;c;{}\x1b\\", STANDARD.encode(&self.text))?;
        output.flush()?;
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use crossterm::event::KeyModifiers;
    use ratatui::style::Style;

    use super::*;

    #[test]
    fn drag_copies_visible_rows_in_order_including_wide_glyphs() {
        let area = Rect::new(2, 3, 7, 2);
        let mut buffer = Buffer::empty(Rect::new(0, 0, 12, 8));
        buffer.set_string(2, 3, "a界b", Style::default());
        buffer.set_string(2, 4, "next", Style::default());
        buffer.set_string(9, 3, "rail", Style::default());
        let mouse = |column, row, kind| MouseEvent {
            column,
            row,
            kind,
            modifiers: KeyModifiers::NONE,
        };
        let mut selection = Selection::default();
        selection.mouse(
            mouse(3, 4, MouseEventKind::Down(MouseButton::Left)),
            area,
            10,
        );
        assert!(selection.mouse(
            mouse(4, 3, MouseEventKind::Drag(MouseButton::Left)),
            area,
            10
        ));
        selection.mouse(mouse(4, 3, MouseEventKind::Up(MouseButton::Left)), area, 10);
        selection.draw(&mut buffer, area, 10);
        assert_eq!(selection.text, "界b\nne");
        assert!(buffer[(3, 3)].modifier.contains(Modifier::REVERSED));
        assert!(buffer[(4, 3)].modifier.contains(Modifier::REVERSED));
        assert!(!buffer[(9, 3)].modifier.contains(Modifier::REVERSED));
        let mut output = Vec::new();
        assert!(selection.copy(&mut output).unwrap());
        assert_eq!(output, b"\x1b]52;c;55WMYgpuZQ==\x1b\\");

        selection.draw(&mut buffer, area, 11);
        assert!(!selection.copy(&mut output).unwrap());
    }
}
