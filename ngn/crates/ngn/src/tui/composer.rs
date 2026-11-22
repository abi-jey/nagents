use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ngn_core::legacy::PaintedComposerTheme;
use ratatui::{
    buffer::Buffer,
    layout::{Position, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Block,
};

use unicode_segmentation::UnicodeSegmentation;

use crate::view;

use super::selection::Pane;

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
        self.user_surface().unwrap_or_default()
    }

    pub(super) fn user_surface(self) -> Option<Style> {
        self.0
            .map(|(shade, text, _)| Style::new().bg(shade).fg(text))
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
    /// Byte offset of the grapheme in the sanitised draft.
    start: usize,
}

impl Glyph<'_> {
    fn whitespace(self) -> bool {
        self.symbol != "\u{a0}" && self.symbol.chars().all(char::is_whitespace)
    }
}

/// Where typing goes in the draft. It remembers the draft length it was set
/// for: when anything else replaces the draft, it falls back to the end.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(super) struct Cursor(Option<(usize, usize)>);

impl Cursor {
    pub(super) fn at(self, input: &str) -> usize {
        match self.0 {
            Some((at, len)) if len == input.len() && input.is_char_boundary(at) => at,
            _ => input.len(),
        }
    }

    fn set(&mut self, input: &str, at: usize) {
        self.0 = (at < input.len()).then_some((at, input.len()));
    }

    /// Insert at the cursor and move past the inserted text.
    pub(super) fn insert(&mut self, input: &mut String, text: &str) {
        let at = self.at(input);
        input.insert_str(at, text);
        self.set(input, at + text.len());
    }

    /// Apply a cursor movement or deletion key. Returns `None` when the key
    /// is not an editing key, otherwise whether the draft text changed.
    pub(super) fn key(&mut self, input: &mut String, key: KeyEvent) -> Option<bool> {
        let at = self.at(input);
        let word = key
            .modifiers
            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT);
        let target = match key.code {
            KeyCode::Left if word => word_start(input, at),
            KeyCode::Left => previous_grapheme(input, at),
            KeyCode::Right if word => word_end(input, at),
            KeyCode::Right => next_grapheme(input, at),
            KeyCode::Home if key.modifiers.contains(KeyModifiers::CONTROL) => 0,
            KeyCode::End if key.modifiers.contains(KeyModifiers::CONTROL) => input.len(),
            KeyCode::Home => line_start(input, at),
            KeyCode::End => line_end(input, at),
            // Single-line drafts leave Up/Down free for other bindings.
            KeyCode::Up | KeyCode::Down if !input.contains('\n') => return None,
            KeyCode::Up => vertical(input, at, false),
            KeyCode::Down => vertical(input, at, true),
            KeyCode::Backspace | KeyCode::Delete => {
                let (from, to) = match (key.code, word) {
                    (KeyCode::Backspace, true) => (word_start(input, at), at),
                    (KeyCode::Backspace, false) => (previous_grapheme(input, at), at),
                    (_, true) => (at, word_end(input, at)),
                    (_, false) => (at, next_grapheme(input, at)),
                };
                if from == to {
                    return Some(false);
                }
                input.replace_range(from..to, "");
                self.set(input, from);
                return Some(true);
            }
            _ => return None,
        };
        self.set(input, target);
        Some(false)
    }
}

fn previous_grapheme(input: &str, at: usize) -> usize {
    input[..at]
        .grapheme_indices(true)
        .next_back()
        .map_or(0, |(index, _)| index)
}

fn next_grapheme(input: &str, at: usize) -> usize {
    input[at..]
        .graphemes(true)
        .next()
        .map_or(at, |grapheme| at + grapheme.len())
}

fn word_start(input: &str, at: usize) -> usize {
    let head = input[..at].trim_end();
    head.rfind(char::is_whitespace).map_or(0, |index| {
        index + head[index..].chars().next().map_or(1, char::len_utf8)
    })
}

fn word_end(input: &str, at: usize) -> usize {
    let tail = &input[at..];
    let skipped = tail.len() - tail.trim_start().len();
    let rest = &tail[skipped..];
    at + skipped + rest.find(char::is_whitespace).unwrap_or(rest.len())
}

fn line_start(input: &str, at: usize) -> usize {
    input[..at].rfind('\n').map_or(0, |index| index + 1)
}

fn line_end(input: &str, at: usize) -> usize {
    input[at..]
        .find('\n')
        .map_or(input.len(), |index| at + index)
}

/// Move to the same grapheme column on the previous or next line.
fn vertical(input: &str, at: usize, down: bool) -> usize {
    let start = line_start(input, at);
    let column = input[start..at].graphemes(true).count();
    let target = if down {
        let end = line_end(input, at);
        if end == input.len() {
            return input.len();
        }
        end + 1
    } else {
        if start == 0 {
            return 0;
        }
        line_start(input, start - 1)
    };
    let line = &input[target..line_end(input, target)];
    target
        + line
            .grapheme_indices(true)
            .nth(column)
            .map_or(line.len(), |(index, _)| index)
}

