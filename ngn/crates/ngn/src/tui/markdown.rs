//! Markdown prose rendering for assistant text.
//!
//! The renderer converts Markdown into styled lines that are already wrapped to
//! the requested width. Wrapping here, rather than in `Paragraph`, lets list
//! items hang their continuation rows under the item text and lets block
//! quotes repeat their bar on every row. Every returned line is at most
//! `width` cells wide.
//!
//! Text arrives incrementally, so any prefix of a document renders without
//! failing; unclosed constructs simply appear as the literal text.

use std::{
    cell::RefCell,
    collections::HashMap,
    hash::{DefaultHasher, Hash, Hasher},
    rc::Rc,
};

use pulldown_cmark::{Alignment, Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};

use super::{ACCENT, MUTED};

const CODE: Color = Color::Rgb(224, 190, 120);
const CACHE_ENTRIES: usize = 256;

type Key = (u64, usize, usize);

/// One rendered row plus what mouse selection needs to copy it faithfully.
#[derive(Clone, Debug, PartialEq)]
pub(super) struct Row {
    pub(super) line: Line<'static>,
    /// The row is a soft wrap of the same paragraph as the next row, so a
    /// copy joins them instead of inserting a line break.
    pub(super) joins_next: bool,
    /// Cells of gutter (quote bars, hanging indentation) excluded from copy.
    /// List markers stay copyable.
    pub(super) copy_from: u16,
}

thread_local! {
    static CACHE: RefCell<HashMap<Key, Rc<Vec<Row>>>> = RefCell::new(HashMap::new());
}

/// Render `text` at `width` cells, reusing the previous result when the same
/// text was rendered at the same width. The draw loop rebuilds the transcript
/// at animation rate, so unchanged parts must not be parsed again.
pub(super) fn render_cached(text: &str, width: u16) -> Rc<Vec<Row>> {
    let mut hasher = DefaultHasher::new();
    text.hash(&mut hasher);
    let key = (hasher.finish(), text.len(), usize::from(width));
    if let Some(hit) = CACHE.with(|cache| cache.borrow().get(&key).cloned()) {
        return hit;
    }
    let lines = Rc::new(render_rows(text, usize::from(width)));
    CACHE.with(|cache| {
        let mut cache = cache.borrow_mut();
        if cache.len() >= CACHE_ENTRIES {
            // Streaming replaces the live part on every delta; start over
            // rather than tracking recency for a few hundred small entries.
            cache.clear();
        }
        cache.insert(key, Rc::clone(&lines));
    });
    lines
}

#[cfg(test)]
pub(super) fn render(text: &str, width: usize) -> Vec<Line<'static>> {
    render_rows(text, width)
        .into_iter()
        .map(|row| row.line)
        .collect()
}

pub(super) fn render_rows(text: &str, width: usize) -> Vec<Row> {
    let mut builder = Builder::new(width);
    if width == 0 {
        return Vec::new();
    }
    let options =
        Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TASKLISTS;
    for event in Parser::new_ext(text, options) {
        builder.event(event);
    }
    builder.flush();
    builder
        .out
        .into_iter()
        .zip(builder.meta)
        .map(|(line, (joins_next, copy_from))| Row {
            line,
            joins_next,
            copy_from,
        })
        .collect()
}

fn muted() -> Style {
    Style::new().fg(MUTED)
}

fn dim() -> Style {
    Style::new().fg(MUTED).add_modifier(Modifier::DIM)
}

#[derive(Clone, Copy)]
struct Cell<'a> {
    sym: &'a str,
    style: Style,
    w: usize,
}

