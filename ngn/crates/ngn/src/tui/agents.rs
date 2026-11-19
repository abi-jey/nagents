//! The native catalog is shared with F5; the rail owns tree and context state.

use std::collections::{HashMap, HashSet};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ngn_core::{Config, Host};
use ngn_protocol::{ProviderAuth, RunStatus, TaskInfo};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{
        Block, BorderType, Padding, Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState,
        Wrap,
    },
};

use super::App;
use crate::view;

const BREAKPOINT: u16 = 110;
const WIDTH: u16 = 31;
const CONTEXT_PAGE: u16 = 10;
const TREE_MIN: u16 = 4;
// The terminal theme's panel role uses the terminal's default background.
const PANEL: Color = Color::Reset;

pub(super) fn is_wide(width: u16) -> bool {
    width > BREAKPOINT
}

pub(super) fn split(body: Rect) -> (Rect, Option<Rect>) {
    if !is_wide(body.width) {
        return (body, None);
    }
    let main = Rect::new(body.x, body.y, body.width - WIDTH, body.height);
    let rail = Rect::new(main.right(), body.y, WIDTH, body.height);
    (main, Some(rail))
}

#[derive(Clone, Copy)]
struct Node {
    task: usize,
    depth: usize,
    has_children: bool,
}

fn tree(tasks: &[TaskInfo]) -> Vec<Node> {
    let by_identity: HashMap<_, _> = tasks
        .iter()
        .enumerate()
        .map(|(index, task)| ((task.run_id.as_str(), task.session_id.as_str()), index))
        .collect();
    let mut children = vec![Vec::new(); tasks.len()];
    let mut roots = Vec::new();
    for (index, task) in tasks.iter().enumerate() {
        let parent = (task.parent_run_id.as_str(), task.parent_session_id.as_str());
        match by_identity.get(&parent) {
            Some(&parent) if parent != index => children[parent].push(index),
            _ => roots.push(index),
        }
    }
    let mut nodes = Vec::with_capacity(tasks.len());
    let mut visited = vec![false; tasks.len()];
    let mut stack = Vec::new();
    // Newest siblings first (catalog order). After ordinary roots, visit any
    // disconnected cycles once so malformed ancestry cannot hide a task.
    for root in roots.into_iter().chain(0..tasks.len()) {
        stack.push((root, 0));
        while let Some((task, depth)) = stack.pop() {
            if visited[task] {
                continue;
            }
            visited[task] = true;
            let children = children[task]
                .iter()
                .rev()
                .filter(|&&child| !visited[child]);
            let pending = stack.len();
            stack.extend(children.map(|&child| (child, depth + 1)));
            nodes.push(Node {
                task,
                depth,
                has_children: stack.len() != pending,
            });
        }
    }
    nodes
}

#[derive(Default)]
struct Context {
    focused: bool,
    expanded: bool,
    scroll: u16,
    viewport: Option<ContextViewport>,
}

#[derive(Clone, Copy)]
pub(super) struct ContextViewport {
    page: u16,
    max_scroll: u16,
}

impl Context {
    fn key(&mut self, code: KeyCode) {
        let Some(viewport) = self.viewport else {
            return;
        };
        if matches!(code, KeyCode::Enter | KeyCode::Char(' ')) {
            self.expanded = !self.expanded;
            self.scroll = 0;
            return;
        }
        // A title-only viewport cannot scroll. Keep the offset for when the
        // terminal is tall enough to show details again.
        if viewport.page == 0 {
            return;
        }
        match code {
            KeyCode::Up => self.scroll = self.scroll.saturating_sub(1),
            KeyCode::Down => self.scroll = self.scroll.saturating_add(1),
            KeyCode::PageUp => self.scroll = self.scroll.saturating_sub(viewport.page),
            KeyCode::PageDown => self.scroll = self.scroll.saturating_add(viewport.page),
            KeyCode::Home => self.scroll = 0,
            KeyCode::End => self.scroll = viewport.max_scroll,
            _ => (),
        }
        self.scroll = self.scroll.min(viewport.max_scroll);
    }

    fn height(&self, inner_height: u16, rows_after_count: u16) -> u16 {
        if !self.expanded {
            return 1;
        }
        // The title gets one row; the payload may not crowd out the task tree.
        1.max(
            ((u32::from(inner_height) * 3 / 5) as u16)
                .min(rows_after_count.saturating_sub(TREE_MIN)),
        )
        .min(1 + CONTEXT_PAGE)
    }

