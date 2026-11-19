use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};

use ratatui::{
    buffer::Buffer,
    layout::{Position, Rect},
    style::{Modifier, Style},
    text::Span,
};

use crate::view;

const BLINK_INTERVAL: Duration = Duration::from_millis(500);

#[derive(Default)]
pub(super) struct Caret {
    blink: bool,
    focused_since: Option<Instant>,
}

impl Caret {
    pub(super) fn new(blink: bool) -> Self {
        Self {
            blink,
            ..Self::default()
        }
    }

    pub(super) fn focus(&mut self, focused: bool, now: Instant) {
        if focused {
            self.focused_since.get_or_insert(now);
        } else {
            self.focused_since = None;
        }
    }

    pub(super) fn restart(&mut self, now: Instant) {
        if self.focused_since.is_some() {
            self.focused_since = Some(now);
        }
    }

    pub(super) fn visible(&self, now: Instant) -> bool {
        self.focused_since.is_some_and(|since| {
            !self.blink
                || (now.saturating_duration_since(since).as_millis() / BLINK_INTERVAL.as_millis())
                    .is_multiple_of(2)
        })
    }
}

#[derive(Clone, Copy)]
struct Glyph<'a> {
    symbol: &'a str,
    width: usize,
}

impl Glyph<'_> {
    fn whitespace(self) -> bool {
        self.symbol != "\u{a0}" && self.symbol.chars().all(char::is_whitespace)
    }
}