fn cells<'a>(spans: &'a [Span<'static>]) -> Vec<Cell<'a>> {
    spans
        .iter()
        .flat_map(|span| span.styled_graphemes(Style::default()))
        .map(|grapheme| Cell {
            sym: grapheme.symbol,
            style: grapheme.style,
            w: Span::raw(grapheme.symbol).width(),
        })
        .collect()
}

fn is_space(cell: &Cell<'_>) -> bool {
    cell.sym.chars().all(char::is_whitespace)
}

fn spans(cells: &[Cell<'_>]) -> Vec<Span<'static>> {
    let mut out: Vec<Span<'static>> = Vec::new();
    let mut run = String::new();
    let mut run_style = Style::default();
    for cell in cells {
        if !run.is_empty() && cell.style != run_style {
            out.push(Span::styled(std::mem::take(&mut run), run_style));
        }
        run_style = cell.style;
        run.push_str(cell.sym);
    }
    if !run.is_empty() {
        out.push(Span::styled(run, run_style));
    }
    out
}

fn total(cells: &[Cell<'_>]) -> usize {
    cells.iter().map(|cell| cell.w).sum()
}

/// Shorten to `width` cells, ending with `…` when anything was removed.
fn truncate<'a>(cells: &[Cell<'a>], width: usize) -> Vec<Cell<'a>> {
    if total(cells) <= width {
        return cells.to_vec();
    }
    let mut out = Vec::new();
    if width == 0 {
        return out;
    }
    let mut used = 0;
    for cell in cells {
        if used + cell.w > width - 1 {
            break;
        }
        used += cell.w;
        out.push(*cell);
    }
    out.push(Cell {
        sym: "…",
        style: cells.last().map_or_else(Style::default, |cell| cell.style),
        w: 1,
    });
    out
}

fn trim_end(line: &mut Vec<Cell<'_>>) {
    while line.last().is_some_and(is_space) {
        line.pop();
    }
}

/// Word wrap, breaking words longer than a row. Wide glyphs that can never
/// fit are dropped so no row exceeds `width`. Each row also reports whether
/// it ended at a (removed) space rather than inside a word.
fn wrap<'a>(cells: &[Cell<'a>], width: usize) -> Vec<(Vec<Cell<'a>>, bool)> {
    let mut lines = Vec::new();
    if width == 0 {
        return lines;
    }
    let mut current: Vec<Cell<'a>> = Vec::new();
    let mut used = 0;
    let mut index = 0;
    while index < cells.len() {
        let cell = cells[index];
        if is_space(&cell) {
            if used > 0 && used < width && !current.last().is_some_and(is_space) {
                current.push(Cell {
                    sym: " ",
                    style: cell.style,
                    w: 1,
                });
                used += 1;
            }
            index += 1;
            continue;
        }
        let end = cells[index..]
            .iter()
            .position(is_space)
            .map_or(cells.len(), |offset| index + offset);
        let word = &cells[index..end];
        let word_width = total(word);
        if used + word_width <= width {
            current.extend_from_slice(word);
            used += word_width;
        } else {
            trim_end(&mut current);
            if !current.is_empty() {
                lines.push((std::mem::take(&mut current), true));
            }
            used = 0;
            if word_width <= width {
                current.extend_from_slice(word);
                used = word_width;
            } else {
                for cell in word {
                    if cell.w > width {
                        continue;
                    }
                    if used + cell.w > width {
                        lines.push((std::mem::take(&mut current), false));
                        used = 0;
                    }
                    current.push(*cell);
                    used += cell.w;
                }
            }
        }
        index = end;
    }
    trim_end(&mut current);
    if !current.is_empty() {
        lines.push((current, false));
    }
    lines
}

fn trim_spans(line: &mut Vec<Span<'static>>) {
    while line
        .last()
        .is_some_and(|span| span.content.trim().is_empty())
    {
        line.pop();
    }
    if let Some(last) = line.last_mut() {
        last.content = last.content.trim_end().to_owned().into();
    }
}

enum Inline {
    Span(Span<'static>),
    Break,
}

enum Ctx {
    Quote {
        content: bool,
    },
    Item {
        marker: Option<String>,
        width: usize,
        content: bool,
    },
}

impl Ctx {
    fn content(&self) -> bool {
        match self {
            Self::Quote { content } | Self::Item { content, .. } => *content,
        }
    }
}

struct Link {
    dest: String,
    text: String,
}

struct TableRow {
    header: bool,
    cells: Vec<Vec<Span<'static>>>,
}

struct Table {
    aligns: Vec<Alignment>,
    rows: Vec<TableRow>,
    row: Option<TableRow>,
}

struct Builder {
    width: usize,
    out: Vec<Line<'static>>,
    /// Per `out` row: (joins the next row, gutter cells not copied).
    meta: Vec<(bool, u16)>,
    /// Set by `flush` while emitting all but the last row of a paragraph.
    joins: bool,
    ctx: Vec<Ctx>,
    lists: Vec<Option<u64>>,
    styles: Vec<Style>,
    inline: Vec<Inline>,
    link: Option<Link>,
    table: Option<Table>,
    code: Option<String>,
    blank: bool,
}

impl Builder {
    fn new(width: usize) -> Self {
        Self {
            width,
            out: Vec::new(),
            meta: Vec::new(),
            joins: false,
            ctx: Vec::new(),
            lists: Vec::new(),
            styles: Vec::new(),
            inline: Vec::new(),
            link: None,
            table: None,
            code: None,
            blank: false,
        }
    }