    fn draw(
        &self,
        frame: &mut Frame,
        area: Rect,
        active: bool,
        (config, session): (&Config, &str),
        muted: Color,
    ) -> ContextViewport {
        let info = Color::Rgb(129, 199, 190);
        let title_style = if active {
            Style::new().add_modifier(Modifier::REVERSED)
        } else {
            Style::new().fg(info)
        };
        let marker = if self.expanded { "▾" } else { "▸" };
        frame.render_widget(
            Line::styled(format!("{marker} Workspace & context"), title_style),
            Rect { height: 1, ..area },
        );
        let page = area.height.saturating_sub(1);
        if page == 0 {
            return ContextViewport {
                page,
                max_scroll: 0,
            };
        }
        let clean = |value: &str| view::plain(value).replace(['\n', '\t'], " ");
        // Match the Python terminal theme's heading and assistant roles.
        let heading = |title| {
            Line::styled(
                title,
                Style::new().fg(Color::Blue).add_modifier(Modifier::BOLD),
            )
        };
        // Allowlist display fields. Never format the provider or its errors,
        // nor consult environment variables, auth stores, or runtime auth.
        let (provider, model, auth) = match config.providers.get(&config.provider) {
            Some(provider) => (
                format!("Provider: {}", clean(&provider.kind)),
                format!("Model: {}", clean(&provider.model)),
                match provider.auth {
                    ProviderAuth::ApiKey => "API key mode",
                    ProviderAuth::Chatgpt => "ChatGPT login mode",
                    ProviderAuth::Codex => "Codex login mode",
                },
            ),
            None if config.provider.is_empty() => (
                "No provider selected".into(),
                "Model: unavailable".into(),
                "Not configured",
            ),
            None => (
                "Provider unavailable".into(),
                "Model: unavailable".into(),
                "Mode unavailable",
            ),
        };
        let paragraph = Paragraph::new(vec![
            heading("WORKSPACE"),
            Line::raw(clean(&config.workspace.to_string_lossy())),
            Line::raw(if config.read_only {
                "Read-only mode: on"
            } else {
                "Read-only mode: off"
            }),
            Line::default(),
            heading("SESSION"),
            Line::styled(
                if session.is_empty() {
                    "New session".into()
                } else {
                    clean(session)
                },
                Style::new().fg(Color::Cyan),
            ),
            Line::default(),
            heading("CONFIGURATION"),
            Line::raw(provider),
            Line::raw(model),
            Line::styled(
                "Profiles, plugins and project trust: not shown in native TUI",
                Style::new().fg(muted),
            ),
            Line::default(),
            heading("AUTHENTICATION"),
            Line::styled(auth, Style::new().fg(info)),
            Line::styled("Credential status not checked", Style::new().fg(muted)),
        ])
        .wrap(Wrap { trim: false });
        let details = Rect::new(area.x, area.y + 1, area.width.saturating_sub(1), page);
        let line_count = paragraph
            .line_count(details.width)
            .min(usize::from(u16::MAX)) as u16;
        let max_scroll = line_count.saturating_sub(page);
        let scroll = self.scroll.min(max_scroll);
        frame.render_widget(paragraph.scroll((scroll, 0)), details);
        if max_scroll > 0 {
            let mut state = ScrollbarState::new(usize::from(max_scroll) + 1)
                .position(usize::from(scroll))
                .viewport_content_length(usize::from(page));
            frame.render_stateful_widget(
                Scrollbar::new(ScrollbarOrientation::VerticalRight)
                    .begin_symbol(None)
                    .end_symbol(None)
                    .style(Style::new().fg(muted)),
                Rect {
                    width: area.width,
                    ..details
                },
                &mut state,
            );
        }
        ContextViewport { page, max_scroll }
    }
}

#[derive(Default)]
pub(super) struct Rail {
    pub(super) focused: bool,
    current: bool,
    nodes: Vec<Node>,
    selected: Option<String>,
    collapsed: HashSet<String>,
    viewport: usize,
    offset: usize,
    context: Context,
}

fn window_start(offset: usize, cursor: usize, length: usize, height: usize) -> usize {
    offset
        .min(length.saturating_sub(height))
        .clamp(cursor.saturating_sub(height.saturating_sub(1)), cursor)
}

impl Rail {
    pub(super) fn catalog_current(&self) -> bool {
        self.current
    }

    pub(super) fn invalidate(&mut self) {
        self.current = false;
    }

    pub(super) fn refresh(&mut self, tasks: &[TaskInfo]) {
        self.current = true;
        self.nodes = tree(tasks);
        let ids: HashSet<_> = tasks.iter().map(|task| task.run_id.as_str()).collect();
        self.collapsed.retain(|run| ids.contains(run.as_str()));
        if self.selected.as_deref().is_some_and(|id| !ids.contains(id)) {
            self.selected = None;
        }
        if self.focused && self.selected.is_none() {
            self.key(KeyCode::Home, tasks);
        }
    }

    pub(super) fn resize(&mut self, width: u16, height: u16) {
        let inner_height = height.saturating_sub(2);
        self.viewport = usize::from(
            inner_height
                .saturating_sub(1)
                .saturating_sub(self.context_height(inner_height)),
        );
        if !is_wide(width) {
            self.blur();
        }
    }

    fn context_height(&self, inner_height: u16) -> u16 {
        let rows = inner_height.saturating_sub(1);
        // Reserve the compact sidebar's task rows before adding the disclosure.
        if rows <= self.nodes.len().min(usize::from(TREE_MIN)) as u16 {
            0
        } else {
            self.context.height(inner_height, rows)
        }
    }

    fn blur(&mut self) {
        self.focused = false;
        self.context.focused = false;
    }

    pub(super) fn observe_context(&mut self, viewport: Option<ContextViewport>) {
        self.context.viewport = viewport;
        if let Some(viewport) = viewport {
            // Preserve the position if the payload is temporarily hidden in a
            // short terminal; otherwise clamp against the latest layout.
            if viewport.page > 0 {
                self.context.scroll = self.context.scroll.min(viewport.max_scroll);
            }
        } else {
            self.context.focused = false;
        }
    }

    fn visible(&self, tasks: &[TaskInfo]) -> Vec<Node> {
        let mut folded_depth = None;
        self.nodes
            .iter()
            .copied()
            .filter(|node| {
                if folded_depth.is_some_and(|depth| node.depth > depth) {
                    return false;
                }
                let collapsed =
                    node.has_children && self.collapsed.contains(&tasks[node.task].run_id);
                folded_depth = collapsed.then_some(node.depth);
                true
            })
            .collect()
    }

    fn cursor(&self, visible: &[Node], tasks: &[TaskInfo]) -> usize {
        self.selected
            .as_ref()
            .and_then(|id| {
                visible
                    .iter()
                    .position(|node| &tasks[node.task].run_id == id)
            })
            .unwrap_or(0)
    }

