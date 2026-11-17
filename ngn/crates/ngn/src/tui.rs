use std::{io::stdout, sync::Arc, time::Duration};

use anyhow::{Result, ensure};
use crossterm::{
    event::{
        DisableBracketedPaste, EnableBracketedPaste, Event as TerminalEvent, EventStream, KeyCode,
        KeyEventKind, KeyModifiers,
    },
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use futures_util::StreamExt;
use ngn_core::{
    ApprovalMode, Host,
    host::Approval,
    queues::{QueueAction, QueueState},
    store::{Run, Session},
};
use ngn_protocol::{EventRecord, TaskInfo};
use ratatui::{
    Frame, Terminal,
    backend::CrosstermBackend,
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, List, ListItem, ListState, Paragraph, Wrap},
};
use uuid::Uuid;

use crate::view;

struct Restore;
impl Drop for Restore {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(stdout(), DisableBracketedPaste, LeaveAlternateScreen);
    }
}

#[derive(Default)]
struct App {
    session: String,
    sessions: Vec<Session>,
    records: Vec<EventRecord>,
    after: i64,
    input: String,
    status: String,
    active: Option<Run>,
    approval: Option<Approval>,
    scroll: u16,
    preview_scroll: u16,
    details: bool,
    picker: bool,
    picker_index: usize,
    tasks: Vec<TaskInfo>,
    task_picker: bool,
    task_index: usize,
    task_detail: bool,
    task_records: Vec<EventRecord>,
    task_after: i64,
    queue: QueueState,
    queue_picker: bool,
    queue_index: usize,
    submission: Option<(String, String, String)>,
}

impl App {
    async fn catch_up(&mut self, host: &Host) -> Result<()> {
        loop {
            let records = host.store.events(&self.session, self.after).await?;
            let count = records.len();
            if let Some(last) = records.last() {
                self.after = last.sequence;
            }
            self.records.extend(records);
            if self.records.len() > 5000 {
                self.records.drain(..self.records.len() - 5000);
            }
            if count < 1000 {
                break;
            }
        }
        Ok(())
    }

    async fn state(&mut self, host: &Host) {
        self.active = host
            .active()
            .await
            .into_iter()
            .find(|run| run.session_id == self.session);
        let approval = host.approvals().await.into_iter().find(|approval| {
            approval.root_session_id == self.session || approval.session_id == self.session
        });
        if self.approval.as_ref().map(|approval| &approval.id)
            != approval.as_ref().map(|approval| &approval.id)
        {
            self.preview_scroll = 0;
        }
        self.approval = approval;
        let selected = self
            .queue
            .entries
            .get(self.queue_index)
            .map(|entry| entry.run.id.clone());
        match host.queue(&self.session).await {
            Ok(queue) => {
                self.queue = queue;
                self.queue_index = selected
                    .and_then(|id| {
                        self.queue
                            .entries
                            .iter()
                            .position(|entry| entry.run.id == id)
                    })
                    .unwrap_or(0);
            }
            Err(error) => self.status = format!("Cannot load input queue: {error}"),
        }
        if self.task_picker {
            let selected = self
                .tasks
                .get(self.task_index)
                .map(|task| task.run_id.clone());
            match host.store.tasks(&self.session).await {
                Ok(tasks) => {
                    self.tasks = tasks;
                    if let Some(selected) = selected {
                        if let Some(index) =
                            self.tasks.iter().position(|task| task.run_id == selected)
                        {
                            self.task_index = index;
                        } else {
                            self.task_index = 0;
                            self.task_detail = false;
                            self.task_records.clear();
                            self.task_after = 0;
                        }
                    }
                }
                Err(error) => self.status = format!("Cannot load subagents: {error}"),
            }
            if self.task_detail
                && let Some(task) = self.tasks.get(self.task_index)
            {
                match host.store.events(&task.session_id, self.task_after).await {
                    Ok(records) => {
                        if let Some(record) = records.last() {
                            self.task_after = record.sequence;
                        }
                        self.task_records.extend(records);
                        if self.task_records.len() > 5000 {
                            self.task_records.drain(..self.task_records.len() - 5000);
                        }
                    }
                    Err(error) => self.status = format!("Cannot inspect subagent: {error}"),
                }
            }
        }
    }