    fn style(&self) -> Style {
        self.styles
            .iter()
            .fold(Style::default(), |all, style| all.patch(*style))
    }

    fn prefix_width(&self) -> usize {
        self.ctx
            .iter()
            .map(|ctx| match ctx {
                Ctx::Quote { .. } => 2,
                Ctx::Item { width, .. } => *width,
            })
            .sum()
    }

    /// Prefixes that leave no room for text are dropped entirely.
    fn compact(&self) -> bool {
        self.prefix_width() >= self.width
    }

    fn available(&self) -> usize {
        if self.compact() {
            self.width
        } else {
            self.width - self.prefix_width()
        }
    }

    /// The container gutter for one row. A list marker is drawn once, on the
    /// first row emitted for its item; later rows indent under the text.
    fn prefix(&mut self, consume: bool) -> Vec<Span<'static>> {
        let compact = self.compact();
        let mut out = Vec::new();
        for ctx in &mut self.ctx {
            let (text, style) = match ctx {
                Ctx::Quote { .. } => ("▌ ".to_owned(), dim()),
                Ctx::Item { marker, width, .. } => match marker.take_if(|_| consume) {
                    Some(marker) => (marker, muted()),
                    None => (" ".repeat(*width), Style::default()),
                },
            };
            if !compact {
                out.push(Span::styled(text, style));
            }
        }
        out
    }

    fn mark(&mut self) {
        for ctx in &mut self.ctx {
            match ctx {
                Ctx::Quote { content } | Ctx::Item { content, .. } => *content = true,
            }
        }
        self.blank = false;
    }

    fn emit(&mut self, body: Vec<Span<'static>>) {
        let mut line = self.prefix(true);
        let gutter: usize = line
            .iter()
            .take_while(|span| span.content.trim().is_empty() || span.content == "▌ ")
            .map(Span::width)
            .sum();
        line.extend(body);
        self.out.push(Line::from(line));
        self.meta
            .push((self.joins, u16::try_from(gutter).unwrap_or(u16::MAX)));
        self.mark();
    }

    fn blank_line(&mut self) {
        if self.blank || self.out.is_empty() {
            return;
        }
        let mut line = self.prefix(false);
        trim_spans(&mut line);
        self.out.push(Line::from(line));
        self.meta.push((false, 0));
        self.blank = true;
    }

    fn begin_block(&mut self) {
        self.flush();
        if self.ctx.last().is_none_or(Ctx::content) {
            self.blank_line();
        }
    }

    fn begin_list(&mut self) {
        self.flush();
        if !matches!(self.ctx.last(), Some(Ctx::Item { .. })) {
            self.begin_block();
        }
    }

    fn push_text(&mut self, text: &str) {
        let style = self.style();
        if let Some(link) = &mut self.link {
            link.text.push_str(text);
        }
        for (index, piece) in text.split('\n').enumerate() {
            if index > 0 {
                self.inline.push(Inline::Break);
            }
            if !piece.is_empty() {
                self.inline
                    .push(Inline::Span(Span::styled(piece.to_owned(), style)));
            }
        }
    }

    fn flush(&mut self) {
        if self.inline.is_empty() {
            return;
        }
        let inline = std::mem::take(&mut self.inline);
        for segment in inline.split(|part| matches!(part, Inline::Break)) {
            let segment: Vec<Span<'static>> = segment
                .iter()
                .filter_map(|part| match part {
                    Inline::Span(span) => Some(span.clone()),
                    Inline::Break => None,
                })
                .collect();
            let all = cells(&segment);
            let available = self.available();
            let rows = wrap(&all, available);
            let last = rows.len().saturating_sub(1);
            for (index, (row, at_space)) in rows.into_iter().enumerate() {
                let mut row = row;
                // Copy joins soft-wrapped rows verbatim, so put back the space
                // the wrap removed. A full row has no room; it copies as a
                // line break instead of gluing two words together.
                self.joins = index < last && (!at_space || total(&row) < available);
                if index < last && at_space && self.joins {
                    let style = row.last().map_or_else(Style::default, |cell| cell.style);
                    row.push(Cell {
                        sym: " ",
                        style,
                        w: 1,
                    });
                }
                self.emit(spans(&row));
            }
            self.joins = false;
        }
    }