    fn key(&mut self, code: KeyCode, tasks: &[TaskInfo]) -> bool {
        let visible = self.visible(tasks);
        let cursor = self.cursor(&visible, tasks);
        let last = visible.len().saturating_sub(1);
        let page = self.viewport.max(1);
        let start = window_start(self.offset, cursor, visible.len(), page);
        let next = match code {
            KeyCode::Up => cursor.saturating_sub(1),
            KeyCode::Down => (cursor + 1).min(last),
            KeyCode::PageUp => cursor.saturating_sub(page),
            KeyCode::PageDown => (cursor + page).min(last),
            KeyCode::Home => 0,
            KeyCode::End => last,
            KeyCode::Left | KeyCode::Right | KeyCode::Char(' ') => {
                if let Some(node) = visible.get(cursor).filter(|node| node.has_children) {
                    let id = &tasks[node.task].run_id;
                    if code == KeyCode::Left
                        || (code == KeyCode::Char(' ') && !self.collapsed.contains(id))
                    {
                        self.collapsed.insert(id.clone());
                    } else {
                        self.collapsed.remove(id);
                    }
                }
                cursor
            }
            KeyCode::Enter => cursor,
            _ => return false,
        };
        self.selected = visible
            .get(next)
            .map(|node| tasks[node.task].run_id.clone());
        let offset = match code {
            KeyCode::PageUp => start.saturating_sub(page),
            KeyCode::PageDown => start + page,
            _ => start,
        };
        self.offset = window_start(offset, next, visible.len(), page);
        code == KeyCode::Enter && self.selected.is_some()
    }

    fn draw(
        &self,
        frame: &mut Frame,
        area: Rect,
        tasks: &[TaskInfo],
        (focused, approval_run): (bool, Option<&str>),
        (config, session): (&Config, &str),
        (accent, muted): (Color, Color),
    ) -> Option<ContextViewport> {
        let heading = Style::new().fg(accent).add_modifier(Modifier::BOLD);
        let block = Block::bordered()
            .border_type(BorderType::Rounded)
            .title(Line::styled(" Agents / Ctrl+T ", heading))
            .padding(Padding::horizontal(1))
            .style(Style::new().bg(PANEL))
            .border_style(Style::new().fg(if focused { accent } else { muted }));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        if inner.is_empty() {
            return None;
        }
        frame.render_widget(
            Line::styled(format!("Agents / {}", tasks.len()), heading),
            Rect { height: 1, ..inner },
        );
        let context_height = self.context_height(inner.height);
        let tree_height = inner.height - 1 - context_height;
        let rows = Rect::new(inner.x, inner.y + 1, inner.width, tree_height);
        let visible = self.visible(tasks);
        let cursor = self.cursor(&visible, tasks);
        let height = usize::from(rows.height);
        let start = window_start(self.offset, cursor, visible.len(), height);
        let overflow = visible.len().saturating_sub(height);
        if overflow > 0 && height > 0 {
            frame.render_stateful_widget(
                Scrollbar::new(ScrollbarOrientation::VerticalRight)
                    .begin_symbol(None)
                    .end_symbol(None)
                    .thumb_symbol("█")
                    .thumb_style(Style::new().fg(if focused { accent } else { muted }))
                    .track_symbol(Some("│"))
                    .track_style(Style::new().fg(muted)),
                rows,
                &mut ScrollbarState::new(overflow + 1)
                    .position(start)
                    .viewport_content_length(height),
            );
        }
        let width = rows.width.saturating_sub(u16::from(overflow > 0));
        for (row, node) in visible.iter().skip(start).take(height).enumerate() {
            let task = &tasks[node.task];
            let selected =
                focused && (!self.context.focused || context_height == 0) && start + row == cursor;
            let style = if selected {
                Style::new().add_modifier(Modifier::REVERSED)
            } else {
                Style::new()
            };
            let (status, color) = match task.status {
                RunStatus::Queued => ("Queued", muted),
                RunStatus::Running if approval_run == Some(task.run_id.as_str()) => {
                    ("Approval", Color::Yellow)
                }
                RunStatus::Running => ("Running", accent),
                RunStatus::Completed => ("Done", Color::Green),
                RunStatus::Failed => ("Error", Color::Red),
                RunStatus::Cancelled => ("Cancelled", muted),
                RunStatus::Interrupted => ("Interrupted", Color::Yellow),
            };
            let marker = if node.has_children {
                if self.collapsed.contains(&task.run_id) {
                    "▸"
                } else {
                    "▾"
                }
            } else {
                "─"
            };
            let indent = match node.depth {
                0 => "",
                1 => "  ",
                2 => "   ",
                _ => "  …",
            };
            let title = view::plain(&task.title).replace(['\n', '\t'], " ");
            let title = if title.trim().is_empty() {
                &task.run_id
            } else {
                &title
            };
            let line = Line::from(vec![
                Span::raw(format!("{indent}{marker} ")),
                Span::styled(status, Style::new().fg(color)),
                Span::raw(" "),
                Span::styled(
                    title,
                    if task.status == RunStatus::Running {
                        heading
                    } else {
                        Style::new()
                    },
                ),
            ])
            .style(style);
            let area = Rect::new(rows.x, rows.y + row as u16, width, 1);
            if task.read_only {
                const LABEL: &str = " read-only";
                let left = area.width.saturating_sub(LABEL.len() as u16);
                frame.render_widget(
                    line,
                    Rect {
                        width: left,
                        ..area
                    },
                );
                frame.render_widget(
                    Line::styled(LABEL, style.fg(muted)),
                    Rect::new(area.x + left, area.y, area.width - left, 1),
                );
            } else {
                frame.render_widget(line, area);
            }
        }
        (context_height > 0).then(|| {
            self.context.draw(
                frame,
                Rect::new(inner.x, rows.bottom(), inner.width, context_height),
                focused && self.context.focused,
                (config, session),
                muted,
            )
        })
    }
}

impl App {
    pub(super) fn draw_agents(
        &self,
        frame: &mut Frame,
        area: Rect,
        config: &Config,
        colors: (Color, Color),
    ) -> Option<ContextViewport> {
        self.agents.draw(
            frame,
            area,
            &self.tasks,
            (
                self.rail_has_focus(),
                self.approval
                    .as_ref()
                    .map(|approval| approval.run_id.as_str()),
            ),
            (config, &self.session),
            colors,
        )
    }

