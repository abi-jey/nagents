use ratatui::{
    style::Style,
    text::{Line, Span},
};

pub(super) struct Glyph<'a> {
    pub(super) drawn: &'a str,
    pub(super) copied: &'a str,
    pub(super) width: u16,
}

pub(super) fn glyph(symbol: &str, available: u16) -> Glyph<'_> {
    let cells = Span::raw(symbol).width();
    let (drawn, copied, width) = if symbol == "\t" {
        (" ", symbol, 1)
    } else if cells > usize::from(available) {
        ("�", "�", 1)
    } else {
        (symbol, symbol, cells as u16)
    };
    Glyph {
        drawn,
        copied,
        width,
    }
}

/// Reflow once. Store only the soft-break offsets, not a second rendered
/// transcript, so even a long logical line can be painted a viewport at a time.
/// Painting and selection then use the same styled slices of this line.
pub(super) struct Wrapped<'a> {
    line: Line<'a>,
    breaks: Vec<usize>,
    bytes: usize,
}

impl<'a> Wrapped<'a> {
    pub(super) fn new(line: Line<'a>, width: u16) -> Self {
        let bytes = line.spans.iter().map(|span| span.content.len()).sum();
        let mut breaks = Vec::new();
        // A glyph never occupies more cells than its UTF-8 byte length, so
        // short lines need no grapheme scan until the paint pass.
        if bytes > usize::from(width) {
            let mut offset = 0;
            let mut used = 0;
            let mut boundary: Option<(usize, usize)> = None;
            for styled in line.styled_graphemes(Style::default()) {
                let symbol = styled.symbol;
                let cells = usize::from(glyph(symbol, width).width);
                if used + cells > usize::from(width) {
                    if let Some((end, through)) = boundary.take() {
                        breaks.push(end);
                        used -= through;
                    }
                    if used + cells > usize::from(width) {
                        breaks.push(offset);
                        used = 0;
                    }
                }
                offset += symbol.len();
                used += cells;
                if symbol != "\u{a0}" && symbol.chars().all(char::is_whitespace) {
                    boundary = Some((offset, used));
                }
            }
        }
        Self {
            line,
            breaks,
            bytes,
        }
    }

    pub(super) fn height(&self) -> usize {
        self.breaks.len() + 1
    }

    pub(super) fn spans(&self, row: usize) -> impl Iterator<Item = Span<'_>> {
        let start = row.checked_sub(1).map_or(0, |index| self.breaks[index]);
        let end = self.breaks.get(row).copied().unwrap_or(self.bytes);
        let mut offset = 0;
        self.line.spans.iter().filter_map(move |span| {
            let from = start.max(offset);
            let base = offset;
            offset += span.content.len();
            let to = end.min(offset);
            (from < to).then(|| {
                Span::styled(
                    &span.content[from - base..to - base],
                    self.line.style.patch(span.style),
                )
            })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap_preserves_styled_text_whitespace_and_grapheme_boundaries() {
        let source = Line::from(vec![
            Span::raw("a 界"),
            Span::styled("e\u{301} b", Style::new().fg(ratatui::style::Color::Green)),
        ]);
        let wrapped = Wrapped::new(source, 4);
        let rows = (0..wrapped.height())
            .map(|row| wrapped.spans(row).collect::<Line<'_>>().to_string())
            .collect::<Vec<_>>();
        assert_eq!(rows, ["a ", "界e\u{301} ", "b"]);
    }
}