/// Lay out graphemes once for both drawing and locating the append position.
/// Keep a cell in reserve as Textual does, and follow the draft's visual tail.
pub(super) fn draw(
    buffer: &mut Buffer,
    area: Rect,
    input: &str,
    caret_visible: bool,
) -> Option<Position> {
    let area = area.intersection(buffer.area);
    if area.is_empty() {
        return None;
    }
    let available = usize::from(area.width);
    let wrap_width = available.saturating_sub(1).max(1);
    let tab_stop = wrap_width.min(4);
    let text = view::plain(input);
    // Unlike str::lines, split keeps the empty visual row after a final newline.
    let lines: Vec<_> = text.split('\n').map(Span::raw).collect();
    let height = usize::from(area.height);
    let mut rows = VecDeque::new();
    let mut row = Vec::new();
    let mut column = 0;
    let mut finish_row = |row: &mut Vec<_>| {
        let mut next = if rows.len() == height {
            rows.pop_front().unwrap_or_default()
        } else {
            Vec::new()
        };
        next.clear();
        std::mem::swap(row, &mut next);
        rows.push_back(next);
    };
    let mut glyphs = Vec::new();
    for (index, span) in lines.iter().enumerate() {
        if index > 0 {
            finish_row(&mut row);
            column = 0;
        }
        glyphs.clear();
        let mut logical_column = 0;
        for grapheme in span.styled_graphemes(Style::default()) {
            let symbol = grapheme.symbol;
            if symbol == "\t" {
                let spaces = tab_stop - logical_column % tab_stop;
                glyphs.extend(std::iter::repeat_n(
                    Glyph {
                        symbol: " ",
                        width: 1,
                    },
                    spaces,
                ));
                logical_column += spaces;
                continue;
            }
            let width = Span::raw(symbol).width();
            if width > 0 {
                // On an extremely narrow terminal a wide grapheme may not fit at all.
                let (symbol, width) = if width > available {
                    ("�", 1)
                } else {
                    (symbol, width)
                };
                glyphs.push(Glyph { symbol, width });
                logical_column += width;
            }
        }
        // Wrap on word boundaries, folding overlong words without splitting graphemes.
        for word in glyphs.chunk_by(|left, right| !left.whitespace() || right.whitespace()) {
            if column > 0
                && column + word.iter().map(|glyph| glyph.width).sum::<usize>() > wrap_width
            {
                finish_row(&mut row);
                column = 0;
            }
            for &glyph in word {
                if column > 0 && column + glyph.width > wrap_width {
                    finish_row(&mut row);
                    column = 0;
                }
                row.push(glyph);
                column += glyph.width;
            }
        }
    }
    finish_row(&mut row);
    // At tiny widths the text can use every cell, leaving the caret on the next row.
    if column == available {
        finish_row(&mut row);
        column = 0;
    }
    let mut y = area.y;
    for row in rows {
        let mut x = area.x;
        for glyph in row {
            buffer[(x, y)].set_symbol(glyph.symbol);
            x += glyph.width as u16;
        }
        y += 1;
    }
    let position = Position::new(area.x + column as u16, y - 1);
    if caret_visible {
        buffer[position].set_style(Style::new().add_modifier(Modifier::REVERSED));
    }
    Some(position)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_draft(input: &str, width: u16, height: u16, expected: &[&str], cursor: (u16, u16)) {
        let area = Rect::new(3, 2, width, height);
        for visible in [true, false] {
            let mut buffer = Buffer::empty(Rect::new(0, 0, width + 5, height + 4));
            let position = Position::new(area.x + cursor.0, area.y + cursor.1);
            assert_eq!(draw(&mut buffer, area, input, visible), Some(position));
            assert_eq!(buffer[position].symbol(), " ");
            let rows: Vec<String> = (area.y..area.bottom())
                .map(|y| {
                    (area.x..area.right())
                        .map(|x| buffer[(x, y)].symbol())
                        .collect()
                })
                .collect();
            assert_eq!(
                rows.iter().map(|row| row.trim_end()).collect::<Vec<_>>(),
                expected
            );
            assert_eq!(
                buffer
                    .content
                    .iter()
                    .filter(|cell| cell.modifier.contains(Modifier::REVERSED))
                    .count(),
                usize::from(visible)
            );
            assert_eq!(
                buffer[position].modifier.contains(Modifier::REVERSED),
                visible
            );
        }
    }

    #[test]
    fn append_position_tracks_empty_exact_width_word_wrap_and_trailing_spaces() {
        assert_draft("", 6, 1, &[""], (0, 0));
        assert_draft("abcde", 6, 1, &["abcde"], (5, 0));
        assert_draft("abc def  ", 6, 2, &["abc", "def"], (5, 1));
        assert_draft("abcdefgh", 5, 2, &["abcd", "efgh"], (4, 1));
    }

    #[test]
    fn pasted_multiline_unicode_tail_reflows_on_resize() {
        // Includes a tab, combining mark, ZWJ emoji, double-width CJK and final newline.
        let input = "scrolled away\r\nab\t界e\u{301}👩‍💻\r\n";
        assert_draft(input, 13, 2, &["ab  界 e\u{301}👩‍💻", ""], (0, 1));
        assert_draft(input, 6, 3, &["ab", "界 e\u{301}👩‍💻", ""], (0, 2));
        assert_draft(input.trim_end(), 6, 1, &["界 e\u{301}👩‍💻"], (5, 0));
    }

    #[test]
    fn tiny_or_clipped_areas_never_paint_the_caret_outside_the_composer() {
        assert_draft("界", 2, 2, &["界", ""], (0, 1));
        assert_draft("界", 1, 2, &["�", ""], (0, 1));
        let mut buffer = Buffer::empty(Rect::new(0, 0, 2, 2));
        assert_eq!(draw(&mut buffer, Rect::new(2, 0, 3, 1), "a", true), None);
        assert_eq!(
            draw(&mut buffer, Rect::new(1, 1, 3, 3), "a", true),
            Some(Position::new(1, 1))
        );
    }

    #[test]
    fn blink_restarts_visible_on_input_and_refocus_and_is_steady_with_reduced_motion() {
        let start = Instant::now();
        let mut caret = Caret::new(true);
        assert!(!caret.visible(start));
        caret.focus(true, start);
        assert!(caret.visible(start + BLINK_INTERVAL - Duration::from_millis(1)));
        assert!(!caret.visible(start + BLINK_INTERVAL));
        assert!(caret.visible(start + 2 * BLINK_INTERVAL));
        caret.restart(start + BLINK_INTERVAL);
        assert!(caret.visible(start + BLINK_INTERVAL));
        caret.focus(false, start + BLINK_INTERVAL);
        assert!(!caret.visible(start + BLINK_INTERVAL));
        caret.focus(true, start + 2 * BLINK_INTERVAL);
        assert!(caret.visible(start + 2 * BLINK_INTERVAL));

        let mut steady = Caret::new(false);
        steady.focus(true, start);
        assert!(steady.visible(start + BLINK_INTERVAL));
        steady.focus(false, start + BLINK_INTERVAL);
        assert!(!steady.visible(start + BLINK_INTERVAL));
    }
}