    pub(super) fn rail_has_focus(&self) -> bool {
        self.agents.focused && self.approval.is_none() && !self.shows_overlay()
    }

    pub(super) fn agents_key(&mut self, key: KeyEvent, width: u16) -> bool {
        if self.approval.is_some() || self.shows_overlay() {
            return false;
        }
        if key.code == KeyCode::Char('t') && key.modifiers == KeyModifiers::CONTROL {
            if !is_wide(width) {
                self.open_tasks(false);
            } else if self.agents.focused {
                self.agents.blur();
            } else {
                self.agents.focused = true;
                if self.agents.selected.is_none() {
                    self.agents.key(KeyCode::Home, &self.tasks);
                }
            }
            return true;
        }
        if !is_wide(width) || !self.rail_has_focus() || matches!(key.code, KeyCode::F(_)) {
            return false;
        }
        if self.agents.context.viewport.is_some()
            && matches!(key.code, KeyCode::Tab | KeyCode::BackTab)
            && (key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT)
        {
            self.agents.context.focused = !self.agents.context.focused;
        } else if key.modifiers.is_empty() {
            if key.code == KeyCode::Esc {
                self.agents.blur();
            } else if self.agents.context.focused {
                self.agents.context.key(key.code);
            } else if self.agents.key(key.code, &self.tasks) {
                self.open_tasks(true);
            }
        }
        // Never let rail typing, Enter, or navigation reach the composer.
        true
    }

    pub(super) fn reset_task_detail(&mut self, detail: bool) {
        self.task_detail = detail;
        self.task_scroll = 0;
        self.task_records.clear();
        self.task_after = 0;
    }

    pub(super) fn open_tasks(&mut self, detail: bool) {
        let selected = self
            .agents
            .selected
            .as_ref()
            .and_then(|id| self.tasks.iter().position(|task| &task.run_id == id));
        self.task_picker = true;
        self.task_index = selected.unwrap_or(0);
        self.task_from_rail = detail && selected.is_some();
        self.reset_task_detail(self.task_from_rail);
    }