    async fn switch(&mut self, host: &Host, id: String) -> Result<()> {
        host.store.session(&id).await?;
        self.session = id;
        self.after = 0;
        self.records.clear();
        self.scroll = 0;
        self.picker = false;
        self.task_picker = false;
        self.task_detail = false;
        self.queue_picker = false;
        self.catch_up(host).await?;
        self.state(host).await;
        Ok(())
    }
}

fn center(area: Rect, width: u16, height: u16) -> Rect {
    let width = width.min(area.width.saturating_sub(2));
    let height = height.min(area.height.saturating_sub(2));
    Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    )
}

fn draw(frame: &mut Frame, app: &App, host: &Host) {
    let accent = Color::Rgb(190, 224, 163);
    let muted = Color::Rgb(148, 158, 144);
    let rows = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(3),
        Constraint::Length(1),
        Constraint::Length(5),
        Constraint::Length(1),
    ])
    .split(frame.area());
    let provider = if host.config.backend == ngn_core::config::Backend::Demo {
        "offline Rust demo".to_string()
    } else {
        host.config
            .selected_provider()
            .map(|provider| format!("nagents · {}", provider.model))
            .unwrap_or_else(|_| "provider not configured · see config.example.json".into())
    };
    let header = Paragraph::new(vec![
        Line::from(vec![
            Span::styled(
                format!(" ngn {} ", env!("CARGO_PKG_VERSION")),
                Style::new().fg(accent).add_modifier(Modifier::BOLD),
            ),
            Span::raw(view::plain(&provider)),
        ]),
        Line::from(Span::styled(
            format!(
                " {} · {}",
                view::plain(&host.config.workspace.display().to_string()),
                app.session
            ),
            Style::new().fg(muted),
        )),
    ]);
    frame.render_widget(header, rows[0]);
    let text = if app.records.is_empty() {
        "\nWelcome to ngn.\n\nRust owns the harness, native tools, approvals and terminal rendering.\nThe optional nagents runtime supplies the Python agent/model loop.\n\nTry a prompt in --demo mode, or configure your provider.\n\nF2 conversations · F3 new · F4 tool detail · Ctrl+Q quit".into()
    } else {
        view::transcript(&app.records, app.details)
    };
    let paragraph = Paragraph::new(text).wrap(Wrap { trim: false }).block(
        Block::default()
            .borders(Borders::TOP)
            .border_style(Style::new().fg(Color::DarkGray)),
    );
    let total = paragraph.line_count(rows[1].width) as u16;
    let top = total
        .saturating_sub(rows[1].height.saturating_sub(1))
        .saturating_sub(app.scroll);
    frame.render_widget(paragraph.scroll((top, 0)), rows[1]);
    let status = if app.approval.is_some() {
        "Waiting for approval · Ctrl+Y allows once · Ctrl+D denies"
    } else if app.active.is_some() {
        "Agent working · Esc cancels · completed actions are not rolled back"
    } else {
        &app.status
    };
    frame.render_widget(
        Paragraph::new(view::plain(status)).style(Style::new().fg(muted)),
        rows[2],
    );
    let input_lines = app.input.lines().count().max(1) as u16;
    frame.render_widget(
        Paragraph::new(view::plain(&app.input))
            .wrap(Wrap { trim: false })
            .scroll((input_lines.saturating_sub(3), 0))
            .block(
                Block::bordered()
                    .title(format!(
                        " Message · Enter send/queue · {} pending{} · F6 queue ",
                        app.queue.entries.len(),
                        if app.queue.paused { " · PAUSED" } else { "" }
                    ))
                    .border_style(Style::new().fg(accent)),
            ),
        rows[3],
    );
    frame.render_widget(
        Paragraph::new(" F2 sessions · F3 new · F4 tools · F5 agents · F6 queue · Ctrl+Q quit ")
            .style(Style::new().fg(muted)),
        rows[4],
    );
    if app.picker {
        let area = center(frame.area(), 90, 22);
        frame.render_widget(Clear, area);
        let items = app.sessions.iter().enumerate().map(|(index, session)| {
            ListItem::new(format!(
                "{}{}  {}",
                if index == app.picker_index {
                    "› "
                } else {
                    "  "
                },
                view::plain(&session.title),
                session.id
            ))
            .style(if index == app.picker_index {
                Style::new().fg(accent)
            } else {
                Style::default()
            })
        });
        let mut selection = ListState::default().with_selected(Some(app.picker_index));
        frame.render_stateful_widget(
            List::new(items).block(
                Block::bordered().title(" Conversations · ↑/↓ select · Enter open · Esc close "),
            ),
            area,
            &mut selection,
        );
    }
    if app.task_picker {
        let area = center(frame.area(), 110, 30);
        frame.render_widget(Clear, area);
        if app.task_detail {
            let title = app
                .tasks
                .get(app.task_index)
                .map_or("Subagent", |task| task.title.as_str());
            let text = view::transcript(&app.task_records, app.details);
            let paragraph =
                Paragraph::new(text)
                    .wrap(Wrap { trim: false })
                    .block(Block::bordered().title(format!(
                        " Agent: {} · Esc back · Ctrl+X cancel ",
                        view::plain(title)
                    )));
            let lines = paragraph.line_count(area.width) as u16;
            frame.render_widget(
                paragraph.scroll((
                    lines
                        .saturating_sub(area.height.saturating_sub(2))
                        .saturating_sub(app.scroll),
                    0,
                )),
                area,
            );
        } else {
            let items = app.tasks.iter().map(|task| {
                ListItem::new(format!(
                    "{}{} · {:?}{} · {}",
                    "  ".repeat(task.depth.saturating_sub(1) as usize),
                    view::plain(&task.title),
                    task.status,
                    if task.read_only { " · read only" } else { "" },
                    task.run_id
                ))
            });
            let mut selected = ListState::default().with_selected(Some(app.task_index));
            frame.render_stateful_widget(
                List::new(items)
                    .block(
                        Block::bordered().title(
                            " Agents · ↑/↓ select · Enter inspect · Ctrl+X cancel · Esc close ",
                        ),
                    )
                    .highlight_style(Style::new().fg(accent)),
                area,
                &mut selected,
            );
        }
    }
    if app.queue_picker {
        let area = center(frame.area(), 118, 25);
        frame.render_widget(Clear, area);
        let items: Vec<ListItem<'_>> = if app.queue.entries.is_empty() {
            vec![ListItem::new(
                "No pending inputs. Running work is not affected by queue controls.",
            )]
        } else {
            app.queue
                .entries
                .iter()
                .enumerate()
                .map(|(index, entry)| {
                    ListItem::new(format!(
                        "{}. {} · {}",
                        index + 1,
                        view::plain(&entry.prompt.replace(['\n', '\r'], " "))
                            .chars()
                            .take(100)
                            .collect::<String>(),
                        entry.run.id
                    ))
                })
                .collect()
        };
        let mut selection = ListState::default().with_selected(Some(app.queue_index));
        frame.render_stateful_widget(List::new(items).block(Block::bordered().title(format!(
            " Input queue: {} · Ctrl+P pause/resume · Ctrl+X remove · Ctrl+K clear · Esc close ",
            if app.queue.paused { "PAUSED" } else { "RUNNING" }
        ))).highlight_style(Style::new().fg(accent)), area, &mut selection);
    }
    if let Some(approval) = &app.approval {
        let area = center(frame.area(), 110, 32);
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(view::plain(&approval.preview))
                .wrap(Wrap { trim: false })
                .scroll((app.preview_scroll, 0))
                .block(
                    Block::bordered()
                        .title(format!(
                            " Approval: depth {} · {} / {} · Ctrl+Y allow once · Ctrl+D deny · Esc cancel ",
                            approval.depth,
                            view::plain(&approval.agent_title),
                            view::plain(&approval.call.name)
                        ))
                        .border_style(Style::new().fg(Color::Yellow)),
                ),
            area,
        );
    }
}

