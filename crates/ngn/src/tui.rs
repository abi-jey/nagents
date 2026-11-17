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
    store::{Run, Session},
};
use ngn_protocol::EventRecord;
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
        let approval = host
            .approvals()
            .await
            .into_iter()
            .find(|approval| approval.session_id == self.session);
        if self.approval.as_ref().map(|approval| &approval.id)
            != approval.as_ref().map(|approval| &approval.id)
        {
            self.preview_scroll = 0;
        }
        self.approval = approval;
    }

    async fn switch(&mut self, host: &Host, id: String) -> Result<()> {
        host.store.session(&id).await?;
        self.session = id;
        self.after = 0;
        self.records.clear();
        self.scroll = 0;
        self.picker = false;
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
                    .title(" Message · Enter send · Shift+Enter newline ")
                    .border_style(Style::new().fg(accent)),
            ),
        rows[3],
    );
    frame.render_widget(
        Paragraph::new(" F2 sessions · F3 new · F4 tools · PgUp/PgDn scroll · Ctrl+Q quit ")
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
                            " Approval: {} · Ctrl+Y allow once · Ctrl+D deny · Esc cancel ",
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
    ensure!(
        app.active.is_none(),
        "A run is active; wait or press Esc to cancel. Input is retained."
    );
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
        app.status = "/new · /sessions · /resume ID · /quit · F4 tool details. Modifying tools require explicit approval.".into();
    } else {
        ensure!(!input.starts_with('/'), "Unknown command; try /help");
        host.start(
            &app.session,
            &input,
            &Uuid::new_v4().to_string(),
            ApprovalMode::Interactive,
        )
        .await?;
        app.scroll = 0;
    }
    app.input.clear();
    Ok(false)
}

pub async fn run(host: Arc<Host>, session: String) -> Result<()> {
    let mut receiver = host.events.subscribe();
    let mut app = App { session, sessions:Vec::new(), records:Vec::new(), after:0, input:String::new(), status:"Ready · this first Rust release does not yet include legacy voice, channels or delegation".into(), active:None, approval:None, scroll:0, preview_scroll:0, details:false, picker:false, picker_index:0 };
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
                                KeyCode::Esc => { let _ = host.cancel(&approval.run_id).await; },
                                KeyCode::PageUp => app.preview_scroll = app.preview_scroll.saturating_sub(10),
                                KeyCode::PageDown => app.preview_scroll = app.preview_scroll.saturating_add(10),
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
                            KeyCode::F(2) if app.active.is_none() => { app.sessions = host.store.sessions().await?; app.picker = true; app.picker_index = 0; },
                            KeyCode::F(3) if app.active.is_none() => { app.switch(&host, host.store.create_session().await?.id).await?; },
                            KeyCode::F(4) => app.details = !app.details,
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
}
