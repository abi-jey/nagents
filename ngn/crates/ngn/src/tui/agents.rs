//! The native catalog is shared with F5; only the rail owns tree/focus state.

use std::collections::{HashMap, HashSet};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ngn_core::Host;
use ngn_protocol::{RunStatus, TaskInfo};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Padding, Scrollbar, ScrollbarOrientation, ScrollbarState},
};

use super::App;
use crate::view;

const BREAKPOINT: u16 = 110;
const WIDTH: u16 = 31;
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
pub(super) struct Rail {
    pub(super) focused: bool,
    current: bool,
    nodes: Vec<Node>,
    selected: Option<String>,
    collapsed: HashSet<String>,
    viewport: usize,
    offset: usize,
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
        self.viewport = usize::from(height.saturating_sub(3));
        if !is_wide(width) {
            self.focused = false;
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

    pub(super) fn draw(
        &self,
        frame: &mut Frame,
        area: Rect,
        tasks: &[TaskInfo],
        focused: bool,
        (accent, muted): (Color, Color),
        approval_run: Option<&str>,
    ) {
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
            return;
        }
        frame.render_widget(
            Line::styled(format!("Agents / {}", tasks.len()), heading),
            Rect { height: 1, ..inner },
        );
        let rows = Rect::new(inner.x, inner.y + 1, inner.width, inner.height - 1);
        if rows.is_empty() {
            return;
        }
        let visible = self.visible(tasks);
        let cursor = self.cursor(&visible, tasks);
        let height = usize::from(rows.height);
        let start = window_start(self.offset, cursor, visible.len(), height);
        let overflow = visible.len().saturating_sub(height);
        if overflow > 0 {
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
            let selected = focused && start + row == cursor;
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
    }
}

impl App {
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
            } else {
                self.agents.focused = !self.agents.focused;
                if self.agents.focused && self.agents.selected.is_none() {
                    self.agents.key(KeyCode::Home, &self.tasks);
                }
            }
            return true;
        }
        if !is_wide(width) || !self.rail_has_focus() || matches!(key.code, KeyCode::F(_)) {
            return false;
        }
        if key.modifiers.is_empty() {
            if key.code == KeyCode::Esc {
                self.agents.focused = false;
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
                self.agents.focused = false;
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

    use ngn_core::Config;
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
        let app = App {
            input: "draft".into(),
            ..App::default()
        };
        for width in [110, 111] {
            let mut terminal = Terminal::new(TestBackend::new(width, 20)).unwrap();
            terminal
                .draw(|frame| super::super::draw(frame, &app, &host, Instant::now()))
                .unwrap();
            let buffer = terminal.backend().buffer();
            assert_eq!(buffer[(width - 1, 13)].symbol(), "┐"); // Composer top-right.
            assert!(row(buffer, 14, 0..width).contains("draft"));
            if width == 111 {
                assert_eq!(buffer[(80, 3)].symbol(), "╭");
                assert_eq!(buffer[(110, 3)].symbol(), "╮");
                assert_eq!(buffer[(110, 12)].symbol(), "╯");
                assert!(row(buffer, 3, 80..111).contains("Agents / Ctrl+T"));
                assert!(row(buffer, 4, 82..109).starts_with("Agents / 0"));
            } else {
                assert!(!row(buffer, 3, 0..110).contains("Agents /"));
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
            for (height, expected) in [(11, 2), (13, 4), (14, 4), (15, 4)] {
                let mut terminal = Terminal::new(TestBackend::new(111, height)).unwrap();
                terminal
                    .draw(|frame| super::super::draw(frame, &app, &host, now))
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
        rail.resize(111, 7);
        let mut terminal = Terminal::new(TestBackend::new(32, 7)).unwrap();
        let paint = |terminal: &mut Terminal<TestBackend>, rail: &Rail, approval_run| {
            terminal
                .draw(|frame| {
                    rail.draw(
                        frame,
                        Rect::new(1, 0, WIDTH, 7),
                        &tasks,
                        true,
                        (Color::LightGreen, Color::Gray),
                        approval_run,
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
            scroll: 7,
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
        assert_eq!(app.scroll, 7);
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
        assert_eq!(app.scroll, 7);
        assert!(host.active().await.is_empty());
        assert!(host.queue(&app.session).await.unwrap().entries.is_empty());
        assert!(host.store.events(&app.session, 0).await.unwrap().is_empty());
    }
}