    pub(super) async fn task_key(&mut self, host: &Host, key: KeyEvent) {
        match key.code {
            KeyCode::Esc if self.task_from_rail => {
                self.task_picker = false;
                self.task_from_rail = false;
                self.agents.blur();
                self.reset_task_detail(false);
            }
            KeyCode::Esc if self.task_detail => self.reset_task_detail(false),
            KeyCode::Esc => self.task_picker = false,
            KeyCode::Char('x') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                if let Some(task) = self.tasks.get(self.task_index)
                    && let Err(error) = host.cancel(&task.run_id).await
                {
                    self.status = error.to_string();
                }
            }
            KeyCode::Up if !self.task_detail => self.task_index = self.task_index.saturating_sub(1),
            KeyCode::Down if !self.task_detail => {
                self.task_index = (self.task_index + 1).min(self.tasks.len().saturating_sub(1));
            }
            KeyCode::Enter if !self.tasks.is_empty() => self.reset_task_detail(true),
            KeyCode::PageUp if self.task_detail => {
                self.task_scroll = self.task_scroll.saturating_add(10)
            }
            KeyCode::PageDown if self.task_detail => {
                self.task_scroll = self.task_scroll.saturating_sub(10)
            }
            _ => (),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::Instant;

    use ratatui::{Terminal, backend::TestBackend, buffer::Buffer};

    use super::*;

    fn task(id: &str, parent: &str, status: RunStatus) -> TaskInfo {
        TaskInfo {
            run_id: id.into(),
            session_id: format!("session-{id}"),
            parent_run_id: parent.into(),
            parent_session_id: format!("session-{parent}"),
            root_run_id: "root".into(),
            root_session_id: "session-root".into(),
            call_id: format!("call-{id}"),
            title: id.into(),
            depth: 99, // The upstream depth alone cannot establish ancestry.
            read_only: false,
            status,
        }
    }

    fn row(buffer: &Buffer, y: u16, columns: std::ops::Range<u16>) -> String {
        columns.map(|x| buffer[(x, y)].symbol()).collect()
    }

    fn paint_rail(app: &mut App, config: &Config, height: u16) -> Buffer {
        let mut terminal = Terminal::new(TestBackend::new(WIDTH + 2, height)).unwrap();
        terminal
            .draw(|frame| {
                let viewport = app.draw_agents(
                    frame,
                    Rect::new(1, 0, WIDTH, height),
                    config,
                    (Color::LightGreen, Color::Gray),
                );
                app.agents.observe_context(viewport);
            })
            .unwrap();
        terminal.backend().buffer().clone()
    }

    #[test]
    fn breakpoint_leaves_composer_full_width_and_renders_empty_rail() {
        let dir = crate::test_support::private_tempdir().unwrap();
        let host = Host::open(
            Config {
                workspace: dir.path().into(),
                data_dir: dir.path().join("state"),
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap();
        let mut app = App {
            input: "draft".into(),
            ..App::default()
        };
        for width in [110, 111] {
            let mut terminal = Terminal::new(TestBackend::new(width, 20)).unwrap();
            terminal
                .draw(|frame| super::super::draw(frame, &mut app, &host, Instant::now()))
                .unwrap();
            let buffer = terminal.backend().buffer();
            assert_eq!(buffer[(width - 1, 13)].symbol(), "┐"); // Composer top-right.
            assert!(row(buffer, 14, 0..width).contains("draft"));
            if width == 111 {
                assert_eq!(buffer[(80, 0)].symbol(), "╭");
                assert_eq!(buffer[(110, 0)].symbol(), "╮");
                assert_eq!(buffer[(110, 12)].symbol(), "╯");
                assert!(row(buffer, 0, 80..111).contains("Agents / Ctrl+T"));
                assert!(row(buffer, 1, 82..109).starts_with("Agents / 0"));
                assert!(row(buffer, 11, 80..111).contains("▸ Workspace & context"));
                assert!(!(1..11).any(|y| row(buffer, y, 80..111).contains("WORKSPACE")));
            } else {
                assert!(!(0..13).any(|y| row(buffer, y, 0..110).contains("Agents /")));
                assert!(!(0..13).any(|y| row(buffer, y, 0..110).contains("Workspace & context")));
            }
        }
    }

    #[test]
    fn short_wide_layout_is_monotone_with_or_without_completion() {
        let dir = crate::test_support::private_tempdir().unwrap();
        let config = Config {
            workspace: dir.path().into(),
            data_dir: dir.path().join("state"),
            ..Config::default()
        }
        .resolve()
        .unwrap();
        let host = Host::open(config).unwrap();
        let mut app = App {
            tasks: (0..8)
                .map(|index| task(&format!("Task-{index}"), "root", RunStatus::Running))
                .collect(),
            ..App::default()
        };
        app.agents.refresh(&app.tasks);
        let now = Instant::now();
        app.caret.focus(true, now);
        for draft in ["plain draft", "/se"] {
            app.input = draft.into();
            app.completion.edited(&app.input);
            for (height, expected) in [(11, 3), (13, 4), (14, 4), (15, 4)] {
                let mut terminal = Terminal::new(TestBackend::new(111, height)).unwrap();
                terminal
                    .draw(|frame| super::super::draw(frame, &mut app, &host, now))
                    .unwrap();
                let buffer = terminal.backend().buffer();
                let rail_rows: Vec<_> = (0..height).map(|y| row(buffer, y, 82..109)).collect();
                assert!(rail_rows.iter().any(|line| line.starts_with("Agents / 8")));
                assert_eq!(
                    rail_rows
                        .iter()
                        .filter(|line| line.contains("Task-"))
                        .count(),
                    expected,
                    "111×{height} with {draft:?}"
                );
                assert!((0..height).any(|y| buffer[(108, y)].symbol() == "█"));
                let composer = (0..height)
                    .find(|&y| buffer[(110, y)].symbol() == "┐")
                    .expect("full-width composer");
                assert!(row(buffer, composer + 1, 1..80).starts_with(draft));
                assert!(
                    buffer[(1 + draft.len() as u16, composer + 1)]
                        .modifier
                        .contains(Modifier::REVERSED)
                );
                assert_eq!(
                    (0..composer).any(|y| row(buffer, y, 0..80).contains("› /sessions")),
                    draft == "/se"
                );
                assert!(row(buffer, height - 1, 0..111).contains("Provider not configured"));
            }
        }
    }

    #[test]
    fn explicit_parent_identity_handles_missing_mismatched_and_cyclic_ancestry() {
        let mut mismatched = task("mismatch", "parent", RunStatus::Cancelled);
        mismatched.parent_session_id = "different-session".into();
        let mut wrong_run = task("wrong-run", "missing-run", RunStatus::Completed);
        wrong_run.parent_session_id = "session-parent".into();
        let tasks = vec![
            task("child", "parent", RunStatus::Failed),
            task("parent", "root", RunStatus::Running),
            mismatched,
            wrong_run,
            task("cycle-a", "cycle-b", RunStatus::Interrupted),
            task("cycle-b", "cycle-a", RunStatus::Running),
        ];
        let nodes = tree(&tasks);
        let projection: Vec<_> = nodes
            .iter()
            .map(|node| (tasks[node.task].run_id.as_str(), node.depth))
            .collect();
        assert_eq!(
            projection,
            [
                ("parent", 0),
                ("child", 1),
                ("mismatch", 0),
                ("wrong-run", 0),
                ("cycle-a", 0),
                ("cycle-b", 1),
            ]
        );
    }

    #[test]
    fn status_labels_fit_with_unsafe_wide_titles_and_viewport_is_bounded() {
        let mut tasks = vec![
            task("error", "parent", RunStatus::Failed),
            task("parent", "root", RunStatus::Running),
            task("complete", "root", RunStatus::Completed),
            task("cancel", "root", RunStatus::Cancelled),
            task("interrupt", "root", RunStatus::Interrupted),
            task("queued", "root", RunStatus::Queued),
        ];
        tasks[0].title = format!("見\x1b\n{}", "界".repeat(30));
        tasks[0].read_only = true;
        let mut rail = Rail::default();
        rail.refresh(&tasks);
        rail.resize(111, 8);
        let mut terminal = Terminal::new(TestBackend::new(32, 8)).unwrap();
        let paint = |terminal: &mut Terminal<TestBackend>, rail: &Rail, approval_run| {
            terminal
                .draw(|frame| {
                    rail.draw(
                        frame,
                        Rect::new(1, 0, WIDTH, 8),
                        &tasks,
                        (true, approval_run),
                        (&Config::default(), ""),
                        (Color::LightGreen, Color::Gray),
                    );
                })
                .unwrap();
        };
        paint(&mut terminal, &rail, None);
        let buffer = terminal.backend().buffer();
        assert!(row(buffer, 0, 1..32).contains("Agents / Ctrl+T"));
        assert!(buffer[(3, 0)].modifier.contains(Modifier::BOLD));
        assert!(row(buffer, 1, 3..30).starts_with("Agents / 6"));
        assert!(row(buffer, 2, 3..29).starts_with("▾ Running parent"));
        assert!(buffer[(13, 2)].modifier.contains(Modifier::BOLD));
        let child = row(buffer, 3, 3..29);
        assert!(child.starts_with("  ─ Error 見"));
        assert!(!child.contains('\x1b'));
        assert!(child.ends_with(" read-only"));
        assert_eq!(buffer[(7, 3)].fg, Color::Red);
        assert_eq!(row(buffer, 3, 1..3), "│ ");
        assert_eq!(row(buffer, 3, 30..32), " │");
        assert_eq!(buffer[(29, 2)].symbol(), "█");
        assert_eq!(buffer[(29, 5)].symbol(), "│");
        assert!(row(buffer, 4, 3..29).starts_with("─ Done complete"));
        assert!(row(buffer, 5, 3..29).starts_with("─ Cancelled cancel"));
        paint(&mut terminal, &rail, Some("parent"));
        assert!(row(terminal.backend().buffer(), 2, 3..29).starts_with("▾ Approval parent"));
        rail.key(KeyCode::Down, &tasks);
        rail.key(KeyCode::Down, &tasks);
        rail.key(KeyCode::Down, &tasks);
        assert_eq!(rail.offset, 0); // Selection can move within the viewport.
        rail.key(KeyCode::Home, &tasks);
        rail.key(KeyCode::PageDown, &tasks);
        assert_eq!(rail.selected.as_deref(), Some("interrupt"));
        assert_eq!(rail.offset, 2);
        paint(&mut terminal, &rail, None);
        let buffer = terminal.backend().buffer();
        assert!(row(buffer, 4, 3..29).starts_with("─ Interrupted interrupt"));
        assert!(row(buffer, 5, 3..29).starts_with("─ Queued queued"));
        assert_eq!(buffer[(29, 2)].symbol(), "│");
        assert_eq!(buffer[(29, 5)].symbol(), "█");
        assert!(buffer[(5, 4)].modifier.contains(Modifier::REVERSED));
    }

    #[test]
    fn context_has_bounded_metadata_and_reads_only_safe_display_fields() {
        use ngn_protocol::ProviderConfig;

        let mut config = Config {
            workspace: "/workspace/very-long-directory-name/日本語\n\x1bproject".into(),
            data_dir: "/private/data".into(),
            source: Some("/private/config".into()),
            provider: "active".into(),
            read_only: true,
            providers: [(
                "active".into(),
                ProviderConfig {
                    kind: "openai".into(),
                    model: "configured-model".into(),
                    api: "responses".into(),
                    base_url: "https://user:secret@example.invalid".into(),
                    api_key_env: "PRIVATE_KEY_VAR".into(),
                    auth: ProviderAuth::ApiKey,
                },
            )]
            .into(),
            ..Config::default()
        };
        let mut app = App {
            session: "session-example".into(),
            tasks: vec![task("child", "root", RunStatus::Running)],
            ..App::default()
        };
        app.agents.refresh(&app.tasks);
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let focus = KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL);
        assert!(app.agents_key(focus, 111));
        let buffer = paint_rail(&mut app, &config, 30);
        assert!(buffer[(3, 2)].modifier.contains(Modifier::REVERSED));
        assert!(row(&buffer, 28, 2..31).contains("▸ Workspace & context"));
        assert_eq!(buffer[(3, 28)].fg, Color::Rgb(129, 199, 190));
        assert!(app.agents_key(key(KeyCode::Tab), 111));
        assert!(app.agents_key(key(KeyCode::Enter), 111));
        let buffer = paint_rail(&mut app, &config, 30);
        let viewport = app.agents.context.viewport.unwrap();
        assert_eq!(viewport.page, 10);
        assert!(viewport.max_scroll > 0);
        assert!(buffer[(3, 18)].modifier.contains(Modifier::REVERSED));
        assert!(!buffer[(3, 2)].modifier.contains(Modifier::REVERSED));
        assert!(row(&buffer, 18, 2..31).contains("▾ Workspace & context"));
        let find_row = |buffer: &Buffer, text: &str| {
            (19..29)
                .find(|&y| row(buffer, y, 3..29).trim_end() == text)
                .unwrap_or_else(|| panic!("missing context row: {text}"))
        };
        let check_heading = |buffer: &Buffer, text: &str| {
            let cell = &buffer[(3, find_row(buffer, text))];
            assert_eq!(cell.fg, Color::Blue, "{text}");
            assert!(cell.modifier.contains(Modifier::BOLD), "{text}");
        };
        for text in ["WORKSPACE", "SESSION", "CONFIGURATION"] {
            check_heading(&buffer, text);
        }
        assert_eq!(
            buffer[(3, find_row(&buffer, "session-example"))].fg,
            Color::Cyan
        );
        let panel = |buffer: &Buffer| {
            (19..29)
                .map(|y| row(buffer, y, 3..29))
                .collect::<Vec<_>>()
                .join("\n")
        };
        let first = panel(&buffer);
        assert!(first.replace(' ', "").contains("日本語project"), "{first}");
        assert!(first.contains("Read-only mode: on"));
        assert!(!first.contains('\x1b'));
        assert!(app.agents_key(key(KeyCode::PageDown), 111));
        assert_eq!(app.agents.context.scroll, viewport.max_scroll.min(10));
        let buffer = paint_rail(&mut app, &config, 30);
        let last = panel(&buffer);
        assert!(last.contains("Provider: openai") && last.contains("Model: configured-model"));
        check_heading(&buffer, "AUTHENTICATION");
        assert_eq!(
            buffer[(3, find_row(&buffer, "API key mode"))].fg,
            Color::Rgb(129, 199, 190)
        );
        assert!(last.contains("Credential status not"));
        for forbidden in ["secret", "PRIVATE_KEY_VAR", "/private", "openai.json"] {
            assert!(!first.contains(forbidden) && !last.contains(forbidden));
        }
        for y in 1..29 {
            assert_eq!(buffer[(31, y)].symbol(), "│");
            assert_eq!(buffer[(32, y)].symbol(), " ");
        }
        assert_eq!(buffer[(31, 29)].symbol(), "╯");

        config.providers.get_mut("active").unwrap().auth = ProviderAuth::Chatgpt;
        let buffer = paint_rail(&mut app, &config, 30);
        assert!(panel(&buffer).contains("ChatGPT login mode"));
        for height in [0, 1, 2, 3, 5, 8, 10, 16, 29, 40] {
            let buffer = paint_rail(&mut app, &config, height);
            let inner = height.saturating_sub(2);
            if let Some(viewport) = app.agents.context.viewport {
                assert!(viewport.page <= 10);
                assert!(viewport.page <= inner.saturating_sub(2 + TREE_MIN));
                assert!(viewport.page <= (inner * 3 / 5).saturating_sub(1));
                assert_eq!(buffer[(31, height - 1)].symbol(), "╯");
            } else {
                assert!(inner <= 2);
            }
        }

        // A missing/invalid provider must not expose config diagnostics.
        config.providers.clear();
        assert!(app.agents_key(key(KeyCode::Tab), 111));
        assert!(app.agents_key(key(KeyCode::End), 111));
        let last = panel(&paint_rail(&mut app, &config, 30));
        assert!(last.contains("Provider unavailable"), "{last}");
        assert!(last.contains("Mode unavailable") && !last.contains("/private"));
    }

    #[test]
    fn title_only_resize_preserves_scroll_and_focus_but_allows_toggle() {
        let dir = crate::test_support::private_tempdir().unwrap();
        let host = Host::open(
            Config {
                workspace: dir.path().into(),
                data_dir: dir.path().join("state"),
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap();
        let mut app = App::default();
        let draw = |app: &mut App, height| {
            let mut terminal = Terminal::new(TestBackend::new(111, height)).unwrap();
            terminal
                .draw(|frame| super::super::draw(frame, app, &host, Instant::now()))
                .unwrap();
            terminal.backend().buffer().clone()
        };
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let rail_has = |buffer: &Buffer, text: &str| {
            (0..buffer.area.height).any(|y| row(buffer, y, 80..111).contains(text))
        };
        draw(&mut app, 40);
        assert!(app.agents_key(
            KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL),
            111
        ));
        assert!(app.agents_key(key(KeyCode::Tab), 111));
        assert!(app.agents_key(key(KeyCode::Enter), 111));
        draw(&mut app, 40);
        assert_eq!(app.agents.context.viewport.unwrap().page, 10);
        assert!(app.agents_key(key(KeyCode::End), 111));
        let full = draw(&mut app, 40);
        assert!(rail_has(&full, "AUTHENTICATION"));
        let saved = app.agents.context.scroll;
        assert!(saved > 0);

        let short = draw(&mut app, 15);
        assert_eq!(app.agents.context.viewport.unwrap().page, 0);
        assert!(rail_has(&short, "▾ Workspace & context"));
        assert!(!rail_has(&short, "AUTHENTICATION"));
        for code in [
            KeyCode::Down,
            KeyCode::Up,
            KeyCode::PageUp,
            KeyCode::PageDown,
            KeyCode::Home,
            KeyCode::End,
        ] {
            assert!(app.agents_key(key(code), 111));
            assert_eq!(app.agents.context.scroll, saved);
        }
        let restored = draw(&mut app, 40);
        for y in 0..33 {
            assert_eq!(row(&full, y, 80..111), row(&restored, y, 80..111));
        }
        assert!(app.rail_has_focus() && app.agents.context.focused);

        draw(&mut app, 15);
        assert!(app.agents_key(key(KeyCode::Char(' ')), 111));
        assert!(rail_has(&draw(&mut app, 15), "▸ Workspace & context"));
        assert!(app.agents_key(key(KeyCode::Enter), 111));
        assert_eq!(app.agents.context.scroll, 0);
        assert!(rail_has(&draw(&mut app, 40), "WORKSPACE"));
        assert!(app.rail_has_focus() && app.agents.context.focused);
    }

    #[test]
    fn disclosure_focus_preserves_selection_draft_active_run_and_modal_priority() {
        let config = Config::default();
        let mut app = App {
            session: "current-session".into(),
            input: "/se".into(),
            scroll: Some(7),
            active: Some(ngn_core::store::Run {
                id: "active-root".into(),
                session_id: "current-session".into(),
                request_id: "request".into(),
                status: RunStatus::Running,
            }),
            tasks: vec![
                task("first", "active-root", RunStatus::Running),
                task("selected", "active-root", RunStatus::Running),
            ],
            ..App::default()
        };
        app.completion.edited(&app.input);
        let tasks = app.tasks.clone();
        app.agents.refresh(&app.tasks);
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let focus = KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL);
        paint_rail(&mut app, &config, 16);
        assert!(app.agents_key(focus, 111));
        assert!(app.agents_key(key(KeyCode::Down), 111));
        assert_eq!(app.agents.selected.as_deref(), Some("selected"));
        assert!(app.agents_key(key(KeyCode::Tab), 111));
        assert!(app.agents.context.focused);
        assert!(app.agents_key(key(KeyCode::Char(' ')), 111));
        paint_rail(&mut app, &config, 16);
        assert!(app.agents.context.expanded);
        for code in [KeyCode::End, KeyCode::Down, KeyCode::Char('x')] {
            assert!(app.agents_key(key(code), 111));
        }
        assert_eq!(
            app.agents.context.scroll,
            app.agents.context.viewport.unwrap().max_scroll
        );
        assert!(!app.agents_key(key(KeyCode::F(5)), 111));
        assert!(!app.agents_key(key(KeyCode::F(6)), 111));
        app.open_tasks(false);
        assert_eq!(app.tasks[app.task_index].run_id, "selected");
        assert!(!app.agents_key(key(KeyCode::Enter), 111));
        app.task_picker = false;
        app.approval = Some(ngn_core::host::Approval {
            id: "approval".into(),
            run_id: "active-root".into(),
            session_id: app.session.clone(),
            root_session_id: app.session.clone(),
            agent_title: "root".into(),
            depth: 0,
            call: ngn_protocol::ToolCall {
                call_id: "call".into(),
                name: "write".into(),
                arguments: serde_json::Value::Null,
            },
            preview: String::new(),
        });
        assert!(!app.agents_key(key(KeyCode::Enter), 111));
        assert!(!app.agents_key(key(KeyCode::Esc), 111));
        assert!(!app.agents_key(focus, 111));
        assert!(app.agents.context.expanded);
        app.approval = None;
        assert!(app.agents_key(key(KeyCode::Enter), 111));
        assert!(!app.agents.context.expanded);
        assert_eq!(app.agents.context.scroll, 0);
        let back = KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT);
        assert!(app.agents_key(back, 111));
        assert!(!app.agents.context.focused);
        assert!(app.agents_key(key(KeyCode::Enter), 111));
        assert!(app.task_detail && app.tasks[app.task_index].run_id == "selected");
        app.task_picker = false;
        assert!(app.agents_key(key(KeyCode::Tab), 111));
        assert!(app.agents_key(key(KeyCode::Esc), 111));
        assert!(!app.rail_has_focus());
        assert!(!app.agents.context.focused);
        assert!(app.agents_key(focus, 111));
        assert!(app.agents_key(key(KeyCode::Tab), 111));
        assert!(app.agents_key(key(KeyCode::Enter), 111));
        app.agents.resize(110, 16);
        assert!(!app.rail_has_focus() && !app.agents.context.focused);
        assert!(app.agents.context.expanded);
        assert!(!app.agents_key(key(KeyCode::Tab), 110));
        assert!(app.agents_key(focus, 110));
        assert!(!app.task_detail && app.tasks[app.task_index].run_id == "selected");
        assert_eq!(
            app.active.as_ref().map(|run| run.status),
            Some(RunStatus::Running)
        );
        assert_eq!(app.tasks, tasks);
        assert_eq!(app.input, "/se");
        assert_eq!(app.scroll, Some(7));
        assert!(app.queue.entries.is_empty() && app.submission.is_none());
    }

    #[tokio::test]
    async fn focus_and_exact_inspection_preserve_draft_scroll_and_queue() {
        let dir = crate::test_support::private_tempdir().unwrap();
        let host = Host::open(
            Config {
                workspace: dir.path().into(),
                data_dir: dir.path().join("state"),
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap();
        let mut app = App {
            session: host.store.create_session().await.unwrap().id,
            input: "/se".into(),
            scroll: Some(7),
            ..App::default()
        };
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let focus = KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL);
        assert!(app.agents_key(focus, 111));
        assert!(app.agents_key(key(KeyCode::Enter), 111)); // empty does nothing
        assert!(!app.task_picker);
        assert!(!app.completion_key(key(KeyCode::Enter)));
        assert!(!app.agents_key(key(KeyCode::F(5)), 111));
        assert!(!app.agents_key(key(KeyCode::F(6)), 111));
        app.queue_picker = true;
        assert!(!app.rail_has_focus());
        assert!(!app.agents_key(focus, 111));
        assert!(!app.agents_key(key(KeyCode::PageDown), 111));
        app.queue_picker = false;

        app.tasks = vec![
            task("failed", "root", RunStatus::Failed),
            task("nested", "failed", RunStatus::Completed),
            task("cancelled", "root", RunStatus::Cancelled),
        ];
        app.tasks[2].title = app.tasks[0].title.clone(); // Duplicate labels.
        app.agents.refresh(&app.tasks);
        assert!(app.agents_key(key(KeyCode::Char(' ')), 111)); // Collapse failed parent.
        assert!(app.agents_key(key(KeyCode::Down), 111)); // Now on cancelled (not nested).

        app.tasks.insert(0, task("new", "root", RunStatus::Running));
        app.agents.refresh(&app.tasks);
        app.agents.resize(110, 20);
        assert!(!app.rail_has_focus());
        assert!(app.agents_key(focus, 110)); // Narrow Ctrl+T uses the F5 catalog.
        assert_eq!(app.tasks[app.task_index].run_id, "cancelled");
        assert!(!app.task_detail);
        app.task_key(&host, key(KeyCode::Esc)).await;
        assert!(app.agents_key(focus, 111));
        assert!(app.agents.collapsed.contains("failed"));
        assert!(app.agents_key(key(KeyCode::Enter), 111));
        assert!(app.task_detail);
        assert_eq!(app.tasks[app.task_index].run_id, "cancelled");
        assert!(!app.agents_key(focus, 111)); // Inspector owns input.
        app.task_key(&host, key(KeyCode::PageUp)).await;
        assert_eq!(app.task_scroll, 10);
        assert_eq!(app.scroll, Some(7));
        app.task_key(&host, key(KeyCode::Esc)).await;
        assert!(!app.task_picker && app.composer_available());
        assert!(app.agents_key(focus, 111));
        assert!(app.agents_key(key(KeyCode::Up), 111)); // Still skips folded child.
        assert!(app.agents_key(key(KeyCode::Enter), 111));
        assert_eq!(app.tasks[app.task_index].run_id, "failed");
        app.task_key(&host, key(KeyCode::Esc)).await;
        assert!(!app.task_picker && app.composer_available());
        app.open_tasks(false); // F5 retains the original two-step detail/catalog flow.
        app.task_key(&host, key(KeyCode::Enter)).await;
        app.task_key(&host, key(KeyCode::Esc)).await;
        assert!(app.task_picker && !app.task_detail);
        app.task_key(&host, key(KeyCode::Esc)).await;
        assert!(!app.rail_has_focus());
        assert_eq!(app.input, "/se");
        assert_eq!(app.scroll, Some(7));
        assert!(host.active().await.is_empty());
        assert!(host.queue(&app.session).await.unwrap().entries.is_empty());
        assert!(host.store.events(&app.session, 0).await.unwrap().is_empty());
    }
}