async fn submit(app: &mut App, host: &Arc<Host>) -> Result<bool> {
    let input = app.input.trim().to_owned();
    if input.is_empty() {
        return Ok(false);
    }
    if matches!(input.as_str(), "/quit" | "/exit") {
        return Ok(true);
    }
    if input == "/tasks" {
        app.task_picker = true;
        app.task_detail = false;
        app.task_index = 0;
        app.state(host).await;
        app.input.clear();
        return Ok(false);
    }
    if input == "/queue" {
        app.queue_picker = true;
        app.state(host).await;
        app.input.clear();
        return Ok(false);
    }
    if let Some(action) = input.strip_prefix("/queue ") {
        let action = match action {
            "pause" => QueueAction::Pause,
            "resume" => QueueAction::Resume,
            "clear" => QueueAction::Clear,
            _ => anyhow::bail!("Use /queue pause, /queue resume or /queue clear"),
        };
        host.control_queue(&app.session, action).await?;
        app.state(host).await;
        app.input.clear();
        return Ok(false);
    }
    if input == "/new" {
        app.switch(host, host.store.create_session().await?.id)
            .await?;
    } else if input == "/sessions" {
        app.sessions = host.store.sessions().await?;
        app.picker_index = 0;
        app.picker = true;
    } else if let Some(id) = input.strip_prefix("/resume ") {
        app.switch(host, id.trim().to_owned()).await?;
    } else if input == "/help" {
        app.status = "/new · /sessions · /resume ID · /tasks · /queue [pause|resume|clear] · /quit. Enter queues while busy; approvals remain per-call.".into();
    } else {
        ensure!(!input.starts_with('/'), "Unknown command; try /help");
        if !app
            .submission
            .as_ref()
            .is_some_and(|(session, prompt, _)| session == &app.session && prompt == &input)
        {
            app.submission = Some((
                app.session.clone(),
                input.clone(),
                Uuid::new_v4().to_string(),
            ));
        }
        let request = &app
            .submission
            .as_ref()
            .expect("submission was just initialized")
            .2;
        let accepted = host
            .submit(&app.session, &input, request, ApprovalMode::Interactive)
            .await?;
        app.submission = None;
        app.status = if accepted.status == ngn_protocol::RunStatus::Queued {
            "Input queued · F6 inspect · /queue resume if paused".into()
        } else {
            "Input accepted".into()
        };
        app.scroll = 0;
    }
    app.input.clear();
    Ok(false)
}

