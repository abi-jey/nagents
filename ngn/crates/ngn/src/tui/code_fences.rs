use std::{collections::HashMap, ops::Range};

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::Span,
};
use unicode_segmentation::UnicodeSegmentation;

use crate::view::FenceKey;

use super::selection::Pane;

pub(super) struct Fence {
    pub key: FenceKey,
    pub rows: Range<usize>,
    pub max_offset: usize,
}

#[derive(Default)]
pub(super) struct Pan {
    fences: Vec<Fence>,
    offsets: HashMap<FenceKey, usize>,
    selected: Option<FenceKey>,
    focused: bool,
    reveal: bool,
}

impl Pan {
    pub(super) fn focused(&self) -> bool {
        self.focused
    }

    pub(super) fn blur(&mut self) {
        self.focused = false;
        self.reveal = false;
    }

    pub(super) fn can_pan(&self) -> bool {
        self.fences.iter().any(|fence| fence.max_offset != 0)
    }

    pub(super) fn observe(&mut self, fences: Vec<Fence>) {
        let bounds: HashMap<_, _> = fences
            .iter()
            .map(|fence| (fence.key, fence.max_offset))
            .collect();
        self.offsets.retain(|key, offset| {
            *offset = (*offset).min(bounds.get(key).copied().unwrap_or(0));
            *offset != 0
        });
        if self.selected.is_some_and(|key| !bounds.contains_key(&key)) {
            self.selected = None;
        }
        self.fences = fences;
    }

    pub(super) fn offset(&self, key: FenceKey) -> usize {
        self.offsets.get(&key).copied().unwrap_or(0)
    }

    pub(super) fn selected(&self, key: FenceKey) -> bool {
        self.focused && self.selected == Some(key)
    }

    pub(super) fn reveal(&mut self, top: usize, height: usize) -> usize {
        if !self.reveal || height == 0 {
            return top;
        }
        self.reveal = false;
        self.fences
            .iter()
            .find(|fence| Some(fence.key) == self.selected)
            // Do not snap to the start of a tall block if already inside it.
            .map_or(top, |fence| {
                top.clamp(
                    fence.rows.start.saturating_sub(height - 1),
                    fence.rows.end - 1,
                )
            })
    }

    pub(super) fn key(&mut self, key: KeyEvent, visible: Range<usize>) -> bool {
        if key.code == KeyCode::F(8) && key.modifiers.is_empty() {
            if self.focused {
                self.blur();
            } else {
                let mut targets = self.fences.iter().filter(|fence| fence.max_offset != 0);
                let target = targets
                    .clone()
                    .rfind(|fence| fence.rows.start < visible.end)
                    .or_else(|| targets.next());
                if let Some(target) = target {
                    let selected_visible = self.fences.iter().any(|fence| {
                        Some(fence.key) == self.selected
                            && fence.max_offset != 0
                            && fence.rows.start < visible.end
                            && fence.rows.end > visible.start
                    });
                    if !selected_visible {
                        self.selected = Some(target.key);
                    }
                    self.focused = true;
                    self.reveal = true;
                }
            }
            return true;
        }
        if !self.focused
            || matches!(
                key.code,
                KeyCode::F(_) | KeyCode::PageUp | KeyCode::PageDown
            )
        {
            return false;
        }
        if key.modifiers.is_empty() {
            match key.code {
                KeyCode::Esc => self.blur(),
                KeyCode::Up | KeyCode::Down => {
                    let current = self
                        .fences
                        .iter()
                        .position(|f| Some(f.key) == self.selected);
                    let mut targets = self
                        .fences
                        .iter()
                        .enumerate()
                        .filter(|(_, fence)| fence.max_offset != 0);
                    let next = if key.code == KeyCode::Up {
                        targets.rfind(|(index, _)| current.is_none_or(|n| *index < n))
                    } else {
                        targets.find(|(index, _)| current.is_none_or(|n| *index > n))
                    };
                    if let Some((_, fence)) = next {
                        self.selected = Some(fence.key);
                    }
                    self.reveal = true;
                }
                KeyCode::Left | KeyCode::Right | KeyCode::Home | KeyCode::End => {
                    if let Some(fence) = self.fences.iter().find(|f| Some(f.key) == self.selected) {
                        let offset = self.offset(fence.key);
                        let next = match key.code {
                            KeyCode::Left => offset.saturating_sub(4),
                            KeyCode::Right => (offset + 4).min(fence.max_offset),
                            KeyCode::Home => 0,
                            _ => fence.max_offset,
                        };
                        self.offsets.insert(fence.key, next);
                        self.reveal = true;
                    }
                }
                _ => (),
            }
        }
        // No draft edits or submits while inspecting code.
        true
    }
}

