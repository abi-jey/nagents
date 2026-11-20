use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};

use ngn_core::legacy::PaintedComposerTheme;
use ratatui::{
    buffer::Buffer,
    layout::{Position, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Block,
};

use crate::view;

const BLINK_INTERVAL: Duration = Duration::from_millis(500);

pub(super) fn border(width: u16, pending: usize, accent: Color, muted: Color) -> Block<'static> {
    const TITLE: &str = " Message ";
    let mut block = Block::bordered()
        .title_top(Line::raw(TITLE).left_aligned())
        .border_style(Style::new().fg(accent));
    if pending > 0 {
        // Leave the title and a border cell intact. At narrower widths keep
        // just the count, and omit it if even that would overlap the title.
        let available = usize::from(width).saturating_sub(TITLE.len() + 3);
        let mut badge = format!(" {pending} pending ");
        if badge.len() > available {
            badge = format!(" {pending} ");
        }
        if badge.len() <= available {
            block = block.title_top(Line::styled(badge, Style::new().fg(muted)).right_aligned());
        }
    }
    block
}

#[derive(Clone, Copy, Default)]
pub(super) struct Palette(Option<(Color, Color, Color)>);

impl Palette {
    pub(super) fn new(theme: Option<PaintedComposerTheme>, no_color: bool) -> Self {
        use Color::Rgb;
        use PaintedComposerTheme::*;
        if no_color {
            return Self::default();
        }
        // Computed ngn-composer-background, -text and -accent from Python
        // themes.py. Textual blends the app background 7% toward white for
        // these dark presets, truncating each channel (not tinting the panel).
        Self(theme.map(|theme| match theme {
            Terminal => (Rgb(37, 40, 47), Rgb(230, 234, 242), Rgb(133, 186, 255)),
            Graphite => (Rgb(37, 40, 47), Rgb(230, 234, 242), Rgb(182, 160, 255)),
            Ocean => (Rgb(29, 44, 54), Rgb(224, 243, 245), Rgb(97, 216, 231)),
            Ember => (Rgb(51, 41, 39), Rgb(245, 232, 220), Rgb(241, 173, 117)),
        }))
    }

    fn surface(self) -> Style {
        self.0.map_or(Style::new(), |(shade, text, _)| {
            Style::new().bg(shade).fg(text)
        })
    }

    fn cursor(self) -> Style {
        self.0.map_or(
            Style::new().add_modifier(Modifier::REVERSED),
            |(shade, _, accent)| Style::new().bg(accent).fg(shade),
        )
    }
}

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
    palette: Palette,
) -> Option<Position> {
    let area = area.intersection(buffer.area);
    if area.is_empty() {
        return None;
    }
    buffer.set_style(area, palette.surface());
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
        buffer[position].set_style(palette.cursor());
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
            assert_eq!(
                draw(&mut buffer, area, input, visible, Palette::default()),
                Some(position)
            );
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
        let palette = Palette::default();
        assert_eq!(
            draw(&mut buffer, Rect::new(2, 0, 3, 1), "a", true, palette),
            None
        );
        assert_eq!(
            draw(&mut buffer, Rect::new(1, 1, 3, 3), "a", true, palette),
            Some(Position::new(1, 1))
        );
    }

    #[test]
    fn draft_surface_and_focused_caret_match_python_composer_roles() {
        use Color::{Reset, Rgb};
        use PaintedComposerTheme::*;
        let area = Rect::new(1, 1, 6, 3);
        let start = Instant::now();
        let draft = "ab\ncd";
        for (theme, no_color, shade, text, accent) in [
            (
                Some(Terminal),
                false,
                Rgb(37, 40, 47),
                Rgb(230, 234, 242),
                Rgb(133, 186, 255),
            ),
            (
                Some(Graphite),
                false,
                Rgb(37, 40, 47),
                Rgb(230, 234, 242),
                Rgb(182, 160, 255),
            ),
            (
                Some(Ocean),
                false,
                Rgb(29, 44, 54),
                Rgb(224, 243, 245),
                Rgb(97, 216, 231),
            ),
            (
                Some(Ember),
                false,
                Rgb(51, 41, 39),
                Rgb(245, 232, 220),
                Rgb(241, 173, 117),
            ),
            (Some(Ocean), true, Reset, Reset, Reset),
            (None, false, Reset, Reset, Reset),
        ] {
            let palette = Palette::new(theme, no_color);
            let mut caret = Caret::new(true);
            for (focused, now, visible) in [
                (true, start, true),
                (true, start + BLINK_INTERVAL, false),
                (false, start + BLINK_INTERVAL, false),
                (true, start + BLINK_INTERVAL, true),
            ] {
                let mut buffer = Buffer::empty(Rect::new(0, 0, 8, 5));
                caret.focus(focused, now);
                assert_eq!(caret.visible(now), visible);
                assert_eq!(
                    draw(&mut buffer, area, draft, caret.visible(now), palette),
                    Some(Position::new(3, 2))
                );
                assert_eq!(buffer[(1, 1)].symbol(), "a");
                assert_eq!(buffer[(2, 2)].symbol(), "d");
                assert_eq!(buffer[(3, 2)].symbol(), " ");
                for y in 0..5 {
                    for x in 0..8 {
                        let cell = &buffer[(x, y)];
                        let cursor = visible && (x, y) == (3, 2);
                        let expected = if !area.contains(Position::new(x, y)) {
                            (Reset, Reset, Modifier::empty())
                        } else if cursor && accent != Reset {
                            (shade, accent, Modifier::empty())
                        } else if cursor {
                            (Reset, Reset, Modifier::REVERSED)
                        } else {
                            (text, shade, Modifier::empty())
                        };
                        assert_eq!((cell.fg, cell.bg, cell.modifier), expected);
                    }
                }
            }
        }
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