    fn event(&mut self, event: Event<'_>) {
        match event {
            Event::Start(tag) => self.start(tag),
            Event::End(tag) => self.end(tag),
            Event::Text(text) => {
                if let Some(code) = &mut self.code {
                    code.push_str(&text);
                } else {
                    self.push_text(&text);
                }
            }
            Event::Code(code) => {
                let style = self.style().fg(CODE);
                if let Some(link) = &mut self.link {
                    link.text.push_str(&code);
                }
                self.inline
                    .push(Inline::Span(Span::styled(code.into_string(), style)));
            }
            Event::Html(text) | Event::InlineHtml(text) => self.push_text(&text),
            Event::FootnoteReference(label) => self.push_text(&format!("[{label}]")),
            Event::SoftBreak | Event::HardBreak => self.inline.push(Inline::Break),
            Event::Rule => {
                self.begin_block();
                let line = "─".repeat(self.available());
                self.emit(vec![Span::styled(line, dim())]);
            }
            Event::TaskListMarker(checked) => {
                if let Some(Ctx::Item { marker, .. }) = self.ctx.last_mut()
                    && marker.is_some()
                {
                    *marker = Some(if checked { "☑ " } else { "☐ " }.to_owned());
                }
            }
            _ => {}
        }
    }

    fn start(&mut self, tag: Tag<'_>) {
        match tag {
            Tag::Paragraph => self.begin_block(),
            Tag::Heading { level, .. } => {
                self.begin_block();
                let mut style = Style::new().add_modifier(Modifier::BOLD);
                if matches!(level, HeadingLevel::H1 | HeadingLevel::H2) {
                    style = style.fg(ACCENT);
                }
                self.styles.push(style);
            }
            Tag::BlockQuote(_) => {
                self.begin_block();
                self.ctx.push(Ctx::Quote { content: false });
                self.styles
                    .push(Style::new().add_modifier(Modifier::DIM | Modifier::ITALIC));
            }
            Tag::CodeBlock(_) => {
                self.begin_block();
                self.code = Some(String::new());
            }
            Tag::HtmlBlock => self.begin_block(),
            Tag::List(first) => {
                self.begin_list();
                self.lists.push(first);
            }
            Tag::Item => {
                self.flush();
                let depth = self.lists.len();
                let marker = match self.lists.last_mut() {
                    Some(Some(number)) => {
                        let marker = format!("{number}. ");
                        *number += 1;
                        marker
                    }
                    _ => match depth {
                        0 | 1 => "• ",
                        2 => "◦ ",
                        _ => "▪ ",
                    }
                    .to_owned(),
                };
                let width = Span::raw(marker.as_str()).width();
                self.ctx.push(Ctx::Item {
                    marker: Some(marker),
                    width,
                    content: false,
                });
            }
            Tag::Emphasis => self
                .styles
                .push(Style::new().add_modifier(Modifier::ITALIC)),
            Tag::Strong => self.styles.push(Style::new().add_modifier(Modifier::BOLD)),
            Tag::Strikethrough => self
                .styles
                .push(Style::new().add_modifier(Modifier::CROSSED_OUT)),
            Tag::Link { dest_url, .. } | Tag::Image { dest_url, .. } => {
                self.styles
                    .push(Style::new().add_modifier(Modifier::UNDERLINED));
                self.link = Some(Link {
                    dest: dest_url.into_string(),
                    text: String::new(),
                });
            }
            Tag::Table(aligns) => {
                self.begin_block();
                self.table = Some(Table {
                    aligns,
                    rows: Vec::new(),
                    row: None,
                });
            }
            Tag::TableHead | Tag::TableRow => {
                let header = matches!(tag, Tag::TableHead);
                if let Some(table) = &mut self.table {
                    table.row = Some(TableRow {
                        header,
                        cells: Vec::new(),
                    });
                }
            }
            Tag::TableCell => self.inline.clear(),
            _ => {}
        }
    }