fn glyphs(line: &str) -> impl Iterator<Item = (usize, usize, &str)> {
    // Ratatui's styled graphemes drop tabs; keep them for the code fence tab stops.
    line.graphemes(true)
        .filter(|symbol| *symbol == "\t" || !symbol.contains(char::is_control))
        .scan(0, |column, symbol| {
            let width = if symbol == "\t" {
                4 - *column % 4
            } else {
                Span::raw(symbol).width()
            };
            let cell = (*column, width, symbol);
            *column += width;
            Some(cell)
        })
}

pub(super) fn width(text: &str) -> usize {
    if text.is_ascii() && !text.contains('\t') {
        return text.len();
    }
    glyphs(text)
        .last()
        .map_or(0, |(start, width, _)| start + width)
}

pub(super) fn draw(
    buffer: &mut Buffer,
    area: Rect,
    row: u16,
    text: &str,
    offset: usize,
    selected: bool,
    mut pane: Option<&mut Pane>,
) {
    let y = area.y + row;
    if let Some(pane) = pane.as_deref_mut() {
        pane.line(y, false);
    }
    // ANSI defaults for panel/text and ANSI green for the code role.
    let panel = Style::new().bg(Color::Reset).fg(Color::Reset);
    buffer.set_style(Rect::new(area.x, y, area.width, 1), panel);
    let mut border = panel.fg(Color::Green);
    if selected {
        border = border.add_modifier(Modifier::REVERSED);
    }
    buffer.set_string(area.x, y, "│", border);
    if area.width == 1 {
        return;
    }
    let right = offset + usize::from(area.width - 1);
    if pane.is_none() && text.is_ascii() && !text.contains('\t') {
        if offset < text.len() {
            buffer.set_string(area.x + 1, y, &text[offset..right.min(text.len())], panel);
        }
        return;
    }
    for (column, width, symbol) in glyphs(text) {
        if column + width > right {
            break;
        }
        // A glyph straddling either edge is left blank, not split or shifted.
        // Tabs occupy their original columns (including a clipped tab).
        if column >= offset && width != 0 {
            let x = area.x + 1 + (column - offset) as u16;
            if symbol != "\t" {
                buffer.set_stringn(x, y, symbol, width, panel);
            }
            if let Some(pane) = pane.as_deref_mut() {
                pane.glyph(x, y, width as u16, symbol);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clips_both_edges_without_splitting_graphemes_or_moving_tab_stops() {
        let text = "a界e\u{301}\t👩‍💻z";
        assert_eq!(width(text), 11);
        let area = Rect::new(3, 2, 7, 1);
        let render = |offset| {
            let mut buffer = Buffer::empty(area);
            draw(&mut buffer, area, 0, text, offset, false, None);
            buffer
                .content
                .iter()
                .map(|cell| cell.symbol())
                .collect::<Vec<_>>()
                .join("")
        };
        assert_eq!(render(2), "│ e\u{301}    "); // inside 界 at left edge
        assert_eq!(render(3), "│e\u{301}     "); // inside emoji at right edge
        assert_eq!(render(5), "│   👩‍💻 z"); // inside a tab, both emoji cells fit
    }
}