pub async fn run(host: Arc<Host>, session: String) -> Result<()> {
    let mut receiver = host.events.subscribe();
    let mut app = App {
        session,
        sessions: Vec::new(),
        records: Vec::new(),
        after: 0,
        input: String::new(),
        status:
            "Ready · F5 inspects Rust-supervised agents · voice and channels are not migrated yet"
                .into(),
        active: None,
        approval: None,
        scroll: 0,
        preview_scroll: 0,
        details: false,
        picker: false,
        picker_index: 0,
        tasks: Vec::new(),
        task_picker: false,
        task_index: 0,
        task_detail: false,
        task_records: Vec::new(),
        task_after: 0,
        queue: QueueState::default(),
        queue_picker: false,
        queue_index: 0,
        submission: None,
    };
    app.catch_up(&host).await?;
    enable_raw_mode()?;
    let restore = Restore;
    execute!(stdout(), EnterAlternateScreen, EnableBracketedPaste)?;
    let mut terminal = Terminal::new(CrosstermBackend::new(stdout()))?;
    let mut input = EventStream::new();
    let signal = crate::shutdown_signal();
    tokio::pin!(signal);
    let mut ticks = tokio::time::interval(Duration::from_millis(100));
    loop {
        terminal.draw(|frame| draw(frame, &app, &host))?;
        tokio::select! {
            _ = &mut signal => break,
            _ = ticks.tick() => app.state(&host).await,
            received = receiver.recv() => {
                match received { Ok(record) if record.session_id == app.session => app.catch_up(&host).await?, Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => app.catch_up(&host).await?, _ => () }
                app.state(&host).await;
            }
            event = input.next() => {
                let Some(event) = event else { break; };
                match event? {
                    TerminalEvent::Key(key) if key.kind != KeyEventKind::Release => {
                        let control = key.modifiers.contains(KeyModifiers::CONTROL);
                        if control && key.code == KeyCode::Char('q') { break; }
                        if control && key.code == KeyCode::Char('c') { if let Some(run) = &app.active { let _ = host.cancel(&run.id).await; } else { break; } continue; }
                        if let Some(approval) = &app.approval {
                            match key.code {
                                KeyCode::Char('y') | KeyCode::Char('d') if control => { let result = host.decide(&approval.run_id, &approval.id, key.code == KeyCode::Char('y')).await; if let Err(error) = result { app.status = error.to_string(); } },
                                KeyCode::Esc => { let id = app.active.as_ref().map_or(approval.run_id.as_str(), |run| run.id.as_str()); let _ = host.cancel(id).await; },
                                KeyCode::PageUp => app.preview_scroll = app.preview_scroll.saturating_sub(10),
                                KeyCode::PageDown => app.preview_scroll = app.preview_scroll.saturating_add(10),
                                _ => (),
                            }
                            app.state(&host).await; continue;
                        }
                        if app.queue_picker {
                            let action = match key.code {
                                KeyCode::Esc => { app.queue_picker = false; None },
                                KeyCode::Up => { app.queue_index = app.queue_index.saturating_sub(1); None },
                                KeyCode::Down => { app.queue_index = (app.queue_index + 1).min(app.queue.entries.len().saturating_sub(1)); None },
                                KeyCode::Char('p') if control => Some(if app.queue.paused { QueueAction::Resume } else { QueueAction::Pause }),
                                KeyCode::Char('k') if control => Some(QueueAction::Clear),
                                KeyCode::Char('x') if control => {
                                    if let Some(entry) = app.queue.entries.get(app.queue_index) && let Err(error) = host.cancel(&entry.run.id).await { app.status = error.to_string(); }
                                    None
                                },
                                _ => None,
                            };
                            if let Some(action) = action && let Err(error) = host.control_queue(&app.session, action).await { app.status = error.to_string(); }
                            app.state(&host).await;
                            continue;
                        }
                        if app.task_picker {
                            match key.code {
                                KeyCode::Esc => { if app.task_detail { app.task_detail = false; } else { app.task_picker = false; } app.scroll = 0; },
                                KeyCode::Char('x') if control => { if let Some(task) = app.tasks.get(app.task_index) && let Err(error) = host.cancel(&task.run_id).await { app.status = error.to_string(); } },
                                KeyCode::Up if !app.task_detail => app.task_index = app.task_index.saturating_sub(1),
                                KeyCode::Down if !app.task_detail => app.task_index = (app.task_index + 1).min(app.tasks.len().saturating_sub(1)),
                                KeyCode::Enter if !app.tasks.is_empty() => { app.task_detail = true; app.task_records.clear(); app.task_after = 0; app.scroll = 0; },
                                KeyCode::PageUp => app.scroll = app.scroll.saturating_add(10),
                                KeyCode::PageDown => app.scroll = app.scroll.saturating_sub(10),
                                _ => (),
                            }
                            app.state(&host).await; continue;
                        }
                        if app.picker {
                            match key.code {
                                KeyCode::Esc => app.picker = false,
                                KeyCode::Up => app.picker_index = app.picker_index.saturating_sub(1),
                                KeyCode::Down => app.picker_index = (app.picker_index + 1).min(app.sessions.len().saturating_sub(1)),
                                KeyCode::Enter => { if let Some(session) = app.sessions.get(app.picker_index) { let id = session.id.clone(); app.switch(&host, id).await?; } },
                                _ => (),
                            }
                            continue;
                        }
                        match key.code {
                            KeyCode::Esc => { if let Some(run) = &app.active { let _ = host.cancel(&run.id).await; } },
                            KeyCode::F(2) => { app.sessions = host.store.sessions().await?; app.picker = true; app.picker_index = 0; },
                            KeyCode::F(3) => { app.switch(&host, host.store.create_session().await?.id).await?; },
                            KeyCode::F(4) => app.details = !app.details,
                            KeyCode::F(5) => { app.task_picker = true; app.task_detail = false; app.task_index = 0; app.state(&host).await; },
                            KeyCode::F(6) => { app.queue_picker = true; app.queue_index = 0; app.state(&host).await; },
                            KeyCode::PageUp => app.scroll = app.scroll.saturating_add(10),
                            KeyCode::PageDown => app.scroll = app.scroll.saturating_sub(10),
                            KeyCode::Enter if key.modifiers.contains(KeyModifiers::SHIFT) => { if app.input.len() < 32_000 { app.input.push('\n'); } },
                            KeyCode::Enter => { match submit(&mut app, &host).await { Ok(true) => break, Ok(false) => (), Err(error) => app.status = error.to_string() } app.state(&host).await; },
                            KeyCode::Backspace => { app.input.pop(); },
                            KeyCode::Char(character) if !control && !character.is_control() && app.input.len() + character.len_utf8() <= 32_000 => app.input.push(character),
                            _ => (),
                        }
                    }
                    TerminalEvent::Paste(text) if app.approval.is_none() => { let text = view::plain(&text); if app.input.len() + text.len() <= 32_000 { app.input.push_str(&text); } else { app.status = "Paste exceeds 32000 UTF-8 bytes".into(); } },
                    _ => (),
                }
            }
        }
    }
    host.shutdown().await;
    drop(terminal);
    drop(restore);
    println!(
        "ngn {} · Session {}\nResume with: ngn -C {} --resume {}{}",
        env!("CARGO_PKG_VERSION"),
        app.session,
        view::plain(&host.config.workspace.display().to_string()),
        app.session,
        if host.config.backend == ngn_core::config::Backend::Demo {
            " --demo"
        } else {
            ""
        }
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ngn_core::{Config, config::Backend};

    #[tokio::test]
    async fn tui_submissions_queue_while_busy_and_controls_do_not_cancel_the_active_run() {
        let directory = tempfile::tempdir().unwrap();
        let host = Host::open(
            Config {
                workspace: directory.path().into(),
                data_dir: directory.path().join("state"),
                backend: Backend::Demo,
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap();
        let session = host.store.create_session().await.unwrap().id;
        let mut app = App {
            session,
            input: "/queue pause".into(),
            ..App::default()
        };
        submit(&mut app, &host).await.unwrap();
        app.input = "demo hold".into();
        submit(&mut app, &host).await.unwrap();
        app.input = "queued before resume".into();
        submit(&mut app, &host).await.unwrap();
        app.state(&host).await;
        assert_eq!(app.queue.entries.len(), 2);
        assert!(app.queue.paused);
        let root = app.queue.entries[0].run.id.clone();
        app.input = "/queue resume".into();
        submit(&mut app, &host).await.unwrap();
        app.state(&host).await;
        assert_eq!(app.active.as_ref().unwrap().id, root);
        app.input = "queued while busy".into();
        submit(&mut app, &host).await.unwrap();
        app.input = "/queue".into();
        submit(&mut app, &host).await.unwrap();
        assert!(app.queue_picker);
        assert_eq!(app.queue.entries.len(), 2);
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(130, 35)).unwrap();
        terminal.draw(|frame| draw(frame, &app, &host)).unwrap();
        let contents: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(contents.contains("Input queue: RUNNING"));
        assert!(contents.contains("queued while busy"));
        app.input = "/queue clear".into();
        submit(&mut app, &host).await.unwrap();
        assert!(app.queue.paused && app.queue.entries.is_empty());
        assert!(host.active().await.iter().any(|run| run.id == root));
        host.shutdown().await;
    }

    #[test]
    fn terminal_layout_renders_at_small_and_large_sizes() {
        let directory = tempfile::tempdir().unwrap();
        let host = Host::open(
            Config {
                workspace: directory.path().into(),
                data_dir: directory.path().join("state"),
                backend: Backend::Demo,
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap();
        let mut app = App {
            session: "ngn-test".into(),
            sessions: Vec::new(),
            records: Vec::new(),
            after: 0,
            input: "hello".into(),
            status: "Ready".into(),
            active: None,
            approval: None,
            scroll: 0,
            preview_scroll: 0,
            details: false,
            picker: false,
            picker_index: 0,
            tasks: Vec::new(),
            task_picker: false,
            task_index: 0,
            task_detail: false,
            task_records: Vec::new(),
            task_after: 0,
            queue: QueueState::default(),
            queue_picker: false,
            queue_index: 0,
            submission: None,
        };
        for (width, height) in [(1, 1), (20, 6), (40, 12), (120, 40)] {
            let mut terminal =
                Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
            terminal.draw(|frame| draw(frame, &app, &host)).unwrap();
        }
        app.picker = true;
        app.picker_index = 29;
        app.sessions = (0..30)
            .map(|index| Session {
                id: format!("session-{index}"),
                title: format!("Conversation{index}"),
                updated_at_ms: 0,
            })
            .collect();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
        terminal.draw(|frame| draw(frame, &app, &host)).unwrap();
        let contents: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(
            contents.contains("Conversation29"),
            "Selected conversation must scroll into view"
        );
    }

    #[tokio::test]
    async fn root_ui_inspects_children_and_approves_only_the_actual_child_call() {
        let directory = tempfile::tempdir().unwrap();
        let host = Host::open(
            Config {
                workspace: directory.path().into(),
                data_dir: directory.path().join("state"),
                backend: Backend::Demo,
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap();
        let session = host.store.create_session().await.unwrap();
        let root = host
            .start(
                &session.id,
                "demo hold",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Interactive,
            )
            .await
            .unwrap();
        let mut receiver = host.events.subscribe();
        let child = host.tool(&root, ngn_protocol::ToolCall { call_id:"preview-child".into(), name:"to_agent".into(), arguments:serde_json::json!({"prompt":"demo approval", "title":"Preview child", "background":true}) }).await.unwrap();
        let child_id = child.result["task"]["run_id"].as_str().unwrap();
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let record = receiver.recv().await.unwrap();
                if record.run_id == child_id
                    && matches!(record.event, ngn_protocol::Event::ApprovalRequested { .. })
                {
                    break;
                }
            }
        })
        .await
        .unwrap();
        let mut app = App {
            session: session.id.clone(),
            sessions: Vec::new(),
            records: Vec::new(),
            after: 0,
            input: String::new(),
            status: String::new(),
            active: None,
            approval: None,
            scroll: 0,
            preview_scroll: 0,
            details: false,
            picker: false,
            picker_index: 0,
            tasks: Vec::new(),
            task_picker: true,
            task_index: 0,
            task_detail: false,
            task_records: Vec::new(),
            task_after: 0,
            queue: QueueState::default(),
            queue_picker: false,
            queue_index: 0,
            submission: None,
        };
        app.state(&host).await;
        let approval = app.approval.as_ref().unwrap();
        assert_eq!(approval.run_id, child_id);
        assert_eq!(approval.agent_title, "Preview child");
        assert_eq!(approval.depth, 1);
        assert!(host.decide(&root.id, &approval.id, true).await.is_err());
        host.decide(child_id, &approval.id, false).await.unwrap();
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let record = receiver.recv().await.unwrap();
                if record.run_id == child_id
                    && matches!(record.event, ngn_protocol::Event::RunFinished { .. })
                {
                    break;
                }
            }
        })
        .await
        .unwrap();
        app.task_detail = true;
        app.state(&host).await;
        app.catch_up(&host).await.unwrap();
        assert_eq!(app.tasks.len(), 1);
        assert!(
            app.task_records
                .iter()
                .all(|record| record.session_id == app.tasks[0].session_id)
        );
        assert!(
            app.task_records
                .iter()
                .any(|record| matches!(record.event, ngn_protocol::Event::TextCompleted { .. }))
        );
        assert!(
            !app.records
                .iter()
                .any(|record| matches!(record.event, ngn_protocol::Event::TextDelta { .. }))
        );
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|frame| draw(frame, &app, &host)).unwrap();
        let contents: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(contents.contains("Agent: Preview child"));
        assert!(!directory.path().join("demo.txt").exists());
        host.shutdown().await;
    }
}