/// The cursor as a byte offset into `view::plain(input)`. An offset that is
/// out of range or inside a character means the end of the draft.
fn plain_cursor(input: &str, cursor: usize) -> usize {
    input
        .get(..cursor)
        .map_or_else(|| view::plain(input).len(), |head| view::plain(head).len())
}

/// Lay out graphemes once for both drawing and locating the caret. Keep a
/// cell in reserve as Textual does. The visible rows follow the caret, which
/// is the draft's tail while typing at the end.
pub(super) fn draw(
    buffer: &mut Buffer,
    area: Rect,
    input: &str,
    cursor: usize,
    caret_visible: bool,
    palette: Palette,
    mut pane: Option<&mut Pane>,
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
    let cursor = plain_cursor(input, cursor);
    let height = usize::from(area.height);
    let mut rows: Vec<(Vec<Glyph>, bool)> = Vec::new();
    let mut row = Vec::new();
    let mut column = 0;
    let mut caret: Option<(usize, usize)> = None;
    let mut glyphs = Vec::new();
    let mut line_start = 0;
    // Unlike str::lines, split keeps the empty visual row after a final newline.
    for (index, line) in text.split('\n').enumerate() {
        if index > 0 {
            // The caret sits at the end of the previous line, before its newline.
            if caret.is_none() && cursor < line_start {
                caret = Some((rows.len(), column));
            }
            rows.push((std::mem::take(&mut row), false));
            column = 0;
        }
        glyphs.clear();
        let mut logical_column = 0;
        for (offset, symbol) in line.grapheme_indices(true) {
            let start = line_start + offset;
            if symbol == "\t" {
                let spaces = tab_stop - logical_column % tab_stop;
                glyphs.push(Glyph {
                    symbol,
                    width: spaces,
                    start,
                });
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
                glyphs.push(Glyph {
                    symbol,
                    width,
                    start,
                });
                logical_column += width;
            }
        }
        // Wrap on word boundaries, folding overlong words without splitting graphemes.
        for word in glyphs.chunk_by(|left, right| !left.whitespace() || right.whitespace()) {
            if column > 0
                && column + word.iter().map(|glyph| glyph.width).sum::<usize>() > wrap_width
            {
                rows.push((std::mem::take(&mut row), true));
                column = 0;
            }
            for &glyph in word {
                if column > 0 && column + glyph.width > wrap_width {
                    rows.push((std::mem::take(&mut row), true));
                    column = 0;
                }
                if caret.is_none() && glyph.start >= cursor {
                    caret = Some((rows.len(), column));
                }
                row.push(glyph);
                column += glyph.width;
            }
        }
        line_start += line.len() + 1;
    }
    rows.push((row, false));
    let caret = caret.unwrap_or_else(|| {
        // At tiny widths the text can use every cell, leaving the caret on the next row.
        if column == available {
            rows.push((Vec::new(), false));
            (rows.len() - 1, 0)
        } else {
            (rows.len() - 1, column)
        }
    });
    // Show the tail unless the caret has moved above it.
    let tail = rows.len().saturating_sub(height);
    let first = if caret.0 < tail { caret.0 } else { tail };
    let mut y = area.y;
    for (row, soft_wrap) in rows.iter().skip(first).take(height) {
        if let Some(pane) = pane.as_deref_mut() {
            pane.line(y, *soft_wrap);
        }
        let mut x = area.x;
        for glyph in row {
            if glyph.symbol != "\t" {
                buffer[(x, y)].set_symbol(glyph.symbol);
            }
            if let Some(pane) = pane.as_deref_mut() {
                pane.glyph(x, y, glyph.width as u16, glyph.symbol);
            }
            x += glyph.width as u16;
        }
        y += 1;
    }
    let position = Position::new(area.x + caret.1 as u16, area.y + (caret.0 - first) as u16);
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
                draw(
                    &mut buffer,
                    area,
                    input,
                    usize::MAX,
                    visible,
                    Palette::default(),
                    None
                ),
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
    fn visible_draft_selection_keeps_tabs_and_hard_line_breaks() {
        use crate::tui::selection::{CopyText, Selection};
        use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};

        let area = Rect::new(2, 1, 8, 2);
        let mut buffer = Buffer::empty(Rect::new(0, 0, 10, 3));
        let mut pane = Pane::new(area);
        draw(
            &mut buffer,
            area,
            "ab\t界e\u{301}\nend",
            usize::MAX,
            false,
            Palette::default(),
            Some(&mut pane),
        );
        let mut selection = Selection::default();
        selection.frame(Pane::default(), pane, Pane::default());
        for (kind, column, row) in [
            (MouseEventKind::Down(MouseButton::Left), 3, 1),
            (MouseEventKind::Up(MouseButton::Left), 4, 2),
        ] {
            selection.mouse(
                MouseEvent {
                    kind,
                    row,
                    column,
                    modifiers: KeyModifiers::NONE,
                },
                None,
            );
        }
        assert!(
            matches!(selection.copy_text(), Some(CopyText::Text(text)) if text == "b\t界e\u{301}\nend")
        );
    }

    #[test]
    fn tiny_or_clipped_areas_never_paint_the_caret_outside_the_composer() {
        assert_draft("界", 2, 2, &["界", ""], (0, 1));
        assert_draft("界", 1, 2, &["�", ""], (0, 1));
        let mut buffer = Buffer::empty(Rect::new(0, 0, 2, 2));
        let palette = Palette::default();
        assert_eq!(
            draw(
                &mut buffer,
                Rect::new(2, 0, 3, 1),
                "a",
                usize::MAX,
                true,
                palette,
                None
            ),
            None
        );
        assert_eq!(
            draw(
                &mut buffer,
                Rect::new(1, 1, 3, 3),
                "a",
                usize::MAX,
                true,
                palette,
                None
            ),
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
                    draw(
                        &mut buffer,
                        area,
                        draft,
                        usize::MAX,
                        caret.visible(now),
                        palette,
                        None
                    ),
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

    #[test]
    fn cursor_moves_by_grapheme_word_and_line_and_edits_in_place() {
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
        let press = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let ctrl = |code| KeyEvent::new(code, KeyModifiers::CONTROL);
        let mut input = String::from("héllo 👩‍💻 world");
        let mut cursor = Cursor::default();
        assert_eq!(cursor.at(&input), input.len());

        // " world" is six graphemes; the seventh Left steps over the whole emoji.
        for _ in 0..7 {
            cursor.key(&mut input, press(KeyCode::Left));
        }
        assert_eq!(&input[cursor.at(&input)..], "👩‍💻 world");
        cursor.key(&mut input, press(KeyCode::Right));
        assert_eq!(&input[cursor.at(&input)..], " world");
        cursor.insert(&mut input, "!");
        assert_eq!(input, "héllo 👩‍💻! world");
        assert_eq!(
            cursor.key(&mut input, press(KeyCode::Backspace)),
            Some(true)
        );
        assert_eq!(
            cursor.key(&mut input, press(KeyCode::Backspace)),
            Some(true)
        );
        assert_eq!(input, "héllo  world");
        cursor.key(&mut input, ctrl(KeyCode::Left));
        assert_eq!(cursor.at(&input), 0);
        cursor.key(&mut input, ctrl(KeyCode::Right));
        assert_eq!(&input[cursor.at(&input)..], "  world");
        assert_eq!(cursor.key(&mut input, press(KeyCode::Delete)), Some(true));
        assert_eq!(input, "héllo world");
        cursor.key(&mut input, press(KeyCode::Home));
        assert_eq!(cursor.at(&input), 0);
        cursor.key(&mut input, press(KeyCode::End));
        assert_eq!(cursor.at(&input), input.len());

        // Up/Down only act on multi-line drafts and keep the column.
        assert_eq!(cursor.key(&mut input, press(KeyCode::Up)), None);
        let mut input = String::from("abcd\nxy\nlonger");
        let mut cursor = Cursor::default();
        cursor.key(&mut input, press(KeyCode::Up));
        assert_eq!(&input[cursor.at(&input)..], "\nlonger");
        cursor.key(&mut input, press(KeyCode::Up));
        assert_eq!(&input[cursor.at(&input)..], "cd\nxy\nlonger");
        cursor.key(&mut input, press(KeyCode::Down));
        assert_eq!(&input[cursor.at(&input)..], "\nlonger");

        // Replacing the draft elsewhere puts the cursor back at the end.
        input = "new draft".into();
        assert_eq!(cursor.at(&input), input.len());
    }

    #[test]
    fn caret_is_drawn_at_the_cursor_and_kept_visible() {
        let area = Rect::new(0, 0, 6, 2);
        let mut buffer = Buffer::empty(area);
        let draft = "one\ntwo\nthree\nfour";
        // Caret on the 'w' of "two": rows scroll up to show it.
        let at = draft.find('w').unwrap();
        let position = draw(&mut buffer, area, draft, at, true, Palette::default(), None).unwrap();
        assert_eq!(position, Position::new(1, 0));
        let row =
            |buffer: &Buffer, y| -> String { (0..6).map(|x| buffer[(x, y)].symbol()).collect() };
        assert_eq!(row(&buffer, 0).trim_end(), "two");
        assert!(buffer[(1, 0)].modifier.contains(Modifier::REVERSED));
        // At the end the tail is shown as before.
        let mut buffer = Buffer::empty(area);
        let position = draw(
            &mut buffer,
            area,
            draft,
            usize::MAX,
            true,
            Palette::default(),
            None,
        )
        .unwrap();
        assert_eq!(position, Position::new(4, 1));
        assert_eq!(row(&buffer, 1).trim_end(), "four");
    }
}
