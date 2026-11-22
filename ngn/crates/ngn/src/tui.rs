use std::{
    collections::BTreeSet,
    io::{self, Write, stdout},
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, ensure};
#[cfg(unix)]
use crossterm::event::{
    KeyboardEnhancementFlags, PopKeyboardEnhancementFlags, PushKeyboardEnhancementFlags,
};
use crossterm::{
    cursor::{Hide, Show},
    event::{
        DisableBracketedPaste, EnableBracketedPaste, Event as TerminalEvent, EventStream, KeyCode,
        KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind,
    },
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use futures_util::StreamExt;
use ngn_core::{
    ApprovalMode, Host,
    host::{Approval, IdleTurnChanged, InputAdmission},
    queues::{QueueAction, QueueState},
    steering::{SteeringState, SteeringStatus},
    store::Run,
};
use ngn_protocol::{EventRecord, TaskInfo};
use ratatui::{
    Frame, Terminal,
    backend::CrosstermBackend,
    buffer::Buffer,
    layout::{Constraint, Layout, Position, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, Clear, List, ListItem, ListState, Paragraph, Wrap},
};
use uuid::Uuid;

use crate::{animations_enabled, view};

mod agents;
mod approval;
mod clipboard;
mod code_fences;
mod completion;
mod composer;
mod footer;
mod markdown;
mod picker;
mod scrollbar;
mod selection;
mod shimmer;
mod startup;
mod text_layout;
mod tool_cards;

// Shared by the draw pass and Markdown rendering.
const ACCENT: Color = Color::Rgb(190, 224, 163);
const MUTED: Color = Color::Rgb(148, 158, 144);

use completion::Command;

const HELP: &str = "\
Basics
  Enter          Send · while a turn runs, steer it
  Shift+Enter    New line
  ←/→ Home/End   Move in the draft · Ctrl+←/→ by word · ↑/↓ between lines
  Delete         Delete forward · Ctrl+Backspace deletes a word
  Ctrl+Enter     Queue the message for after the current turn
  Esc            Stop the current turn (finished actions stay done)
  /              Commands · ↑/↓ select · Tab/Enter fill · Esc close
  Ctrl+Q         Quit (/quit) · Ctrl+C also quits

Approvals
  y or Enter     Allow a pending tool call once
  n              Deny it · Esc stops the turn · PgUp/PgDn scroll

Conversations
  F2             Past conversations (/sessions, /resume ID)
  F3             New conversation (/new)

Reading
  PgUp/PgDn      Scroll · the mouse wheel also scrolls
  Scrollbar      Click the track or drag the thumb
  Click a tool   Show or hide its details
  F4             Tool cards by keyboard · ↑/↓ select · Enter toggle
  F8             Pan wide code blocks · ←/→ · Home/End · Esc back
  Drag           Select text; release copies it · Esc clears
                 Your terminal's own selection is often Shift+drag

Agents and queue
  F5             Subagents (/tasks) · Ctrl+X cancels one
  Ctrl+T         Agents sidebar on wide screens · Tab for context
  F6             Pending messages (/queue pause|resume|clear)
                 /queue retry or /queue verify for an unconfirmed send
  F7             Steering receipts (/steering) · /steer TEXT";

/// Section headings are the unindented lines of `HELP`.
fn help_text() -> Text<'static> {
    HELP.lines()
        .map(|line| {
            if line.starts_with(' ') || line.is_empty() {
                Line::raw(line)
            } else {
                Line::styled(line, Style::new().fg(ACCENT).add_modifier(Modifier::BOLD))
            }
        })
        .collect()
}

const PLACEHOLDER: &str = "Ask anything · / for commands · F1 for shortcuts";

const WELCOME_HINT: &str = "Describe a task or ask a question · / for commands";
const WELCOME_KEYS: &str = "F1 shortcuts · F2 past conversations · Esc stops a turn";

/// The first screen of a new conversation. Rows that do not fit are left out
/// rather than wrapped, so a narrow terminal stays uncluttered.
fn welcome(workspace: &std::path::Path, width: u16) -> Vec<Line<'static>> {
    let home = std::env::var_os("HOME").map(std::path::PathBuf::from);
    let place = match home
        .as_deref()
        .and_then(|home| workspace.strip_prefix(home).ok())
    {
        Some(rest) if rest.as_os_str().is_empty() => "~".to_owned(),
        Some(rest) => format!("~/{}", rest.display()),
        None => workspace.display().to_string(),
    };
    let dim = Style::new().add_modifier(Modifier::DIM);
    let version = format!("ngn {}", env!("CARGO_PKG_VERSION"));
    // Keep the end of a long path, which names the project, behind an ellipsis.
    let mut place = view::plain(&place);
    let budget = usize::from(width).saturating_sub(Span::raw(&version).width() + 3);
    if Span::raw(&place).width() > budget && budget > 1 {
        let mut tail: Vec<char> = Vec::new();
        for c in place.chars().rev() {
            tail.insert(0, c);
            if Span::raw(tail.iter().collect::<String>()).width() + 1 > budget {
                tail.remove(0);
                break;
            }
        }
        place = format!("…{}", tail.into_iter().collect::<String>());
    }
    [
        Line::from(vec![
            Span::styled(version, Style::new().add_modifier(Modifier::BOLD)),
            Span::styled(format!(" · {place}"), dim),
        ]),
        Line::styled(WELCOME_HINT, dim),
        Line::styled(WELCOME_KEYS, dim),
    ]
    .into_iter()
    .filter(|line| line.width() <= usize::from(width))
    .collect()
}

#[derive(Default)]
struct Restore {
    mouse_reporting: bool,
    #[cfg(unix)]
    keyboard_pushed: bool,
}

impl Restore {
    fn enable_mouse(&mut self) -> io::Result<()> {
        // Arm cleanup even if writing the enable sequence fails partway.
        self.mouse_reporting = true;
        let mut output = stdout().lock();
        output.write_all(b"\x1b[?1003h\x1b[?1006h")?;
        output.flush()
    }

    #[cfg(unix)]
    fn enable_modified_keys(&mut self) -> std::io::Result<()> {
        // Unsupported terminals ignore the push/pop sequences. Don't query
        // support here: that would require a second reader or block on stdin.
        self.keyboard_pushed = true;
        execute!(
            stdout(),
            PushKeyboardEnhancementFlags(KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES)
        )
    }
}

impl Drop for Restore {
    fn drop(&mut self) {
        if self.mouse_reporting {
            let mut output = stdout().lock();
            let _ = output.write_all(b"\x1b[?1006l\x1b[?1003l");
            let _ = output.flush();
        }
        #[cfg(unix)]
        if self.keyboard_pushed {
            // The alternate screen has its own keyboard flags stack.
            let _ = execute!(stdout(), PopKeyboardEnhancementFlags);
        }
        let _ = disable_raw_mode();
        let _ = execute!(stdout(), DisableBracketedPaste, LeaveAlternateScreen, Show);
    }
}

#[derive(Default)]
struct SteeringSubmission {
    session: String,
    run: String,
    prompt: String,
    request: String,
}

const MAX_UNCONFIRMED_FIFO: usize = 16;

#[derive(Clone)]
struct FifoSubmission {
    session: String,
    prompt: String,
    request: String,
}

#[derive(Default)]
struct MouseUpdate {
    redraw: bool,
    selection_released: bool,
}

#[derive(Default)]
struct App {
    session: String,
    records: Vec<EventRecord>,
    after: i64,
    input: String,
    caret: composer::Caret,
    composer_palette: composer::Palette,
    terminal_background: Option<[u8; 3]>,
    no_color: bool,
    completion: completion::Completion,
    status: String,
    /// An admission error, uncertain receipt or dispatch warning, not a hint.
    failure: Option<String>,
    activity: footer::Activity,
    active: Option<Run>,
    approval: Option<Approval>,
    scroll: Option<usize>,
    transcript_area: Rect,
    scrollbar: scrollbar::Scrollbar,
    selection: selection::Selection,
    preview_scroll: u16,
    /// Where typing goes in the draft.
    cursor: composer::Cursor,
    /// When the visible approval first appeared (guards against early keys).
    approval_since: approval::Since,
    /// Whether the approval panel shows enough to decide; Allow waits for it.
    approval_fits: bool,
    tools: tool_cards::Cards,
    picker: Option<picker::Picker>,
    tasks: Vec<TaskInfo>,
    agents: agents::Rail,
    task_picker: bool,
    task_index: usize,
    task_detail: bool,
    task_from_rail: bool,
    task_scroll: u16,
    task_records: Vec<EventRecord>,
    task_after: i64,
    queue: QueueState,
    queue_picker: bool,
    queue_index: usize,
    // Bounded quarantine. Never evict uncertain attempts to make room.
    fifo_submissions: Vec<FifoSubmission>,
    /// A definite idle-to-active rejection is not permission to steer on retry.
    idle_rejected_draft: Option<String>,
    steering_submissions: Vec<SteeringSubmission>,
    steering: Vec<SteeringState>,
    steering_runs: BTreeSet<String>,
    steering_dirty: bool,
    steering_picker: bool,
    steering_scroll: u16,
    help: bool,
    help_scroll: u16,
}