    fn end(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Paragraph | TagEnd::HtmlBlock => self.flush(),
            TagEnd::Heading(_) => {
                self.flush();
                self.styles.pop();
            }
            TagEnd::BlockQuote(_) => {
                self.flush();
                self.styles.pop();
                self.ctx.pop();
            }
            TagEnd::CodeBlock => self.end_code(),
            TagEnd::List(_) => {
                self.flush();
                self.lists.pop();
            }
            TagEnd::Item => {
                self.flush();
                if let Some(Ctx::Item {
                    marker: Some(_), ..
                }) = self.ctx.last()
                {
                    // An empty item still shows its marker.
                    self.emit(Vec::new());
                    if let Some(line) = self.out.last_mut() {
                        trim_spans(&mut line.spans);
                    }
                }
                self.ctx.pop();
            }
            TagEnd::Emphasis | TagEnd::Strong | TagEnd::Strikethrough => {
                self.styles.pop();
            }
            TagEnd::Link | TagEnd::Image => {
                self.styles.pop();
                if let Some(link) = self.link.take()
                    && !link.dest.is_empty()
                    && link.dest != link.text.trim()
                {
                    self.inline.push(Inline::Span(Span::styled(
                        format!(" ({})", link.dest),
                        dim(),
                    )));
                }
            }
            TagEnd::TableCell => {
                let cell = std::mem::take(&mut self.inline)
                    .into_iter()
                    .map(|part| match part {
                        Inline::Span(span) => span,
                        Inline::Break => Span::raw(" "),
                    })
                    .collect();
                if let Some(row) = self.table.as_mut().and_then(|table| table.row.as_mut()) {
                    row.cells.push(cell);
                }
            }
            TagEnd::TableHead | TagEnd::TableRow => {
                if let Some(table) = &mut self.table
                    && let Some(row) = table.row.take()
                {
                    table.rows.push(row);
                }
            }
            TagEnd::Table => {
                if let Some(table) = self.table.take() {
                    self.emit_table(&table);
                }
            }
            _ => {}
        }
    }

    fn end_code(&mut self) {
        let Some(code) = self.code.take() else {
            return;
        };
        let indent = if self.available() > 2 { "  " } else { "" };
        let room = self.available() - indent.len();
        for line in code.split_terminator('\n') {
            let body = [Span::styled(line.replace('\t', "    "), dim())];
            let row = truncate(&cells(&body), room);
            let mut line = vec![Span::raw(indent)];
            line.extend(spans(&row));
            self.emit(line);
        }
    }

    fn emit_table(&mut self, table: &Table) {
        let columns = table
            .rows
            .iter()
            .map(|row| row.cells.len())
            .max()
            .unwrap_or(0);
        if columns == 0 {
            return;
        }
        let available = self.available();
        let mut widths = vec![1; columns];
        for row in &table.rows {
            for (index, cell) in row.cells.iter().enumerate() {
                widths[index] = widths[index].max(total(&cells(cell)));
            }
        }
        // Padded separators read best; fall back to bare bars when even one
        // cell per column would not fit.
        let padded = columns + 3 * (columns - 1) <= available;
        let gap = if padded { 3 } else { 1 };
        let overhead = gap * (columns - 1);
        while widths.iter().sum::<usize>() + overhead > available {
            let Some(widest) = widths.iter().copied().max().filter(|w| *w > 1) else {
                break;
            };
            if let Some(slot) = widths.iter_mut().find(|w| **w == widest) {
                *slot -= 1;
            }
        }
        let bar = if padded { " │ " } else { "│" };
        for row in &table.rows {
            let mut line: Vec<Span<'static>> = Vec::new();
            for (index, width) in widths.iter().enumerate() {
                if index > 0 {
                    line.push(Span::styled(bar, dim()));
                }
                let empty = Vec::new();
                let content = row.cells.get(index).unwrap_or(&empty);
                let mut content = cells(content);
                if row.header {
                    for cell in &mut content {
                        cell.style = cell.style.add_modifier(Modifier::BOLD);
                    }
                }
                let content = truncate(&content, *width);
                let spare = width - total(&content);
                let (left, right) = match table.aligns.get(index) {
                    Some(Alignment::Right) => (spare, 0),
                    Some(Alignment::Center) => (spare / 2, spare - spare / 2),
                    _ => (0, spare),
                };
                line.push(Span::raw(" ".repeat(left)));
                line.extend(spans(&content));
                line.push(Span::raw(" ".repeat(right)));
            }
            let clipped = truncate(&cells(&line), available);
            self.emit(spans(&clipped));
            if row.header {
                let joint = if padded { "─┼─" } else { "┼" };
                let rule = widths
                    .iter()
                    .map(|width| "─".repeat(*width))
                    .collect::<Vec<_>>()
                    .join(joint);
                let rule = [Span::styled(rule, dim())];
                let clipped = truncate(&cells(&rule), available);
                self.emit(spans(&clipped));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(lines: &[Line<'_>]) -> Vec<String> {
        lines
            .iter()
            .map(|line| {
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect()
            })
            .collect()
    }

    /// Visible text; a copy-only joining space at a wrap is invisible.
    fn rows(source: &str, width: usize) -> Vec<String> {
        text(&render(source, width))
            .into_iter()
            .map(|row| row.trim_end().to_owned())
            .collect()
    }

    #[test]
    fn soft_wraps_copy_as_one_paragraph_without_the_gutter() {
        let rows = render_rows("> one two three\n\n- alpha beta gamma\n\nabcdefghijkl", 10);
        let copy: String = rows
            .iter()
            .map(|row| {
                let text: String = row
                    .line
                    .spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect();
                let mut cells = 0;
                let skip = text
                    .char_indices()
                    .find(|(_, ch)| {
                        let at = cells;
                        cells += Span::raw(ch.to_string()).width();
                        at >= usize::from(row.copy_from)
                    })
                    .map_or(text.len(), |(index, _)| index);
                let end = if row.joins_next { "" } else { "\n" };
                format!("{}{end}", &text[skip..])
            })
            .collect();
        // Quote bars and hanging indentation are not copied; list markers
        // are. Wrapped words keep their space; a word split mid-way joins.
        assert_eq!(
            copy,
            "one two three\n\n• alpha beta gamma\n\nabcdefghijkl\n"
        );
        assert!(rows.iter().all(|row| row.line.width() <= 10));
        // A row that exactly fills the width has no room for the joining
        // space, so it copies as a line break rather than gluing words.
        let full = render_rows("> one two three", 9);
        assert!(!full[0].joins_next && !full[1].joins_next);
    }

    fn style_of(lines: &[Line<'static>], needle: &str) -> Style {
        lines
            .iter()
            .flat_map(|line| &line.spans)
            .find(|span| span.content.contains(needle))
            .map(|span| span.style)
            .unwrap_or_default()
    }

    #[test]
    fn empty_input_renders_nothing() {
        assert!(render("", 40).is_empty());
        assert!(render("\n\n  \n", 40).is_empty());
        assert!(render("text", 0).is_empty());
    }

    #[test]
    fn heading_has_no_hash_and_is_bold() {
        let lines = render("# Title\n\n### Small", 40);
        assert_eq!(text(&lines), ["Title", "", "Small"]);
        let title = style_of(&lines, "Title");
        assert!(title.add_modifier.contains(Modifier::BOLD));
        assert_eq!(title.fg, Some(ACCENT));
        let small = style_of(&lines, "Small");
        assert!(small.add_modifier.contains(Modifier::BOLD));
        assert_eq!(small.fg, None);
    }

    #[test]
    fn inline_styles_drop_their_markers() {
        let lines = render("**bold** *ital* ~~gone~~ `code`", 40);
        assert_eq!(text(&lines), ["bold ital gone code"]);
        assert!(
            style_of(&lines, "bold")
                .add_modifier
                .contains(Modifier::BOLD)
        );
        assert!(
            style_of(&lines, "ital")
                .add_modifier
                .contains(Modifier::ITALIC)
        );
        assert!(
            style_of(&lines, "gone")
                .add_modifier
                .contains(Modifier::CROSSED_OUT)
        );
        assert_eq!(style_of(&lines, "code").fg, Some(CODE));
    }

    #[test]
    fn links_show_url_only_when_it_differs() {
        let lines = render("[docs](https://x.dev) and <https://y.dev>", 60);
        assert_eq!(text(&lines), ["docs (https://x.dev) and https://y.dev"]);
        assert!(
            style_of(&lines, "docs")
                .add_modifier
                .contains(Modifier::UNDERLINED)
        );
        assert!(
            style_of(&lines, "https://y.dev")
                .add_modifier
                .contains(Modifier::UNDERLINED)
        );
    }

    #[test]
    fn lists_use_bullets_numbers_and_tasks() {
        assert_eq!(
            rows("- a\n  - b\n    - c\n- d", 40),
            ["• a", "  ◦ b", "    ▪ c", "• d"]
        );
        assert_eq!(rows("3. x\n4. y", 40), ["3. x", "4. y"]);
        assert_eq!(rows("- [ ] todo\n- [x] done", 40), ["☐ todo", "☑ done"]);
    }

    #[test]
    fn wrapped_list_rows_hang_under_the_text() {
        assert_eq!(rows("- aaa bbb ccc ddd", 9), ["• aaa bbb", "  ccc ddd"]);
        assert_eq!(rows("10. one two three", 12), ["10. one two", "    three"]);
    }

    #[test]
    fn quotes_repeat_their_bar_on_every_row() {
        let lines = render("> one two three four\n>\n> second", 10);
        assert_eq!(
            text(&lines)
                .iter()
                .map(|row| row.trim_end())
                .collect::<Vec<_>>(),
            ["▌ one two", "▌ three", "▌ four", "▌", "▌ second"]
        );
        let style = style_of(&lines, "one");
        assert!(style.add_modifier.contains(Modifier::ITALIC));
        assert!(style.add_modifier.contains(Modifier::DIM));
    }

    #[test]
    fn rule_spans_the_width() {
        assert_eq!(rows("a\n\n---\n\nb", 6), ["a", "", "──────", "", "b"]);
    }

    #[test]
    fn paragraphs_are_separated_by_one_blank_row() {
        assert_eq!(rows("\n\na\n\n\n\nb\n\n", 40), ["a", "", "b"]);
        assert_eq!(rows("one\ntwo", 40), ["one", "two"]);
    }

    #[test]
    fn tables_align_into_a_grid() {
        let lines = render("| Name | Qty |\n|:--|--:|\n| apple | 3 |\n| fig | 12 |", 40);
        assert_eq!(
            text(&lines),
            ["Name  │ Qty", "──────┼────", "apple │   3", "fig   │  12"]
        );
        assert!(
            style_of(&lines, "Name")
                .add_modifier
                .contains(Modifier::BOLD)
        );
    }

    #[test]
    fn wide_tables_never_exceed_the_width() {
        let source = "| alpha | beta | gamma |\n|---|---|---|\n\
                      | a very long cell indeed | 界界界界 | z |\n| 1 | 2 | 3 |";
        for width in 1..=60 {
            for line in render(source, width) {
                assert!(line.width() <= width, "{width}: {line:?}");
            }
        }
        let narrow = rows(source, 24);
        assert!(narrow.iter().any(|row| row.contains('…')));
        assert!(narrow.iter().any(|row| row.contains('┼')));
    }

    #[test]
    fn every_prefix_renders_within_the_width_while_streaming() {
        let source = "# Head\n\n**bold *mix* `c` [l](http://u)\n\n> - [x] q\n>   wrapped words here\n\n\
                      1. one\n   - two ~~s~~\n\n| a | b |\n|---|---|\n| 界 | long long |\n\n```rs\nfn x() {}\n";
        for width in [1, 2, 3, 5, 8, 20, 40] {
            let mut end = 0;
            while end <= source.len() {
                if source.is_char_boundary(end) {
                    for line in render(&source[..end], width) {
                        assert!(line.width() <= width, "{width} {end}: {line:?}");
                    }
                }
                end += 1;
            }
        }
    }

    #[test]
    fn unclosed_markers_stay_literal() {
        assert_eq!(rows("**bold", 40), ["**bold"]);
        assert_eq!(rows("| a | b |", 40), ["| a | b |"]);
        assert_eq!(rows("`tick", 40), ["`tick"]);
    }

    #[test]
    fn stray_code_fences_become_dim_indented_text() {
        let lines = render("```rust\nlet x = 1;\n```", 40);
        assert_eq!(text(&lines), ["  let x = 1;"]);
        assert!(style_of(&lines, "let").add_modifier.contains(Modifier::DIM));
        assert_eq!(rows("```\nopen", 40), ["  open"]);
    }

    #[test]
    fn empty_items_and_deep_nesting_survive_tiny_widths() {
        assert_eq!(rows("-\n- x", 10), ["•", "• x"]);
        for line in render("> > > - - - deep text here", 3) {
            assert!(line.width() <= 3);
        }
    }

    #[test]
    fn cache_reuses_unchanged_renders() {
        let first = render_cached("# Cached", 30);
        assert!(Rc::ptr_eq(&first, &render_cached("# Cached", 30)));
        assert!(!Rc::ptr_eq(&first, &render_cached("# Cached", 31)));
    }
}