impl App {
    async fn catch_up(&mut self, host: &Host) -> Result<()> {
        loop {
            let records = host.store.events(&self.session, self.after).await?;
            let count = records.len();
            if let Some(last) = records.last() {
                self.after = last.sequence;
            }
            for record in &records {
                if matches!(
                    record.event,
                    ngn_protocol::Event::TaskStarted { .. }
                        | ngn_protocol::Event::TaskUpdated { .. }
                        | ngn_protocol::Event::TaskFinished { .. }
                ) {
                    self.agents.invalidate();
                }
                if matches!(
                    record.event,
                    ngn_protocol::Event::RunStarted { .. }
                        | ngn_protocol::Event::SteeringUpdated { .. }
                        | ngn_protocol::Event::RunFinished { .. }
                ) {
                    self.steering_dirty = true;
                    self.steering_runs.insert(record.run_id.clone());
                }
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
        let previous = self.active.as_ref().map(|run| run.id.clone());
        let (active, child_text) = host.conversation_activity(&self.session).await;
        self.active = active;
        let changed = previous.as_deref() != self.active.as_ref().map(|run| run.id.as_str());
        if changed && let Err(error) = self.catch_up(host).await {
            self.status = format!("Cannot load conversation: {error}");
        }
        if self.steering_dirty || changed {
            let mut runs: Vec<String> = self
                .steering_runs
                .iter()
                .cloned()
                .chain(self.active.iter().map(|run| run.id.clone()))
                .chain(self.steering.iter().map(|state| state.run_id.clone()))
                .collect();
            runs.sort();
            runs.dedup();
            for run in runs {
                match host.steering(&run).await {
                    Ok(state) if state.session_id == self.session => {
                        self.steering.retain(|saved| saved.run_id != run);
                        self.steering.push(state);
                    }
                    Ok(_) => (),
                    Err(error) => self.status = format!("Cannot inspect steering: {error}"),
                }
            }
            self.steering_dirty = false;
        }
        let approvals = host.approvals().await;
        let approval = approvals
            .iter()
            .find(|approval| {
                approval.root_session_id == self.session || approval.session_id == self.session
            })
            .cloned();
        if self.approval.as_ref().map(|approval| &approval.id)
            != approval.as_ref().map(|approval| &approval.id)
        {
            self.preview_scroll = 0;
            self.approval_since.reset();
            // The pending modal and durable transcript have separate sources.
            // Pick up a committed decision before drawing the newly exposed row.
            if self.approval.is_some()
                && let Err(error) = self.catch_up(host).await
            {
                self.status = format!("Cannot load conversation: {error}");
            }
        }
        self.approval = approval;
        self.sync_activity();
        let selected = self
            .queue
            .entries
            .get(self.queue_index)
            .map(|entry| entry.run.id.clone());
        match host.queue(&self.session).await {
            Ok(queue) => {
                self.queue = queue;
                if self.queue.entries.is_empty() && self.status.starts_with("Input queued · ") {
                    self.status.clear();
                }
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
        if !self.agents.catalog_current() {
            let selected = self
                .tasks
                .get(self.task_index)
                .map(|task| task.run_id.clone());
            match host.store.tasks(&self.session).await {
                Ok(tasks) => {
                    self.tasks = tasks;
                    self.agents.refresh(&self.tasks);
                    if let Some(selected) = selected {
                        if let Some(index) =
                            self.tasks.iter().position(|task| task.run_id == selected)
                        {
                            self.task_index = index;
                        } else {
                            self.task_index = 0;
                            self.reset_task_detail(false);
                        }
                    }
                }
                Err(error) => self.status = format!("Cannot load subagents: {error}"),
            }
        }
        self.agents.activity.set_animated(self.activity.animated());
        self.agents.activity.sync_approvals(&approvals);
        self.agents
            .activity
            .sync_dispatches(&host.child_tool_dispatches());
        self.agents.activity.sync_text_phases(&child_text);
        if self.task_picker
            && self.task_detail
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

    fn sync_activity(&mut self) {
        self.activity.sync(
            self.active.as_ref().map(|run| run.id.as_str()),
            self.approval.is_some(),
            self.failure.is_some(),
            &self.records,
            Instant::now(),
            ngn_core::store::now_ms(),
        );
    }

    fn shows_overlay(&self) -> bool {
        self.help
            || self.picker.is_some()
            || self.task_picker
            || self.queue_picker
            || self.steering_picker
    }

    fn composer_available(&self) -> bool {
        self.tools_available() && !self.tools.focused()
    }

    fn tools_available(&self) -> bool {
        !self.shows_overlay() && self.approval.is_none() && !self.agents.focused
    }

    fn caret_visible(&self, now: Instant) -> bool {
        self.composer_available() && self.caret.visible(now)
    }

    fn transcript_key(&mut self, key: KeyEvent) -> bool {
        if self.tools_available() {
            self.tools.key(key, &mut self.scroll)
        } else {
            // Modal handlers take priority. If approval suspended transcript focus,
            // don't let its unhandled Enter/Space fall through to the composer.
            matches!(key.code, KeyCode::F(4) | KeyCode::F(8))
                || (self.tools.focused() && !matches!(key.code, KeyCode::F(_)))
        }
    }

    fn transcript_wheel(&mut self, mouse: MouseEvent) -> bool {
        let up = match mouse.kind {
            MouseEventKind::ScrollUp => true,
            MouseEventKind::ScrollDown => false,
            _ => return false,
        };
        let previous = self.scroll;
        let position = Position::new(mouse.column, mouse.row);
        if self.tools_available()
            // Textual interprets Shift/Ctrl+wheel as horizontal scrolling.
            && !mouse.modifiers.intersects(KeyModifiers::SHIFT | KeyModifiers::CONTROL)
            && (self.transcript_area.contains(position) || self.scrollbar.contains(position))
        {
            self.tools.scroll_rows(&mut self.scroll, up, 2);
        }
        self.scroll != previous
    }

    fn transcript_mouse(&mut self, mouse: MouseEvent) -> MouseUpdate {
        if !self.tools_available() {
            // Panels and overlays have no cards to click, but their text
            // can still be selected and copied.
            self.tools.clear_pointer();
            self.scrollbar.cancel_drag();
            let effect = self.selection.mouse(mouse, None);
            return MouseUpdate {
                redraw: effect.redraw,
                selection_released: effect.selection_released,
            };
        }
        let position = Position::new(mouse.column, mouse.row);
        let previous = self.scroll;
        let scrollbar = self.scrollbar.mouse(mouse, &mut self.scroll);
        let pointer = (!scrollbar).then_some(position);
        let hover = self.tools.hover(self.transcript_area, pointer);
        if scrollbar {
            let cleared = self.selection.clear();
            return MouseUpdate {
                redraw: hover || cleared || self.scroll != previous,
                selection_released: false,
            };
        }
        let target = self.tools.header_at(self.transcript_area, position);
        let effect = self.selection.mouse(mouse, target);
        let clicked = effect.click.is_some();
        if let Some(key) = effect.click {
            self.tools.click(key, &mut self.scroll);
        }
        if effect.hold_transcript {
            self.tools.hold_view(&mut self.scroll);
        }
        let scrolled = self.transcript_wheel(mouse);
        MouseUpdate {
            redraw: hover || effect.redraw || clicked || scrolled,
            selection_released: effect.selection_released,
        }
    }

    fn copy_selection(&mut self) {
        let feedback = match self.selection.copy_text() {
            None => return,
            Some(selection::CopyText::TooLarge) => " Selection exceeds 64 KiB ",
            Some(selection::CopyText::Text(text)) => match clipboard::request_copy(text) {
                Ok(()) => " Copy requested ",
                Err(_) => " Copy failed ",
            },
        };
        self.selection.feedback(feedback);
    }

    fn completion_key(&mut self, key: KeyEvent) -> bool {
        self.composer_available() && self.completion.key(&mut self.input, key)
    }

    async fn open_picker(&mut self, host: &Host) -> Result<()> {
        self.picker = Some(picker::Picker::new(host.store.sessions().await?));
        Ok(())
    }

    async fn picker_key(&mut self, host: &Host, key: KeyEvent) -> Result<()> {
        match self.picker.as_mut().and_then(|picker| picker.key(key)) {
            Some(picker::Choice::Close) => self.picker = None,
            Some(picker::Choice::New) => {
                self.switch(host, host.store.create_session().await?.id)
                    .await?;
            }
            Some(picker::Choice::Existing(id)) => self.switch(host, id).await?,
            None => (),
        }
        Ok(())
    }

    async fn switch(&mut self, host: &Host, id: String) -> Result<()> {
        host.store.session(&id).await?;
        self.session = id;
        self.after = 0;
        self.records.clear();
        self.scroll = None;
        self.tools.reset();
        self.scrollbar = scrollbar::Scrollbar::default();
        self.selection = selection::Selection::default();
        self.picker = None;
        self.tasks.clear();
        self.agents = agents::Rail::default();
        self.task_picker = false;
        self.task_index = 0;
        self.task_from_rail = false;
        self.reset_task_detail(false);
        self.queue_picker = false;
        self.steering_picker = false;
        self.help = false;
        self.status.clear();
        self.failure = None;
        self.steering.clear();
        self.steering_runs.clear();
        self.steering_dirty = true;
        self.catch_up(host).await?;
        self.tools.skip_completions(self.after);
        self.activity.reset(self.after, Instant::now());
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

fn footer_area(area: Rect) -> Rect {
    let height = 2.min(area.height);
    Rect::new(area.x, area.bottom() - height, area.width, height)
}

fn content_rows(area: Rect, menu_height: u16) -> [Rect; 3] {
    let body = Rect::new(area.x, area.y, area.width, area.height.saturating_sub(2));
    let composer = if agents::is_wide(area.width) {
        // Keep the draft compact on short screens. Grow it at the same heights
        // as before so the reclaimed rows go to the transcript and Agents rail.
        let extra = body.height.saturating_sub(11);
        body.height.min(3) + extra.min(2)
    } else if body.height >= 5 {
        5.min(body.height - 2)
    } else {
        body.height
    };
    let menu_height = menu_height.min(body.height.saturating_sub(composer + 2));
    Layout::vertical([
        Constraint::Min(0),
        Constraint::Length(menu_height),
        Constraint::Length(composer),
    ])
    .areas(body)
}

fn main_transcript<'a>(
    records: &'a [EventRecord],
    approval: Option<&Approval>,
    wall_ms: u64,
) -> Vec<view::TranscriptPart<'a>> {
    let mut parts = view::transcript(records, wall_ms, view::TranscriptScope::Main);
    if let Some(approval) = approval {
        for part in &mut parts {
            if let view::TranscriptPart::Card(card) = part
                && card.key == (approval.run_id.as_str(), approval.call.call_id.as_str())
                && card.status == "running"
            {
                // The live modal can precede its durable request. Don't
                // report running or extrapolate elapsed through that gap.
                card.status = "Approval waiting";
                card.elapsed = None;
                break;
            }
        }
    }
    parts
}

fn draw(frame: &mut Frame, app: &mut App, host: &Host, now: Instant) {
    let wall_ms = ngn_core::store::now_ms();
    let accent = ACCENT;
    let muted = MUTED;
    let area = frame.area();
    let footer = footer_area(area);
    let menu = app
        .composer_available()
        .then(|| app.completion.menu(&app.input))
        .flatten();
    let [conversation, suggestions, draft] =
        content_rows(area, menu.as_ref().map_or(0, completion::Menu::height));
    // Suggestions occupy the bottom of the left column, not rows of the
    // adjacent tree. The tree's height is independent of the draft/menu.
    let (mut transcript_area, rail) = agents::split(
        Rect {
            height: conversation.height + suggestions.height,
            ..conversation
        },
        app.has_rail_content(),
    );
    transcript_area.height = conversation.height;
    app.agents.clear_motion_rows();
    let context = rail.and_then(|rail| app.draw_agents(frame, rail, &host.config, (accent, muted)));
    app.agents.observe_context(context);
    // Keep the exact waiting child visible next to its approval prompt.
    let approval_dialog = app.approval.as_ref().map(|approval| {
        let body = approval::content(approval, app.no_color);
        let mut area = approval::panel(transcript_area, &body);
        if !approval::fits(area, &body) {
            // Cover the composer too rather than ask for a blind decision.
            area = approval::panel(frame.area(), &body);
        }
        (approval, area, body)
    });
    if let Some((_, area, body)) = &approval_dialog {
        app.approval_fits = approval::fits(*area, body);
        app.preview_scroll = app.preview_scroll.min(approval::max_scroll(*area, body));
    }
    let parts = main_transcript(&app.records, app.approval.as_ref(), wall_ms);
    app.tools.observe_completions(&parts, app.after, now);
    let interactive = app.tools_available();
    let sweep = (!app.shows_overlay())
        .then(|| app.activity.shimmering_tool())
        .flatten();
    // Keep the wrapping width stable while a conversation grows. On very
    // narrow terminals, give both cells to the text so wide glyphs still fit.
    let gutter =
        u16::from(transcript_area.width > 2 && (!app.records.is_empty() || app.failure.is_some()));
    let text_area = Rect {
        width: transcript_area.width - gutter,
        ..transcript_area
    };
    let mut transcript = tool_cards::Layout::new(
        &parts,
        text_area.width,
        Some(&app.tools),
        interactive,
        sweep,
        tool_cards::UserPaint::new(
            app.composer_palette.user_surface(),
            app.terminal_background,
            app.no_color,
        ),
    );
    if app.shows_overlay() {
        transcript.hide_wipes();
    } else if let Some((_, area, _)) = &approval_dialog {
        let area = *area;
        transcript.mask_wipes(area);
    }
    if app.records.is_empty() {
        for line in welcome(&host.config.workspace, text_area.width) {
            transcript.push_decoration(line);
        }
    }
    if let Some(reason) = &app.failure {
        // Keep the complete rejection in the scrollable conversation as well
        // as the one-line footer. It remains visible without submitting again.
        transcript.push(footer::failure_text(reason));
    }
    let (drawn_area, transcript_text) = app.tools.draw(
        frame,
        text_area,
        transcript,
        &mut app.scroll,
        now,
        Some(Style::new().fg(muted)),
    );
    app.transcript_area = drawn_area;
    app.scrollbar.update(
        Rect::new(
            drawn_area.right(),
            drawn_area.y,
            if interactive { gutter } else { 0 },
            drawn_area.height,
        ),
        app.tools.scroll_position(),
    );
    if let Some(menu) = menu {
        menu.draw(
            frame,
            Rect {
                width: transcript_area.width,
                ..suggestions
            },
            accent,
            muted,
        );
    }
    let mut composer = composer::border(draft.width, app.queue.entries.len(), accent, muted);
    let input_area = composer.inner(draft);
    let mut draft_text = interactive.then(|| selection::Pane::new(input_area));
    let _ = composer::draw(
        frame.buffer_mut(),
        input_area,
        &app.input,
        app.cursor.at(&app.input),
        app.caret_visible(now),
        app.composer_palette,
        draft_text.as_mut(),
    );
    if app.input.is_empty() && app.composer_available() && input_area.width > 2 {
        // Drawn after the draft pane is recorded, so it is never selectable.
        // The first cell stays free for the caret.
        let hint = Line::styled(
            PLACEHOLDER,
            Style::new().fg(MUTED).add_modifier(Modifier::DIM),
        );
        frame
            .buffer_mut()
            .set_line(input_area.x + 1, input_area.y, &hint, input_area.width - 1);
    }
    if let Some(hint) = app.selection.hint()
        && draft.width > 2
    {
        let width = usize::from(draft.width - 2);
        let title = Line::styled(hint, Style::new().fg(muted));
        composer = composer.title_bottom(footer::fit(title, width).right_aligned());
    }
    frame.render_widget(composer, draft);
    app.activity.draw(frame, footer, app, &host.config, now);
    if app.task_picker {
        let area = center(frame.area(), 110, 30);
        frame.render_widget(Clear, area);
        if app.task_detail {
            let title = app
                .tasks
                .get(app.task_index)
                .map_or("Subagent", |task| task.title.as_str());
            let block = Block::bordered().title(format!(
                " Agent: {} · Esc {} · Ctrl+X cancel ",
                view::plain(title),
                if app.task_from_rail { "close" } else { "back" }
            ));
            let inner = block.inner(area);
            let parts = view::transcript(
                &app.task_records,
                wall_ms,
                view::TranscriptScope::AgentInspector,
            );
            let transcript = tool_cards::Layout::new(
                &parts,
                inner.width,
                None,
                false,
                None,
                tool_cards::UserPaint::new(
                    app.composer_palette.user_surface(),
                    app.terminal_background,
                    app.no_color,
                ),
            );
            frame.render_widget(block, area);
            transcript.draw_from_bottom(frame, inner, app.task_scroll);
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
    if app.help {
        let area = center(frame.area(), 76, 36);
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(help_text())
                .block(Block::bordered().title(" Keyboard shortcuts · Esc close · PgUp/PgDn "))
                .wrap(Wrap { trim: false })
                .scroll((app.help_scroll, 0)),
            area,
        );
    }
    if let Some((approval, area, body)) = approval_dialog {
        let more = app.preview_scroll < approval::max_scroll(area, &body);
        frame.render_widget(Clear, area);
        frame.render_widget(
            body.scroll((app.preview_scroll, 0)).block(approval::block(
                approval,
                app.approval_fits,
                more,
            )),
            area,
        );
    }
    if app.steering_picker {
        let area = center(frame.area(), 118, 30);
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(steering_text(app))
                .wrap(Wrap { trim: false })
                .scroll((app.steering_scroll, 0))
                .block(
                    Block::bordered()
                        .title(" Steering receipts · F7/Esc close · PgUp/PgDn scroll · no replay "),
                ),
            area,
        );
    }
    if let Some(picker) = &app.picker {
        picker.draw(frame, accent, muted);
    }
    // Capture borders, status rows and overlays as painted, but not the scrollbar control.
    let screen = selection::Pane::screen(frame.buffer_mut());
    app.selection
        .frame(transcript_text, draft_text.unwrap_or_default(), screen);
    app.selection.paint(frame.buffer_mut());
    app.scrollbar.paint(frame.buffer_mut(), muted);
    if app.no_color {
        strip_color(frame.buffer_mut(), area);
    }
}

fn strip_color(buffer: &mut Buffer, area: Rect) {
    // Crossterm otherwise emits empty SGR color transitions under NO_COLOR.
    // Those reset attributes too, including the composer's reverse caret.
    buffer.set_style(
        area,
        Style::new()
            .fg(Color::Reset)
            .bg(Color::Reset)
            .underline_color(Color::Reset),
    );
}

fn steering_status(status: SteeringStatus) -> &'static str {
    match status {
        SteeringStatus::Pending => "pending · admitted, not offered yet",
        SteeringStatus::Offered => "offered · persistence not confirmed",
        SteeringStatus::Applied => {
            "applied · persistence acknowledged, not proof of model consumption"
        }
        SteeringStatus::Uncertain => {
            "uncertain · may be persisted; inspect conversation, no replay"
        }
        SteeringStatus::Undelivered => {
            "undelivered · turn ended before offer; not queued or replayed"
        }
    }
}

fn steering_text(app: &App) -> String {
    let mut text = String::from(
        "Enter or /steer TEXT targets the capable active root. Ctrl+Enter uses next-run FIFO.\n",
    );
    if let Some(run) = &app.active {
        let accepting = app.steering.iter().any(|state| {
            state.run_id == run.id
                && state.session_id == app.session
                && state.supported
                && state.accepting
        });
        text.push_str(&format!(
            "Exact active root {} · {}\n",
            run.id,
            if accepting {
                "accepting at persisted safe boundaries"
            } else {
                "unsupported or closing; no new steering admitted"
            }
        ));
    }
    let mut receipts: Vec<_> = app
        .steering
        .iter()
        .flat_map(|state| &state.receipts)
        .collect();
    receipts.sort_by_key(|receipt| receipt.position);
    if receipts.is_empty() {
        text.push_str("No steering receipts.\n");
    }
    for receipt in receipts {
        text.push_str(&format!(
            "\n#{} · {}\nRun {} · {}\n{}\n",
            receipt.position,
            steering_status(receipt.status),
            receipt.run_id,
            receipt.input_id,
            receipt.prompt
        ));
    }
    for binding in &app.steering_submissions {
        if binding.session == app.session
            || app
                .input
                .trim()
                .strip_prefix("/steer ")
                .unwrap_or(app.input.trim())
                .trim_start()
                == binding.prompt
        {
            text.push_str(&format!("\nAdmission unconfirmed · retry Enter or /steer in the original session with the SAME text, exact run/UUID retained\nSession {} · Run {} · request {}\n{}\n", binding.session, binding.run, binding.request, binding.prompt));
        }
    }
    view::plain(&text)
}

async fn steer(app: &mut App, host: &Host, prompt: &str) -> Result<()> {
    ensure!(
        !prompt.trim().is_empty(),
        "Use /steer TEXT; draft retained, not queued"
    );
    ensure!(
        !app.fifo_submissions
            .iter()
            .any(|binding| binding.prompt == prompt),
        "This text has a FIFO submission binding; use Ctrl+Enter to retry without steering"
    );
    ensure!(
        !app.steering_submissions
            .iter()
            .any(|binding| binding.prompt == prompt && binding.session != app.session),
        "This steering draft is bound to its original session/run/UUID; resume that session to retry, never rebind"
    );
    if !app
        .steering_submissions
        .iter()
        .any(|binding| binding.session == app.session && binding.prompt == prompt)
    {
        let run = app
            .active
            .as_ref()
            .context("No active root; steering draft retained, not queued")?;
        ensure!(
            run.session_id == app.session,
            "Active root belongs to another session; steering draft retained, not queued"
        );
        // Bind before admission so even a closed/unsupported run cannot make a
        // later Enter on the retained draft queue or target a different run.
        app.steering_submissions.push(SteeringSubmission {
            session: app.session.clone(),
            run: run.id.clone(),
            prompt: prompt.to_owned(),
            request: Uuid::new_v4().to_string(),
        });
    }
    let binding = app
        .steering_submissions
        .iter()
        .find(|binding| binding.session == app.session && binding.prompt == prompt)
        .expect("steering binding initialized");
    app.steering_dirty = true;
    let receipt = host
        .steer(&binding.run, &binding.prompt, &binding.request)
        .await
        .map_err(|error| anyhow::anyhow!("{error}; steering draft retained, not queued"))?;
    app.status = format!(
        "Steering {} · F7 inspect exact run",
        steering_status(receipt.status)
    );
    app.steering_submissions
        .retain(|binding| binding.session != app.session || binding.prompt != prompt);
    if app.idle_rejected_draft.as_deref() == Some(prompt) {
        app.idle_rejected_draft = None;
    }
    app.steering_dirty = true;
    app.input.clear();
    Ok(())
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum SubmissionKey {
    Enter,
    Queue,
}

async fn submit(app: &mut App, host: &Arc<Host>, key: SubmissionKey) -> Result<bool> {
    let input = app.input.trim().to_owned();
    if input.is_empty() {
        return Ok(false);
    }
    ensure!(
        key != SubmissionKey::Queue || !input.starts_with('/'),
        "Use Enter to complete or run a slash command"
    );
    let (name, args) = input
        .split_once(' ')
        .map_or((input.as_str(), None), |(name, args)| (name, Some(args)));
    match (Command::lookup(name), args) {
        (Some(Command::Exit), None) => return Ok(true),
        (Some(Command::Steering), None) => {
            app.steering_picker = true;
            app.steering_scroll = 0;
            app.steering_dirty = true;
            app.state(host).await;
        }
        (Some(Command::Steer), args) => {
            steer(app, host, args.unwrap_or_default().trim_start()).await?;
        }
        (Some(Command::Tasks), None) => {
            app.open_tasks(false);
            app.state(host).await;
        }
        (Some(Command::Queue), None) => {
            app.queue_picker = true;
            app.state(host).await;
        }
        (Some(Command::Queue), Some(action)) => {
            let (verb, request) = action
                .split_once(' ')
                .map_or((action, None), |(verb, request)| {
                    (verb, Some(request.trim()))
                });
            if matches!(verb, "retry" | "verify") {
                let index = app
                    .fifo_submissions
                    .iter()
                    .rposition(|binding| {
                        binding.session == app.session
                            && request.is_none_or(|request| request == binding.request)
                    })
                    .context("No matching unconfirmed FIFO draft in this conversation")?;
                let binding = &app.fifo_submissions[index];
                if verb == "retry" {
                    app.input.clone_from(&binding.prompt);
                    app.completion.edited(&app.input);
                    return Ok(false);
                }
                match host
                    .input_receipt(&binding.session, &binding.prompt, &binding.request)
                    .await
                {
                    Ok(Some(receipt)) => {
                        let message = format!("Original FIFO request confirmed · {}", receipt.id);
                        if receipt.status.is_none() {
                            app.failure = Some(format!("{message} · saved run status unreadable"));
                        } else {
                            app.status = message;
                        }
                        app.fifo_submissions.remove(index);
                    }
                    Ok(None) => {
                        app.fifo_submissions.remove(index);
                        app.status =
                            "Original FIFO request was not admitted · retry binding released"
                                .into();
                    }
                    Err(error) => {
                        app.failure = Some(format!("FIFO acceptance still unconfirmed · {error}"));
                    }
                }
                app.input.clear();
                return Ok(false);
            }
            let action = match action {
                "pause" => QueueAction::Pause,
                "resume" => QueueAction::Resume,
                "clear" => QueueAction::Clear,
                _ => anyhow::bail!("Use /queue pause, resume, clear, retry or verify"),
            };
            host.control_queue(&app.session, action).await?;
            app.state(host).await;
        }
        (Some(Command::New), None) => {
            app.switch(host, host.store.create_session().await?.id)
                .await?;
        }
        (Some(Command::Sessions | Command::Resume), None) => app.open_picker(host).await?,
        (Some(Command::Resume), Some(id)) => app.switch(host, id.trim().to_owned()).await?,
        (Some(Command::Help), None) => {
            app.help = true;
            app.help_scroll = 0;
        }
        _ => {
            ensure!(!input.starts_with('/'), "Unknown command; try /help");
            // A state refresh must not repurpose a rejected idle send as a
            // steering gesture. Only editing the text or an explicit route can.
            ensure!(
                key != SubmissionKey::Enter
                    || app.idle_rejected_draft.as_deref() != Some(input.as_str()),
                "Draft retained from an idle send · Ctrl+Enter to queue, or edit it before pressing Enter"
            );
            let bound_to_steering = app
                .steering_submissions
                .iter()
                .any(|binding| binding.prompt == input);
            if key == SubmissionKey::Enter && (app.active.is_some() || bound_to_steering) {
                steer(app, host, &input).await?;
                app.scroll = None;
                return Ok(false);
            }
            ensure!(
                !bound_to_steering,
                "This text is bound to steering; use Enter to retry the original run"
            );
            let saved = app
                .fifo_submissions
                .iter()
                .position(|binding| binding.prompt == input);
            let binding = if let Some(index) = saved {
                let binding = &app.fifo_submissions[index];
                ensure!(
                    binding.session == app.session,
                    "This FIFO draft is unconfirmed in {} · resume there to retry or verify",
                    binding.session
                );
                binding.clone()
            } else {
                ensure!(
                    app.fifo_submissions.len() < MAX_UNCONFIRMED_FIFO,
                    "Unconfirmed FIFO limit reached · resolve an original request with /queue verify"
                );
                FifoSubmission {
                    session: app.session.clone(),
                    prompt: input,
                    request: Uuid::new_v4().to_string(),
                }
            };
            let outcome = match key {
                SubmissionKey::Enter => {
                    host.submit_idle_with_receipt(
                        &binding.session,
                        &binding.prompt,
                        &binding.request,
                        ApprovalMode::Interactive,
                    )
                    .await
                }
                SubmissionKey::Queue => {
                    host.submit_with_receipt(
                        &binding.session,
                        &binding.prompt,
                        &binding.request,
                        ApprovalMode::Interactive,
                    )
                    .await
                }
            };
            app.scroll = None;
            match outcome {
                InputAdmission::Accepted { receipt, warning } => {
                    if app.idle_rejected_draft.as_deref() == Some(binding.prompt.as_str()) {
                        app.idle_rejected_draft = None;
                    }
                    if let Some(index) = saved {
                        app.fifo_submissions.remove(index);
                    }
                    if let Some(warning) = warning {
                        app.failure = Some(format!("Input accepted · {warning} · F6 inspect"));
                        app.status.clear();
                    } else {
                        app.status = match receipt.status {
                            Some(ngn_protocol::RunStatus::Queued) => {
                                "Input queued · F6 inspect · /queue resume if paused".into()
                            }
                            Some(ngn_protocol::RunStatus::Running) => String::new(),
                            // A retry can find a run cancelled since the original admission.
                            _ => format!("Original FIFO request confirmed · {}", receipt.id),
                        };
                    }
                }
                InputAdmission::Rejected(error) => {
                    // Exact receipt absent and no input was admitted; editing
                    // or deliberately submitting to another session is safe.
                    if let Some(index) = saved {
                        app.fifo_submissions.remove(index);
                    }
                    if error.is::<IdleTurnChanged>() {
                        app.idle_rejected_draft = Some(binding.prompt);
                    }
                    return Err(error);
                }
                InputAdmission::Unconfirmed(error) => {
                    app.failure = Some(format!(
                        "FIFO acceptance unconfirmed · /queue verify or /queue retry · request {} · {error}",
                        binding.request
                    ));
                    if saved.is_none() {
                        app.fifo_submissions.push(binding);
                    }
                    return Ok(false);
                }
            }
        }
    }
    app.input.clear();
    Ok(false)
}

async fn submit_from_enter(app: &mut App, host: &Arc<Host>, key: SubmissionKey) -> bool {
    if app.input.trim().is_empty() {
        return false;
    }
    app.failure = None;
    match submit(app, host, key).await {
        Ok(quit) => {
            if app.input.is_empty() {
                app.completion.edited(&app.input);
            }
            quit
        }
        Err(error) => {
            app.failure = Some(format!("Not sent: {error}"));
            app.scroll = None;
            false
        }
    }
}

pub async fn run(
    host: Arc<Host>,
    resume: Option<&str>,
    continue_session: bool,
    no_animations: bool,
) -> Result<()> {
    let mut receiver = host.events.subscribe();
    let animate = animations_enabled(
        no_animations,
        host.config.legacy_animations,
        std::env::var("TEXTUAL_ANIMATIONS").ok().as_deref(),
    );
    let no_color = std::env::var_os("NO_COLOR").is_some_and(|value| !value.is_empty());
    let query_background = !no_color && host.config.legacy_composer_theme.is_none();
    let mut app = App {
        records: Vec::new(),
        after: 0,
        input: String::new(),
        caret: composer::Caret::new(animate),
        composer_palette: composer::Palette::new(host.config.legacy_composer_theme, no_color),
        no_color,
        activity: footer::Activity::new(Instant::now(), animate),
        tools: tool_cards::Cards::new(animate),
        active: None,
        approval: None,
        preview_scroll: 0,
        cursor: composer::Cursor::default(),
        approval_since: approval::Since::default(),
        approval_fits: true,
        tasks: Vec::new(),
        task_picker: false,
        task_index: 0,
        task_detail: false,
        task_records: Vec::new(),
        task_after: 0,
        queue: QueueState::default(),
        queue_picker: false,
        queue_index: 0,
        ..App::default()
    };
    let signal = startup::shutdown_signal()?;
    tokio::pin!(signal);
    let initialize = async {
        app.session = crate::session(&host, resume, continue_session).await?;
        app.catch_up(&host).await?;
        app.tools.skip_completions(app.after);
        app.activity.reset(app.after, Instant::now());
        app.state(&host).await;
        Ok(())
    };
    let Some(mut startup_input) =
        startup::prepare(animate, query_background, initialize, signal.as_mut()).await?
    else {
        return Ok(());
    };
    enable_raw_mode()?;
    // Crossterm restores the startup mode; drop this guard before restoring
    // the original tty attributes held by startup_input.
    let mut restore = Restore::default();
    execute!(stdout(), EnterAlternateScreen, Hide)?;
    let Some(draft) = startup_input.draft_for_first_frame()? else {
        return Ok(());
    };
    execute!(stdout(), EnableBracketedPaste)?;
    if !draft.is_empty() {
        app.input.push_str(draft);
        app.completion.edited(&app.input);
        app.caret.restart(Instant::now());
    }
    app.terminal_background = startup_input.background();
    let mut terminal = Terminal::new(CrosstermBackend::new(stdout()))?;
    let mut input = None;
    let mut ticks = tokio::time::interval(Duration::from_millis(100));
    ticks.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut motion = tokio::time::interval(footer::FRAME);
    motion.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut cached: Option<Buffer> = None;
    let mut motion_only = false;
    let mut cached_caret_visible = false;
    let mut width = 0;
    let mut skip_draw = false;
    loop {
        let now = Instant::now();
        let activity_motion = app.activity.needs_frame(now);
        app.activity.advance(now);
        app.agents.advance_activity(&app.tasks, now);
        if !std::mem::take(&mut skip_draw) {
            width = terminal
                .draw(|frame| {
                    let area = frame.area();
                    // Suggestions never take vertical space from the tree.
                    app.agents
                        .resize(area.width, content_rows(area, 0)[0].height);
                    app.caret.focus(app.composer_available(), now);
                    let caret_visible = app.caret_visible(now);
                    if motion_only
                        && cached_caret_visible == caret_visible
                        && let Some(previous) = &cached
                        && previous.area == frame.area()
                    {
                        // Ratatui clears the back buffer for each draw. Reuse the last
                        // full frame rather than reflow 5000 transcript events at 30fps.
                        let area = footer_area(frame.area());
                        frame.buffer_mut().clone_from(previous);
                        app.tools.paint_motion(frame.buffer_mut(), now);
                        app.agents.paint_motion(frame.buffer_mut(), &app.tasks);
                        frame.render_widget(Clear, area);
                        app.activity.draw(frame, area, &app, &host.config, now);
                        if app.no_color {
                            let area = frame.area();
                            strip_color(frame.buffer_mut(), area);
                        }
                    } else {
                        draw(frame, &mut app, &host, now);
                        let animate = (activity_motion
                            || app.tools.needs_frame(now)
                            || app.agents.needs_frame())
                            && !app.shows_overlay();
                        cached = animate.then(|| frame.buffer_mut().clone());
                        cached_caret_visible = caret_visible;
                    }
                })?
                .area
                .width;
        }
        motion_only = false;
        let mut redraw = false;
        if input.is_none() {
            // Ratatui has flushed the first visible frame. Keep the startup
            // reader (including its UTF-8 state) until this point so that keys
            // typed before the frame cannot become workspace actions.
            let draft = tokio::select! {
                biased;
                _ = &mut signal => None,
                draft = startup_input.finish() => draft?,
            };
            let Some(draft) = draft else {
                return Ok(());
            };
            // Don't request mouse reports while the draft-only startup reader
            // still owns stdin. EventStream will decode them after handoff.
            restore.enable_mouse()?;
            if draft != app.input {
                app.input = draft;
                app.completion.edited(&app.input);
                app.caret.restart(Instant::now());
                redraw = true;
            }
            if app.terminal_background != startup_input.background() {
                app.terminal_background = startup_input.background();
                redraw = true;
            }
            // Startup owns stdin until here. Do not change the keyboard protocol
            // while the startup reader is still collecting draft-only input.
            #[cfg(unix)]
            restore.enable_modified_keys()?;
        }
        let input = input.get_or_insert_with(EventStream::new);
        if redraw {
            continue;
        }
        let animate = (activity_motion || app.tools.needs_frame(now) || app.agents.needs_frame())
            && !app.shows_overlay();
        tokio::select! {
            _ = &mut signal => break,
            _ = ticks.tick() => {
                if !app.shows_overlay() {
                    app.agents.recover_activity(&host.store, &app.tasks);
                }
                app.state(&host).await;
            },
            _ = motion.tick(), if animate => motion_only = true,
            received = receiver.recv() => {
                match received {
                    Ok(record) => {
                        if record.session_id == app.session {
                            app.catch_up(&host).await?;
                        } else {
                            app.agents.activity.ingest(&record);
                            if matches!(record.event, ngn_protocol::Event::TextDelta { .. } | ngn_protocol::Event::TextCompleted { .. }) {
                                // Private streaming text cannot alter the root's
                                // queue, catalog, or pending approval snapshot.
                                continue;
                            }
                        }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                        app.catch_up(&host).await?;
                        app.tools.skip_completions(app.after);
                        app.agents.activity.invalidate();
                    }
                    _ => ()
                }
                app.state(&host).await;
            }
            event = input.next() => {
                let Some(event) = event else { break; };
                match event? {
                    TerminalEvent::Key(key) if key.kind != KeyEventKind::Release => {
                        let control = key.modifiers.contains(KeyModifiers::CONTROL);
                        if key.modifiers == KeyModifiers::CONTROL && matches!(key.code, KeyCode::Char('c' | 'C' | 'q')) { break; }
                        if app.selection.clear() && key.code == KeyCode::Esc && key.modifiers.is_empty() {
                            continue;
                        }
                        if app.composer_available() { app.caret.restart(Instant::now()); }
                        // The FIFO shortcut belongs only to the composer. In
                        // particular it is not Enter/confirm in a focused picker.
                        if control && key.code == KeyCode::Enter && !app.composer_available() { continue; }
                        if app.picker.is_some() {
                            app.picker_key(&host, key).await?;
                            continue;
                        }
                        if key.code == KeyCode::F(7) { app.steering_picker = !app.steering_picker; app.steering_scroll = 0; app.steering_dirty = true; app.state(&host).await; continue; }
                        if app.steering_picker {
                            match key.code {
                                KeyCode::Esc => app.steering_picker = false,
                                KeyCode::PageUp => app.steering_scroll = app.steering_scroll.saturating_sub(10),
                                KeyCode::PageDown => app.steering_scroll = app.steering_scroll.saturating_add(10),
                                _ => (),
                            }
                            continue;
                        }
                        if let Some(approval) = &app.approval {
                            let quiet = app.approval_since.elapsed();
                            // Every key restarts the quiet period, so a y or
                            // Enter in the middle of typing never approves.
                            app.approval_since.reset();
                            let handled = match approval::action(key, quiet) {
                                approval::Action::Allow if !app.approval_fits => true,
                                approval::Action::Allow | approval::Action::Deny => {
                                    let allow = approval::action(key, Duration::MAX) == approval::Action::Allow;
                                    if let Err(error) = host.decide(&approval.run_id, &approval.id, allow).await { app.status = error.to_string(); }
                                    true
                                },
                                approval::Action::Cancel => { let id = app.active.as_ref().map_or(approval.run_id.as_str(), |run| run.id.as_str()); let _ = host.cancel(id).await; true },
                                approval::Action::ScrollUp => { app.preview_scroll = app.preview_scroll.saturating_sub(10); true },
                                approval::Action::ScrollDown => { app.preview_scroll = app.preview_scroll.saturating_add(10); true },
                                approval::Action::Ignore => true,
                                approval::Action::Pass => false,
                            };
                            if handled { app.state(&host).await; continue; }
                        }
                        if app.help {
                            match key.code {
                                KeyCode::F(1) | KeyCode::Esc => app.help = false,
                                KeyCode::PageUp => app.help_scroll = app.help_scroll.saturating_sub(5),
                                KeyCode::PageDown => app.help_scroll = (app.help_scroll + 5).min(HELP.len() as u16),
                                _ => (),
                            }
                            continue;
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
                            app.task_key(&host, key).await;
                            app.state(&host).await; continue;
                        }
                        if app.agents_key(key, width) {
                            if app.agents.focused { app.tools.blur(); }
                            if app.task_picker { app.state(&host).await; }
                            continue;
                        }
                        if app.transcript_key(key) { continue; }
                        if app.completion_key(key) { continue; }
                        match key.code {
                            KeyCode::Esc => { if let Some(run) = &app.active { let _ = host.cancel(&run.id).await; } },
                            KeyCode::F(1) => { app.help = true; app.help_scroll = 0; },
                            KeyCode::F(2) => app.open_picker(&host).await?,
                            KeyCode::F(3) => { app.switch(&host, host.store.create_session().await?.id).await?; },
                            KeyCode::F(5) => { app.open_tasks(false); app.state(&host).await; },
                            KeyCode::F(6) => { app.queue_picker = true; app.queue_index = 0; app.state(&host).await; },
                            KeyCode::PageUp | KeyCode::PageDown => app.tools.page(&mut app.scroll, key.code == KeyCode::PageUp),
                            // Approval and overlay shortcuts above retain their routing.
                            // Draft edits and submission require the same focus as the caret.
                            _ if !app.composer_available() => (),
                             KeyCode::Enter if key.modifiers == KeyModifiers::SHIFT => { if app.input.len() < 32_000 { app.cursor.insert(&mut app.input, "\n"); app.completion.edited(&app.input); } },
                             KeyCode::Enter if matches!(key.modifiers, KeyModifiers::NONE | KeyModifiers::CONTROL) => {
                                 let submission = if control { SubmissionKey::Queue } else { SubmissionKey::Enter };
                                 if submit_from_enter(&mut app, &host, submission).await { break; }
                                 app.state(&host).await;
                             },
                            KeyCode::Char(character) if !control && !character.is_control() && app.input.len() + character.len_utf8() <= 32_000 => { app.cursor.insert(&mut app.input, character.encode_utf8(&mut [0; 4])); app.completion.edited(&app.input); },
                            _ => {
                                if let Some(changed) = app.cursor.key(&mut app.input, key) {
                                    app.caret.restart(Instant::now());
                                    if changed { app.completion.edited(&app.input); }
                                }
                            },
                        }
                    }
                    TerminalEvent::Paste(text) => {
                        app.selection.clear();
                        if let Some(picker) = &mut app.picker {
                            picker.paste(&text);
                        } else if app.composer_available() {
                            let text = view::plain(&text);
                            if app.input.len() + text.len() > 32_000 {
                                app.status = "Paste exceeds 32000 UTF-8 bytes".into();
                            } else if !text.is_empty() {
                                app.caret.restart(Instant::now());
                                app.cursor.insert(&mut app.input, &text);
                                app.completion.edited(&app.input);
                            }
                        }
                    }
                    TerminalEvent::Mouse(event) => {
                        let update = app.transcript_mouse(event);
                        if update.selection_released {
                            app.copy_selection();
                        }
                        // No redraw for a steady press or unchanged hover.
                        skip_draw = !update.redraw;
                    }
                    TerminalEvent::Resize(..) => {
                        app.tools.clear_pointer();
                        app.scrollbar.cancel_drag();
                        app.selection.clear();
                    }
                    _ => (),
                }
            }
        }
    }
    drop(input);
    drop(terminal);
    drop(restore);
    drop(startup_input);
    host.shutdown().await;
    println!(
        "ngn {} · Session {}\nResume with: ngn -C {} --resume {}",
        env!("CARGO_PKG_VERSION"),
        app.session,
        view::plain(&host.config.workspace.display().to_string()),
        app.session
    );
    if let Some(path) = host.runtime_log_path() {
        println!("Runtime log: {}", view::plain(&path.display().to_string()));
    } else {
        println!("Runtime log: unavailable");
    }
    if let Some(path) = host.wire_log_path() {
        println!("Wire log: {}", view::plain(&path.display().to_string()));
        println!("Sensitive request/response payload logging enabled");
    } else {
        println!("Wire log off · metadata only; no request/response payloads");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ngn_core::{Config, config::Backend};

    #[test]
    fn cli_saved_motion_and_textual_override_control_footer_and_caret() {
        let now = Instant::now();
        let later = now + Duration::from_millis(600);
        for (no_animations, saved, textual, expected) in [
            (false, None, None, true),
            (false, Some(true), None, true),
            (false, Some(false), None, false),
            (false, Some(false), Some("full"), false),
            (false, None, Some("none"), false),
            (false, Some(true), Some("NoNe"), false),
            (true, None, None, false),
            (true, Some(true), Some("full"), false),
        ] {
            let animate = animations_enabled(no_animations, saved, textual);
            let mut activity = footer::Activity::new(now, animate);
            activity.sync(Some("root"), false, false, &[], now, 0);
            assert_eq!(activity.needs_frame(later), expected);

            let mut caret = composer::Caret::new(animate);
            caret.focus(true, now);
            assert!(caret.visible(now));
            assert_eq!(caret.visible(later), !expected);
            assert!(caret.visible(now + Duration::from_secs(1)));
        }
    }

    #[tokio::test]
    async fn enter_without_provider_explains_rejection_and_keeps_draft() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let config = Config {
            workspace: directory.path().into(),
            data_dir: directory.path().join("state"),
            ..Config::default()
        }
        .resolve()
        .unwrap();
        let host = Host::open(config).unwrap();
        let mut app = App {
            session: host.store.create_session().await.unwrap().id,
            input: "keep this draft".into(),
            ..App::default()
        };
        assert!(!app.completion_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        let reason = app.failure.as_deref().unwrap();
        assert!(reason.starts_with("Not sent: No provider selected"));
        assert!(reason.contains("providers.json"));
        assert_eq!(app.input, "keep this draft");
        assert!(host.queue(&app.session).await.unwrap().entries.is_empty());
        app.sync_activity();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(28, 32)).unwrap();
        let now = Instant::now();
        terminal
            .draw(|frame| draw(frame, &mut app, &host, now))
            .unwrap();
        let buffer = terminal.backend().buffer();
        let row = |y| -> String {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect()
        };
        assert!(row(30).trim_start().starts_with("No provider selected"));
        assert!(row(30).contains('…'));
        assert_eq!(buffer[(2, 30)].fg, Color::Red);
        assert!(row(31).contains("Provider not configured"));
        let transcript: String = (0..25).map(|y| row(y).trim_end().to_owned()).collect();
        assert!(transcript.contains("providers.json"), "{transcript}");
        assert!((0..30).any(|y| row(y).contains("keep this draft")));
        app.input = "/help".into();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        assert!(app.failure.is_none() && app.help);
        // Shortcuts are in the help overlay, not the two status rows.
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 32)).unwrap();
        terminal
            .draw(|frame| draw(frame, &mut app, &host, now))
            .unwrap();
        let screen: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        for expected in [
            "Basics",
            "Enter          Send · while a turn runs, steer it",
            "Ctrl+Enter     Queue the message for after the current turn",
            "y or Enter     Allow a pending tool call once",
            "F2             Past conversations",
        ] {
            assert!(screen.contains(expected), "missing help entry: {expected}");
        }
        app.help_scroll = 10;
        terminal
            .draw(|frame| draw(frame, &mut app, &host, now))
            .unwrap();
        let screen: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(screen.contains("Click a tool   Show or hide its details"));
    }

    #[test]
    fn empty_draft_shows_a_dim_placeholder_that_is_not_part_of_the_draft() {
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
        let now = Instant::now();
        for (input, shown) in [("", true), ("draft", false)] {
            let mut app = App {
                input: input.into(),
                ..App::default()
            };
            let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 16)).unwrap();
            terminal
                .draw(|frame| draw(frame, &mut app, &host, now))
                .unwrap();
            let buffer = terminal.backend().buffer();
            let [_, _, composer] = content_rows(buffer.area, 0);
            let row: String = (0..80)
                .map(|x| buffer[(x, composer.y + 1)].symbol())
                .collect();
            assert_eq!(row.contains(PLACEHOLDER), shown, "{row}");
            assert_eq!(app.input, input);
        }
    }

    #[test]
    fn empty_transcript_welcome_fits_its_rows_and_does_not_touch_the_draft() {
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
        let now = Instant::now();
        let mut app = App {
            input: "draft".into(),
            ..App::default()
        };
        app.caret.focus(true, now);
        for width in [1, 20, 42, 43, 80, 111] {
            let mut terminal =
                Terminal::new(ratatui::backend::TestBackend::new(width, 16)).unwrap();
            terminal
                .draw(|frame| draw(frame, &mut app, &host, now))
                .unwrap();
            let buffer = terminal.backend().buffer();
            let [transcript, _, composer] = content_rows(buffer.area, 0);
            let (main, _) = agents::split(transcript, false);
            let row = |y| -> String { (0..main.width).map(|x| buffer[(x, y)].symbol()).collect() };
            let expected = welcome(&host.config.workspace, main.width);
            for (y, line) in (0..).zip(&expected) {
                assert_eq!(row(y).trim_end(), line.to_string());
            }
            if width >= 80 {
                assert_eq!(expected.len(), 3);
                assert!(row(0).starts_with("ngn "));
                assert_eq!(row(1).trim_end(), WELCOME_HINT);
                assert!(buffer[(0, 1)].modifier.contains(Modifier::DIM));
            }
            if width <= 20 {
                // Only the version line survives, its path shortened with "…".
                assert!(expected.len() <= 1);
            }
            if width == 20 {
                assert!(row(0).starts_with("ngn "));
                assert!(row(0).contains('…'));
            }
            assert!(
                (u16::try_from(expected.len()).unwrap()..transcript.height)
                    .all(|y| row(y).trim().is_empty())
            );
            if width >= 20 {
                assert!(row(composer.y + 1).contains("draft"));
                assert!(
                    buffer[(6, composer.y + 1)]
                        .modifier
                        .contains(Modifier::REVERSED)
                );
            }
            if width >= 43 {
                assert!(row(15).contains("Provider not configured"));
            }
        }
        assert!(app.composer_available());
    }

    #[tokio::test]
    async fn fifo_dispatch_failure_after_commit_confirms_the_draft_and_shows_a_warning() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let config = Config {
            workspace: directory.path().into(),
            data_dir: directory.path().join("state"),
            backend: Backend::Demo,
            ..Config::default()
        }
        .resolve()
        .unwrap();
        let database = config.state_dir().join("host.sqlite3");
        let host = Host::open(config).unwrap();
        let mut app = App {
            session: host.store.create_session().await.unwrap().id,
            input: "first draft".into(),
            ..App::default()
        };
        let inject = rusqlite::Connection::open(database).unwrap();
        inject
            .execute_batch(
                "CREATE TRIGGER fail_dispatch BEFORE UPDATE OF status ON runs
             WHEN OLD.prompt = 'first draft' AND NEW.status = 'running'
             BEGIN SELECT RAISE(ABORT, 'test: dispatch after durable enqueue'); END;
             CREATE TRIGGER fail_enqueue BEFORE INSERT ON runs
             WHEN NEW.prompt = 'rejected draft'
             BEGIN SELECT RAISE(ABORT, 'test: enqueue rejected'); END;",
            )
            .unwrap();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        let warning = app.failure.as_deref().unwrap();
        assert!(warning.starts_with("Input accepted"));
        assert!(warning.contains("test: dispatch after durable enqueue"));
        assert!(app.input.is_empty() && app.fifo_submissions.is_empty());
        let first = host.queue(&app.session).await.unwrap().entries[0]
            .run
            .clone();

        app.input = "rejected draft".into();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Queue).await);
        assert_eq!(app.input, "rejected draft");
        assert!(app.fifo_submissions.is_empty());
        assert_eq!(
            app.failure.as_deref(),
            Some("Not sent: test: enqueue rejected")
        );
        assert_eq!(host.queue(&app.session).await.unwrap().entries.len(), 1);

        app.input = "second draft".into();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Queue).await);
        assert!(app.input.is_empty() && app.fifo_submissions.is_empty());
        let queue = host.queue(&app.session).await.unwrap();
        assert_eq!(queue.entries.len(), 2);
        assert_eq!(queue.entries[0].run.request_id, first.request_id);
        assert_eq!(queue.entries[1].prompt, "second draft");
        assert!(host.active().await.is_empty());

        // Retyping an already confirmed prompt is a new, deliberate submission.
        app.input = "first draft".into();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Queue).await);
        let queue = host.queue(&app.session).await.unwrap();
        assert_eq!(queue.entries.len(), 3);
        assert_ne!(queue.entries[2].run.request_id, first.request_id);
        host.shutdown().await;
    }

    #[tokio::test]
    async fn fifo_uncertainty_does_not_block_unrelated_inputs_and_reconciles_exactly() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let config = Config {
            workspace: directory.path().into(),
            data_dir: directory.path().join("state"),
            backend: Backend::Demo,
            ..Config::default()
        }
        .resolve()
        .unwrap();
        let database = config.state_dir().join("host.sqlite3");
        let host = Host::open(config).unwrap();
        let session = host.store.create_session().await.unwrap().id;
        let other = host.store.create_session().await.unwrap().id;
        host.control_queue(&session, QueueAction::Pause)
            .await
            .unwrap();
        host.control_queue(&other, QueueAction::Pause)
            .await
            .unwrap();
        let mut app = App {
            session: session.clone(),
            input: "first uncertain draft".into(),
            ..App::default()
        };
        let request = Uuid::new_v4().to_string();
        let prompt = app.input.clone();
        let run = host
            .submit(&session, &prompt, &request, ApprovalMode::Interactive)
            .await
            .unwrap();
        // Simulate losing the original response and an unreadable receipt identity.
        app.fifo_submissions.push(FifoSubmission {
            session: session.clone(),
            prompt: prompt.clone(),
            request: request.clone(),
        });
        let inject = rusqlite::Connection::open(database).unwrap();
        inject
            .execute("UPDATE runs SET prompt = X'FF' WHERE id = ?", [&run.id])
            .unwrap();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Queue).await);
        assert!(app.failure.as_deref().unwrap().contains("unconfirmed"));
        assert_eq!(app.input, prompt);

        app.input = "different draft".into();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        assert!(app.input.is_empty());
        assert_eq!(app.fifo_submissions[0].request, request);
        app.switch(&host, other.clone()).await.unwrap();
        app.input.clone_from(&prompt);
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Queue).await);
        assert!(
            app.failure
                .as_deref()
                .unwrap()
                .contains("draft is unconfirmed in")
        );
        assert!(host.queue(&other).await.unwrap().entries.is_empty());
        app.input = "healthy conversation".into();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Queue).await);
        assert_eq!(host.queue(&other).await.unwrap().entries.len(), 1);
        assert_eq!(app.fifo_submissions[0].request, request);

        // Terminal state is damaged but identity is readable again. Exact
        // admission remains authoritative and must not queue a replacement.
        inject
            .execute(
                "UPDATE runs SET prompt = ? WHERE id = ?",
                rusqlite::params![prompt, run.id],
            )
            .unwrap();
        host.control_queue(&session, QueueAction::Clear)
            .await
            .unwrap();
        inject
            .execute(
                "UPDATE runs SET status = 'test-unreadable-status' WHERE id = ?",
                [&run.id],
            )
            .unwrap();
        app.switch(&host, session.clone()).await.unwrap();
        app.input = "/queue retry".into();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        assert_eq!(app.input, prompt);
        assert_eq!(app.fifo_submissions[0].request, request);
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Queue).await);
        assert!(app.input.is_empty() && app.fifo_submissions.is_empty());
        let warning = app.failure.as_deref().unwrap();
        assert!(warning.starts_with("Input accepted") && warning.contains("status is unreadable"));
        assert!(host.queue(&session).await.unwrap().entries.is_empty());
        assert!(host.active().await.is_empty());

        // A positively verified absence releases only that binding, and never sends.
        app.fifo_submissions.push(FifoSubmission {
            session: session.clone(),
            prompt: "not admitted".into(),
            request: Uuid::new_v4().to_string(),
        });
        app.input = format!("/queue verify {}", app.fifo_submissions[0].request);
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        assert!(app.status.contains("was not admitted"));
        assert!(app.fifo_submissions.is_empty());
        assert!(host.queue(&session).await.unwrap().entries.is_empty());

        app.input = prompt;
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        let queue = host.queue(&session).await.unwrap();
        assert!(queue.paused);
        assert_eq!(queue.entries.len(), 1);
        assert_ne!(queue.entries[0].run.request_id, request);
        host.shutdown().await;
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn unchanged_idle_rejection_never_steers_the_new_capable_root() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let mut config: Config = serde_json::from_value(serde_json::json!({
            "runner": ["/bin/sh", "-c", r#"
                printf '%s\n' '{"protocol_version":1,"run_id":"","message":{"type":"ready","runtime":"composer-fixture","capabilities":["host_tools","cancel","safe_steering"]}}'
                IFS= read -r start || exit
                IFS= read -r cancel
            "#],
            "provider": "fixture",
            "providers": {"fixture": {"kind": "openai_compatible", "model": "offline",
                "api": "chat_completions", "base_url": "http://127.0.0.1:1/v1",
                "api_key_env": "NGN_COMPOSER_FIXTURE_KEY"}},
        }))
        .unwrap();
        config.workspace = directory.path().into();
        config.data_dir = directory.path().join("state");
        let host = Host::open(config.resolve().unwrap()).unwrap();
        let session = host.store.create_session().await.unwrap().id;
        let root = host
            .start(
                &session,
                "active fixture",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Interactive,
            )
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(5), async {
            while !host.steering(&root.id).await.unwrap().accepting {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        let mut app = App {
            session: session.clone(),
            input: "typed while idle".into(),
            ..App::default()
        };

        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        assert!(
            app.failure
                .as_deref()
                .unwrap()
                .contains("An active turn started")
        );
        assert_eq!(app.input, "typed while idle");
        app.state(&host).await;
        assert_eq!(app.active.as_ref().unwrap().id, root.id);
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        assert!(
            app.failure
                .as_deref()
                .unwrap()
                .contains("Draft retained from an idle send")
        );
        assert_eq!(app.input, "typed while idle");
        assert!(host.steering(&root.id).await.unwrap().receipts.is_empty());
        assert!(host.queue(&session).await.unwrap().entries.is_empty());

        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Queue).await);
        assert!(app.input.is_empty());
        assert!(host.steering(&root.id).await.unwrap().receipts.is_empty());
        assert_eq!(
            host.queue(&session).await.unwrap().entries[0].prompt,
            "typed while idle"
        );

        // A second stale draft can be deliberately steered, but not by merely
        // repeating Enter. The explicit command still targets the exact root.
        app.active = None;
        app.input = "explicit steering after a race".into();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        app.state(&host).await;
        app.input = "/steer explicit steering after a race".into();
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        let receipts = host.steering(&root.id).await.unwrap().receipts;
        assert_eq!(receipts.len(), 1);
        assert_eq!(receipts[0].prompt, "explicit steering after a race");
        assert!(app.idle_rejected_draft.is_none());
        assert_eq!(host.queue(&session).await.unwrap().entries.len(), 1);
        host.shutdown().await;
    }

    #[tokio::test]
    async fn unsupported_steering_preserves_draft_and_retries_do_not_rebind_to_a_new_root_or_fifo()
    {
        let directory = crate::test_support::private_tempdir().unwrap();
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
        let root = host
            .start(
                &session,
                "demo hold",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Interactive,
            )
            .await
            .unwrap();
        let mut app = App {
            session: session.clone(),
            input: "retained text".into(),
            ..App::default()
        };
        app.state(&host).await;
        assert!(
            submit(&mut app, &host, SubmissionKey::Enter)
                .await
                .unwrap_err()
                .to_string()
                .contains("has not negotiated safe_steering")
        );
        assert_eq!(app.input, "retained text");
        assert!(host.queue(&session).await.unwrap().entries.is_empty());
        let request = app.steering_submissions[0].request.clone();
        assert_eq!(app.steering_submissions[0].run, root.id);
        host.cancel(&root.id).await.unwrap();
        tokio::time::timeout(Duration::from_secs(3), async {
            while host.active().await.iter().any(|run| run.id == root.id) {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        app.state(&host).await;
        assert!(app.active.is_none());
        assert!(
            submit(&mut app, &host, SubmissionKey::Enter)
                .await
                .unwrap_err()
                .to_string()
                .contains("exact running root")
        );
        host.control_queue(&session, QueueAction::Resume)
            .await
            .unwrap();
        let replacement = host
            .start(
                &session,
                "demo hold",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Interactive,
            )
            .await
            .unwrap();
        app.state(&host).await;
        app.input = "/steer retained text".into();
        assert!(
            submit(&mut app, &host, SubmissionKey::Enter)
                .await
                .unwrap_err()
                .to_string()
                .contains("exact running root")
        );
        let binding = &app.steering_submissions[0];
        assert_eq!(binding.run, root.id);
        assert_eq!(binding.request, request);
        assert_eq!(app.steering_submissions.len(), 1);
        app.input = "retained text".into();
        assert!(
            submit(&mut app, &host, SubmissionKey::Queue)
                .await
                .unwrap_err()
                .to_string()
                .contains("bound to steering")
        );
        assert_eq!(app.input, "retained text");
        assert_eq!(app.active.as_ref().unwrap().id, replacement.id);
        assert!(app.fifo_submissions.is_empty());
        let other = host.store.create_session().await.unwrap().id;
        app.session = other.clone();
        app.input = "/steer retained text".into();
        assert!(
            submit(&mut app, &host, SubmissionKey::Enter)
                .await
                .unwrap_err()
                .to_string()
                .contains("original session/run/UUID")
        );
        assert_eq!(app.input, "/steer retained text");
        assert!(steering_text(&app).contains(&root.id));
        assert!(host.queue(&session).await.unwrap().entries.is_empty());
        assert!(host.queue(&other).await.unwrap().entries.is_empty());
        host.shutdown().await;
    }

    #[test]
    fn receipt_view_retains_exact_identity_safe_text_and_honest_terminal_statuses() {
        let mut app = App::default();
        app.steering.push(SteeringState {
            run_id: "exact-old-root".into(),
            session_id: "session".into(),
            run_status: ngn_protocol::RunStatus::Cancelled,
            supported: false,
            accepting: false,
            receipts: [
                SteeringStatus::Pending,
                SteeringStatus::Offered,
                SteeringStatus::Applied,
                SteeringStatus::Uncertain,
                SteeringStatus::Undelivered,
            ]
            .into_iter()
            .enumerate()
            .map(|(index, status)| ngn_core::steering::SteeringReceipt {
                input_id: format!("receipt-{index}"),
                request_id: Uuid::new_v4().to_string(),
                run_id: "exact-old-root".into(),
                session_id: "session".into(),
                position: index as i64 + 1,
                prompt: "operator text\x1b[2J".into(),
                status,
                boundary_id: String::new(),
                accepted_at_ms: 0,
                updated_at_ms: 0,
            })
            .collect(),
            run_limit: 32,
            host_limit: 256,
            prompt_byte_limit: 32_000,
        });
        let text = steering_text(&app);
        for expected in [
            "Run exact-old-root",
            "operator text",
            "pending",
            "offered",
            "persistence acknowledged, not proof of model consumption",
            "uncertain",
            "may be persisted",
            "undelivered",
            "not queued or replayed",
        ] {
            assert!(text.contains(expected), "{expected}");
        }
        assert!(!text.contains('\x1b'));
        assert!(text.find("#1").unwrap() < text.find("#5").unwrap());
    }

    #[tokio::test]
    async fn tui_submissions_queue_while_busy_and_controls_do_not_cancel_the_active_run() {
        let directory = crate::test_support::private_tempdir().unwrap();
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
        submit(&mut app, &host, SubmissionKey::Enter).await.unwrap();
        app.input = "demo hold".into();
        submit(&mut app, &host, SubmissionKey::Enter).await.unwrap();
        app.input = "queued before resume".into();
        let request = Uuid::new_v4().to_string();
        let receipt = host
            .submit(
                &app.session,
                &app.input,
                &request,
                ApprovalMode::Interactive,
            )
            .await
            .unwrap();
        // A retained FIFO draft may retry by Ctrl+Enter without making a duplicate.
        app.fifo_submissions.push(FifoSubmission {
            session: app.session.clone(),
            prompt: app.input.clone(),
            request,
        });
        submit(&mut app, &host, SubmissionKey::Queue).await.unwrap();
        app.state(&host).await;
        assert_eq!(app.queue.entries.len(), 2);
        assert_eq!(app.queue.entries[1].run.id, receipt.id);
        assert!(app.queue.paused);
        let root = app.queue.entries[0].run.id.clone();
        app.input = "/queue resume".into();
        submit(&mut app, &host, SubmissionKey::Enter).await.unwrap();
        app.state(&host).await;
        assert_eq!(app.active.as_ref().unwrap().id, root);
        app.input = "queued before resume".into();
        app.fifo_submissions.push(FifoSubmission {
            session: app.session.clone(),
            prompt: app.input.clone(),
            request: receipt.request_id.clone(),
        });
        assert!(
            submit(&mut app, &host, SubmissionKey::Enter)
                .await
                .unwrap_err()
                .to_string()
                .contains("FIFO submission binding")
        );
        submit(&mut app, &host, SubmissionKey::Queue).await.unwrap();
        assert_eq!(host.queue(&app.session).await.unwrap().entries.len(), 1);
        assert!(app.input.is_empty() && app.fifo_submissions.is_empty());
        app.input = "queued while busy".into();
        // Simulate a stale idle view: the host must not queue this Enter.
        app.active = None;
        assert!(
            submit(&mut app, &host, SubmissionKey::Enter)
                .await
                .unwrap_err()
                .to_string()
                .contains("An active turn started")
        );
        assert_eq!(app.input, "queued while busy");
        assert!(app.fifo_submissions.is_empty()); // No receipt existed and no attempt was made.
        assert_eq!(host.queue(&app.session).await.unwrap().entries.len(), 1);
        app.state(&host).await;
        submit(&mut app, &host, SubmissionKey::Queue).await.unwrap();
        app.input = "/queue".into();
        submit(&mut app, &host, SubmissionKey::Enter).await.unwrap();
        assert!(app.queue_picker);
        assert_eq!(app.queue.entries.len(), 2);
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(130, 35)).unwrap();
        terminal
            .draw(|frame| draw(frame, &mut app, &host, Instant::now()))
            .unwrap();
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
        submit(&mut app, &host, SubmissionKey::Enter).await.unwrap();
        assert!(app.queue.paused && app.queue.entries.is_empty());
        assert!(host.active().await.iter().any(|run| run.id == root));
        host.shutdown().await;
    }

    #[test]
    fn terminal_layout_renders_at_small_and_large_sizes() {
        let directory = crate::test_support::private_tempdir().unwrap();
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
            input: "hello".into(),
            status: "Ready".into(),
            ..App::default()
        };
        for (width, height) in [(1, 1), (20, 6), (40, 12), (120, 40)] {
            let mut terminal =
                Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
            terminal
                .draw(|frame| draw(frame, &mut app, &host, Instant::now()))
                .unwrap();
        }
    }

    #[test]
    fn transcript_wheel_uses_rendered_bounds_and_preserves_scroll_anchor() {
        let directory = crate::test_support::private_tempdir().unwrap();
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
            input: "/".into(), // Keep the suggestion menu visible below the transcript.
            // One agent run keeps the rail beside the conversation at 120 columns.
            tasks: vec![TaskInfo {
                run_id: "child".into(),
                session_id: "session-child".into(),
                parent_run_id: "run".into(),
                parent_session_id: "session".into(),
                root_run_id: "run".into(),
                root_session_id: "session".into(),
                call_id: "call-child".into(),
                title: "child".into(),
                depth: 1,
                read_only: true,
                status: ngn_protocol::RunStatus::Completed,
            }],
            ..App::default()
        };
        app.completion.edited(&app.input);
        let lines = |sequence, text| EventRecord {
            schema_version: 1,
            sequence,
            session_id: "session".into(),
            run_id: "run".into(),
            timestamp_ms: 0,
            event: ngn_protocol::Event::TextDelta { text },
        };
        app.records.push(lines(1, "history\n".repeat(80)));
        let redraw = |app: &mut App, width, height| {
            Terminal::new(ratatui::backend::TestBackend::new(width, height))
                .unwrap()
                .draw(|frame| draw(frame, app, &host, Instant::now()))
                .unwrap();
        };
        let wheel = |column, row, kind| MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        };
        redraw(&mut app, 120, 16);
        let wide = app.transcript_area;
        redraw(&mut app, 100, 32);
        let tall = app.transcript_area;
        assert!(tall.width > wide.width && tall.height > wide.height);
        redraw(&mut app, 120, 16);
        let up = wheel(wide.x, wide.y, MouseEventKind::ScrollUp);

        for event in [
            wheel(wide.right() + 1, wide.y, MouseEventKind::ScrollUp), // Agents rail
            wheel(wide.x, wide.bottom(), MouseEventKind::ScrollUp),    // Command suggestions
            wheel(0, 12, MouseEventKind::ScrollUp),                    // Composer
            wheel(0, 15, MouseEventKind::ScrollUp),                    // Footer
            wheel(wide.x, wide.y, MouseEventKind::ScrollLeft),
            wheel(
                wide.x,
                wide.y,
                MouseEventKind::Down(crossterm::event::MouseButton::Left),
            ),
            MouseEvent {
                modifiers: KeyModifiers::SHIFT,
                ..up
            },
        ] {
            app.transcript_wheel(event);
            assert_eq!(app.scroll, None, "unexpected scroll for {event:?}");
        }
        app.agents.focused = true;
        app.transcript_wheel(up);
        assert!(app.agents.focused && app.scroll.is_none());
        app.agents.focused = false;
        app.queue_picker = true;
        app.transcript_wheel(up);
        assert!(app.queue_picker && app.scroll.is_none());
        app.queue_picker = false;

        app.approval = Some(Approval {
            id: "approval".into(),
            run_id: "run".into(),
            session_id: "session".into(),
            root_session_id: "session".into(),
            agent_title: "Assistant".into(),
            depth: 0,
            call: ngn_protocol::ToolCall {
                call_id: "call".into(),
                name: "write".into(),
                arguments: serde_json::json!({}),
            },
            preview: "Proposed write".into(),
        });
        app.transcript_wheel(up);
        assert!(app.approval.is_some() && app.scroll.is_none() && app.preview_scroll == 0);
        app.approval = None;

        let over_bar = wheel(wide.right(), wide.y, MouseEventKind::ScrollUp);
        assert!(app.transcript_mouse(over_bar).redraw);
        let anchored = app
            .scroll
            .expect("wheel over the scrollbar moved up from the tail");
        assert!(app.composer_available());
        assert_eq!(app.input, "/");
        app.records.push(lines(2, "streamed\n".repeat(40)));
        redraw(&mut app, 100, 32);
        assert_eq!(app.scroll, Some(anchored));
        let down = wheel(tall.x, tall.y, MouseEventKind::ScrollDown);
        for _ in 0..100 {
            app.transcript_wheel(down);
        }
        assert_eq!(app.scroll, None);
        let up = wheel(tall.x, tall.y, MouseEventKind::ScrollUp);
        for _ in 0..100 {
            app.transcript_wheel(up);
        }
        assert_eq!(app.scroll, Some(0));
    }

    #[test]
    fn transcript_scrollbar_shows_overflow_and_can_scroll_back_to_the_live_tail() {
        use crossterm::event::MouseButton::Left;
        use ratatui::backend::TestBackend;

        let directory = crate::test_support::private_tempdir().unwrap();
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
        let record = |sequence, text| EventRecord {
            schema_version: 1,
            sequence,
            session_id: "session".into(),
            run_id: "run".into(),
            timestamp_ms: 0,
            event: ngn_protocol::Event::TextDelta { text },
        };
        let mut app = App {
            records: vec![record(1, "short reply".into())],
            no_color: true,
            ..App::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(24, 19)).unwrap();
        let redraw = |terminal: &mut Terminal<TestBackend>, app: &mut App| {
            terminal
                .draw(|frame| draw(frame, app, &host, Instant::now()))
                .unwrap();
            let text = app.transcript_area;
            let buffer = terminal.backend().buffer();
            (text.top()..text.bottom())
                .map(|y| buffer[(text.right(), y)].symbol())
                .collect::<String>()
        };
        assert!(redraw(&mut terminal, &mut app).trim().is_empty());

        app.records[0] = record(1, "history\n\n".repeat(20));
        let bar = redraw(&mut terminal, &mut app);
        assert!(bar.starts_with('│') && bar.ends_with('█'), "{bar:?}");
        app.tools.page(&mut app.scroll, true);
        let bar = redraw(&mut terminal, &mut app);
        assert!(bar.contains('█') && bar.ends_with('│'), "{bar:?}");

        let area = app.transcript_area;
        let mouse = |kind, column, row| MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        };
        assert!(
            app.transcript_mouse(mouse(MouseEventKind::Down(Left), area.right(), area.top()))
                .redraw
        );
        app.transcript_mouse(mouse(MouseEventKind::Up(Left), area.right(), area.top()));
        assert_eq!(app.scroll, Some(0));
        assert!(redraw(&mut terminal, &mut app).starts_with("██"));

        // Grab inside the thumb, then drag down even if the pointer leaves the track.
        let press = app.transcript_mouse(mouse(
            MouseEventKind::Down(Left),
            area.right(),
            area.top() + 1,
        ));
        assert!(!press.redraw);
        assert_eq!(app.scroll, Some(0));
        let drag = app.transcript_mouse(mouse(MouseEventKind::Drag(Left), area.x, area.bottom()));
        assert!(drag.redraw && !drag.selection_released);
        let release = app.transcript_mouse(mouse(MouseEventKind::Up(Left), area.x, area.bottom()));
        assert!(!release.selection_released && app.selection.copy_text().is_none());
        assert_eq!(app.scroll, None);
        assert!(redraw(&mut terminal, &mut app).ends_with('█'));

        app.records.push(record(2, "new live output".into()));
        redraw(&mut terminal, &mut app);
        let buffer = terminal.backend().buffer();
        let last: String = (area.left()..area.right())
            .map(|x| buffer[(x, area.bottom() - 1)].symbol())
            .collect();
        assert_eq!(last.trim_end(), "new live output");
    }

    #[tokio::test]
    async fn composer_header_keeps_its_title_and_pending_count_clear_of_slash_completion() {
        let directory = crate::test_support::private_tempdir().unwrap();
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
            session: host.store.create_session().await.unwrap().id,
            input: "/st".into(),
            ..App::default()
        };
        host.control_queue(&app.session, QueueAction::Pause)
            .await
            .unwrap();
        for pending in [0, 2] {
            for _ in 0..pending {
                host.submit(
                    &app.session,
                    "pending test",
                    &Uuid::new_v4().to_string(),
                    ApprovalMode::Interactive,
                )
                .await
                .unwrap();
            }
            app.state(&host).await;
            for (width, height) in [(1, 1), (11, 6), (16, 12), (24, 12), (120, 36)] {
                let mut terminal =
                    Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
                terminal
                    .draw(|frame| draw(frame, &mut app, &host, Instant::now()))
                    .unwrap();
                let buffer = terminal.backend().buffer();
                let menu_height = app.completion.menu(&app.input).unwrap().height();
                let [_, suggestions, draft] = content_rows(buffer.area, menu_height);
                let row = |y| -> String { (0..width).map(|x| buffer[(x, y)].symbol()).collect() };
                if draft.height > 0 {
                    let chrome = row(draft.top());
                    assert!(chrome.starts_with("┌ Message "), "{chrome}");
                    if pending == 0 || width == 11 {
                        assert!(
                            chrome.ends_with("─┐") || chrome == "┌ Message ┐",
                            "{chrome}"
                        );
                        assert!(!chrome.contains("pending"), "{chrome}");
                    } else if width == 16 {
                        assert!(chrome.ends_with(" 2 ┐"), "{chrome}");
                    } else {
                        assert!(chrome.ends_with(" 2 pending ┐"), "{chrome}");
                    }
                    assert!(
                        !chrome.contains("Enter") && !chrome.contains("PAUSED"),
                        "{chrome}"
                    );
                    assert!(row(draft.top() + 1).contains("/st"));
                }
                if suggestions.height > 1 {
                    assert!(row(suggestions.top() + 1).contains("/steer"));
                }
            }
        }
        assert!(app.completion_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.input, "/steer ");
        assert_eq!(host.queue(&app.session).await.unwrap().entries.len(), 2);
        host.shutdown().await;
    }

    #[tokio::test]
    async fn session_picker_preserves_draft_and_active_work_and_opens_exact_id() {
        let directory = crate::test_support::private_tempdir().unwrap();
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
        let first = host.store.create_session().await.unwrap();
        let second = host.store.create_session().await.unwrap();
        assert_eq!(first.title, second.title);
        let run = host
            .start(
                &first.id,
                "demo hold",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Interactive,
            )
            .await
            .unwrap();
        let mut app = App {
            session: first.id.clone(),
            input: "unsent draft".into(),
            scroll: Some(13),
            ..App::default()
        };
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        app.state(&host).await;
        let now = Instant::now();
        app.caret.focus(true, now);
        app.open_picker(&host).await.unwrap(); // F2
        assert!(!app.caret_visible(now));
        app.picker_key(&host, key(KeyCode::Char('x')))
            .await
            .unwrap();
        app.picker_key(&host, key(KeyCode::Enter)).await.unwrap(); // no match
        assert!(app.picker.is_some());
        assert_eq!(app.session, first.id);
        app.picker_key(&host, key(KeyCode::Esc)).await.unwrap();
        assert!(app.picker.is_none());
        assert_eq!(app.input, "unsent draft");
        assert_eq!(app.scroll, Some(13));
        assert!(host.active().await.iter().any(|active| active.id == run.id));

        for (draft, suggestion, hint) in [
            ("/", "› /help", "Tab/Enter fill"),
            ("/se", "› /sessions", "Tab/Enter fill"),
            ("/unknown", "", "No matching commands"),
        ] {
            app.input = draft.into();
            app.completion.edited(&app.input);
            for (width, height) in [(42, 12), (120, 32)] {
                let mut terminal =
                    Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
                terminal
                    .draw(|frame| draw(frame, &mut app, &host, Instant::now()))
                    .unwrap();
                let buffer = terminal.backend().buffer();
                let row = |y| -> String { (0..width).map(|x| buffer[(x, y)].symbol()).collect() };
                assert!(row(height - 6).contains(draft));
                assert!(
                    buffer[(1 + draft.len() as u16, height - 6)]
                        .modifier
                        .contains(Modifier::REVERSED)
                ); // slash suggestions keep composer focus
                let status = row(height - 2);
                assert!(status.contains("Esc"), "{width} columns: {status}");
                let menu_rows = 0..height - 7;
                assert!(menu_rows.clone().any(|y| row(y).contains(hint)));
                if !suggestion.is_empty() {
                    assert!(menu_rows.clone().any(|y| row(y).contains(suggestion)));
                }
            }
        }

        app.input = "/se".into();
        app.completion.edited(&app.input);
        assert!(app.completion_key(key(KeyCode::Esc)));
        assert_eq!(app.input, "/se");
        assert!(app.completion.menu(&app.input).is_none());
        assert!(!app.completion_key(key(KeyCode::Tab)));
        assert!(host.active().await.iter().any(|active| active.id == run.id));
        app.input.pop();
        app.completion.edited(&app.input);
        app.input.push('e');
        app.completion.edited(&app.input);
        assert!(app.completion_key(key(KeyCode::Tab)));
        assert_eq!(app.input, "/sessions ");
        assert!(app.picker.is_none());
        assert!(host.queue(&app.session).await.unwrap().entries.is_empty());
        assert!(!app.completion_key(key(KeyCode::Enter)));
        assert!(!app.completion_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::CONTROL)));
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Queue).await);
        assert_eq!(app.input, "/sessions ");
        assert!(app.picker.is_none());
        assert!(!submit_from_enter(&mut app, &host, SubmissionKey::Enter).await);
        assert!(app.input.is_empty());
        app.picker
            .as_mut()
            .unwrap()
            .paste(&second.id.to_uppercase());
        app.picker_key(&host, key(KeyCode::Enter)).await.unwrap();
        assert_eq!(app.session, second.id);
        assert!(app.picker.is_none());
        assert_eq!(host.store.sessions().await.unwrap().len(), 2);
        assert!(host.active().await.iter().any(|active| active.id == run.id));

        app.input = format!("/resume {}", &first.id[..12]);
        assert!(submit(&mut app, &host, SubmissionKey::Enter).await.is_err());
        assert_eq!(app.session, second.id);
        app.input = format!("/resume {}", first.id);
        assert!(!app.completion_key(key(KeyCode::Enter)));
        assert!(!submit(&mut app, &host, SubmissionKey::Enter).await.unwrap());
        assert_eq!(app.session, first.id);
        assert!(app.picker.is_none());

        app.input = "/resume".into();
        assert!(!submit(&mut app, &host, SubmissionKey::Enter).await.unwrap());
        app.picker_key(&host, key(KeyCode::Enter)).await.unwrap(); // explicit New row
        assert_ne!(app.session, first.id);
        assert_ne!(app.session, second.id);
        assert_eq!(host.store.sessions().await.unwrap().len(), 3);
        host.shutdown().await;
    }

    #[tokio::test]
    async fn root_ui_inspects_children_and_approves_only_the_actual_child_call() {
        let directory = crate::test_support::private_tempdir().unwrap();
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
            records: Vec::new(),
            after: 0,
            input: "/se".into(),
            status: String::new(),
            active: None,
            approval: None,
            preview_scroll: 0,
            cursor: composer::Cursor::default(),
            approval_since: approval::Since::default(),
            approval_fits: true,
            tasks: Vec::new(),
            task_picker: false,
            task_index: 0,
            task_detail: false,
            task_records: Vec::new(),
            task_after: 0,
            queue: QueueState::default(),
            queue_picker: false,
            queue_index: 0,
            ..App::default()
        };
        app.state(&host).await;
        let now = Instant::now();
        app.caret.focus(true, now);
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        assert_eq!(app.tasks[0].run_id, child_id);
        assert_eq!(app.tasks[0].status, ngn_protocol::RunStatus::Running);
        assert!(!app.agents_key(
            KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL),
            120
        ));
        app.open_tasks(false);
        for code in [KeyCode::Tab, KeyCode::Esc] {
            assert!(!app.completion_key(key(code)));
        }
        app.task_picker = false;
        assert!(!app.caret_visible(now)); // approval alone suppresses the caret
        for code in [KeyCode::Tab, KeyCode::Esc] {
            assert!(!app.completion_key(key(code))); // approval alone still takes priority
        }
        app.task_picker = true;
        assert_eq!(app.input, "/se");
        let approval = app.approval.as_ref().unwrap();
        assert_eq!(approval.run_id, child_id);
        assert_eq!(approval.agent_title, "Preview child");
        assert_eq!(approval.depth, 1);
        let child_run = host
            .active()
            .await
            .into_iter()
            .find(|run| run.id == child_id)
            .unwrap();
        host.emit(
            &child_run,
            ngn_protocol::Event::ToolCompleted {
                result: ngn_protocol::ToolResult {
                    call_id: "child-failure".into(),
                    name: "read_file".into(),
                    result: serde_json::Value::Null,
                    error: "child fixture failed: \x1b[2Jmissing file".into(),
                },
                duration_ms: 7,
            },
        )
        .await
        .unwrap();
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
        app.task_key(&host, key(KeyCode::Enter)).await;
        app.catch_up(&host).await.unwrap();
        app.state(&host).await;
        assert_eq!(app.tasks.len(), 1);
        assert_eq!(app.tasks[app.task_index].run_id, child_id);
        assert_eq!(app.tasks[0].status, ngn_protocol::RunStatus::Completed);
        assert!(app.task_records.iter().all(
            |record| record.session_id == app.tasks[0].session_id && record.run_id == child_id
        ));
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
        terminal
            .draw(|frame| draw(frame, &mut app, &host, Instant::now()))
            .unwrap();
        let contents: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(contents.contains("Agent: Preview child"));
        assert!(contents.contains("read_file · failed · 7ms"));
        assert!(contents.contains("Error: child fixture failed: [2Jmissing file"));
        assert!(contents.contains("Error: Tool approval denied, expired or cancelled"));
        assert!(!contents.contains('\x1b'));
        assert!(!contents.contains("Arguments:"));
        assert!(!contents.contains("Result:"));
        assert!(!directory.path().join("demo.txt").exists());
        host.shutdown().await;
    }
}
