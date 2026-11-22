use std::{
    collections::BTreeSet,
    f64::consts::PI,
    time::{Duration, Instant},
};

use ngn_protocol::{Event, EventRecord, RunStatus};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
};

use crate::view;

use super::shimmer::Sweep;

// Textual's activity.py and app.py: the field has seven Braille cells and
// completion holds its own target for two seconds before returning to idle.
pub(super) const FRAME: Duration = Duration::from_nanos(1_000_000_000 / 30);
const DONE: Duration = Duration::from_secs(2);
const IDLE_SETTLE: Duration = Duration::from_millis(1500);
pub(super) const WIDTH: usize = 7;
const INFO: Color = Color::Cyan;
const READY: Color = Color::Green;
const WORKING: Color = Color::Magenta;
const ERROR: Color = Color::Red;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Phase {
    Idle,
    Working,
    Tool,
    Responding,
    Approval,
    Done,
    Cancelled,
    Error,
}

impl Phase {
    fn channels(self) -> [f64; 13] {
        match self {
            Self::Idle | Self::Cancelled => [
                126., 151., 171., 158., 179., 196., 132., 173., 177., 0.16, 0.45, 0., 0.08,
            ],
            Self::Working => [
                104., 175., 222., 160., 146., 228., 119., 204., 193., 1., 0.9, 0., 0.2,
            ],
            Self::Tool => [
                211., 155., 101., 229., 189., 119., 216., 149., 154., 1.5, 0.85, 0.65, 0.3,
            ],
            Self::Responding => [
                105., 185., 166., 132., 212., 181., 142., 197., 216., 0.85, 0.8, 0.2, 0.3,
            ],
            Self::Approval => [
                186., 155., 110., 221., 184., 112., 200., 158., 128., 0.17, 0.48, 0., 0.08,
            ],
            Self::Done => [
                115., 172., 145., 148., 204., 163., 132., 191., 180., 0.09, 0.28, 0., 0.03,
            ],
            Self::Error => [
                194., 126., 150., 224., 146., 144., 193., 141., 182., 0.3, 0.55, 0., 0.12,
            ],
        }
    }
}

#[derive(Clone, Copy)]
struct Spring {
    value: f64,
    target: f64,
    velocity: f64,
}

impl Spring {
    fn new(value: f64) -> Self {
        Self {
            value,
            target: value,
            velocity: 0.,
        }
    }
}

pub(super) struct Field {
    phase: f64,
    springs: [Spring; 13],
}

impl Field {
    pub(super) fn new() -> Self {
        Self {
            phase: 0.,
            springs: Phase::Idle.channels().map(Spring::new),
        }
    }

    pub(super) fn advance(&mut self, seconds: f64) {
        // Exact critically damped spring, including the integral of the speed
        // channel so interrupting a transition doesn't jump the particle phase.
        const F: f64 = 6.5;
        let decay = (-F * seconds).exp();
        for (index, spring) in self.springs.iter_mut().enumerate() {
            let distance = spring.value - spring.target;
            let correction = spring.velocity + F * distance;
            if index == 9 {
                self.phase += spring.target * seconds
                    + distance * (1. - decay) / F
                    + correction * ((1. - decay) / F.powi(2) - seconds * decay / F);
            }
            spring.value = spring.target + (distance + correction * seconds) * decay;
            spring.velocity = (spring.velocity - F * correction * seconds) * decay;
        }
    }

    pub(super) fn select(&mut self, phase: Phase, immediate: bool) {
        for (spring, target) in self.springs.iter_mut().zip(phase.channels()) {
            if immediate {
                *spring = Spring::new(target);
            } else {
                spring.target = target;
            }
        }
    }

    pub(super) fn spans(&self) -> impl Iterator<Item = Span<'static>> {
        const BITS: [[u8; 2]; 4] = [[1, 8], [2, 16], [4, 32], [64, 128]];
        let [reach, flow, wave] = [10, 11, 12].map(|idx| self.springs[idx].value);
        let mut cells = [0; WIDTH];
        for particle in (0..12).map(f64::from) {
            let angle = self.phase * (0.7 + particle * 0.017) + particle * 2.39996;
            let travel = (angle * 0.73 + particle * 0.35).sin() * flow * 0.2;
            let x = 0.5 + reach * 0.44 * angle.sin() + travel;
            let y = 0.5
                + reach * 0.42 * (angle * 1.37 + particle).cos()
                + wave * 0.1 * (angle * 2.).sin();
            let x = (x.clamp(0., 1.) * (WIDTH * 2 - 1) as f64).round_ties_even() as usize;
            let y = (y.clamp(0., 1.) * 3.).round_ties_even() as usize;
            cells[x / 2] |= BITS[y][x % 2];
        }
        let stops: [u8; 9] = std::array::from_fn(|i| channel(self.springs[i].value));
        cells.into_iter().enumerate().map(move |(column, bits)| {
            let amount = 1. + (column as f64 / (WIDTH - 1) as f64 * PI + self.phase * 0.35).sin();
            let segment = (amount as usize).min(1);
            let mix = amount - segment as f64;
            let [r, g, b] = [0, 1, 2].map(|i| {
                let start = f64::from(stops[segment * 3 + i]);
                let end = f64::from(stops[(segment + 1) * 3 + i]);
                channel(start + (end - start) * mix)
            });
            let symbol = char::from_u32(0x2800 + u32::from(bits)).expect("Braille cell");
            Span::styled(symbol.to_string(), Style::new().fg(Color::Rgb(r, g, b)))
        })
    }
}

fn channel(value: f64) -> u8 {
    value.round_ties_even().clamp(0., 255.) as u8
}

// Anchor durable event timestamps once, then count elapsed time monotonically.
struct Clock {
    observed: Instant,
    age: Duration,
}

impl Clock {
    fn new(now: Instant, wall_ms: u64, timestamp_ms: u64) -> Self {
        Self {
            observed: now,
            age: Duration::from_millis(wall_ms.saturating_sub(timestamp_ms)),
        }
    }

    fn age(&self, now: Instant) -> Duration {
        self.age + now.saturating_duration_since(self.observed)
    }
}

pub(super) struct Activity {
    animated: bool,
    field: Field,
    phase: Phase,
    phase_since: Instant,
    frame_at: Instant,
    seen: i64,
    run: Option<(String, Clock)>,
    tool: Option<ActiveTool>,
    responding: bool,
    outcome: Option<(RunStatus, String, Instant)>,
}

struct ActiveTool {
    sequence: i64,
    call_id: String,
    name: String,
    clock: Clock,
}

impl Default for Activity {
    fn default() -> Self {
        Self::new(Instant::now(), true)
    }
}

impl Activity {
    pub(super) fn animated(&self) -> bool {
        self.animated
    }

    pub(super) fn new(now: Instant, animated: bool) -> Self {
        Self {
            animated,
            field: Field::new(),
            phase: Phase::Idle,
            phase_since: now,
            frame_at: now,
            seen: 0,
            run: None,
            tool: None,
            responding: false,
            outcome: None,
        }
    }

    pub(super) fn reset(&mut self, after: i64, now: Instant) {
        *self = Self::new(now, self.animated);
        self.seen = after; // Don't animate historical completions on session open.
    }

    pub(super) fn sync(
        &mut self,
        active: Option<&str>,
        approval: bool,
        failed: bool,
        records: &[EventRecord],
        now: Instant,
        wall_ms: u64,
    ) {
        let same_run = self.run.as_ref().map(|(id, _)| id.as_str()) == active;
        let after = records.last().map_or(self.seen, |record| record.sequence);
        if same_run && after == self.seen {
            self.select(self.target(approval, failed, now), now);
            return;
        }
        self.advance(now);
        if let Some(id) = active {
            let mut completed = BTreeSet::new();
            let mut tool = None;
            let mut responding = false;
            let mut started = None;
            for record in records.iter().rev().filter(|record| record.run_id == id) {
                match &record.event {
                    Event::RunStarted { .. } => {
                        started = Some(record.timestamp_ms);
                        break;
                    }
                    Event::ToolCompleted { result, .. } => {
                        completed.insert(&result.call_id);
                    }
                    Event::ToolStarted { call }
                        if tool.is_none() && !completed.contains(&call.call_id) =>
                    {
                        tool = Some((record, call));
                    }
                    Event::TextDelta { text } if !text.is_empty() && completed.is_empty() => {
                        responding = true;
                    }
                    _ => (),
                }
            }
            if !same_run {
                self.run = Some((
                    id.into(),
                    Clock::new(now, wall_ms, started.unwrap_or(wall_ms)),
                ));
            }
            if self.tool.as_ref().map(|tool| tool.sequence)
                != tool.map(|(record, _)| record.sequence)
            {
                self.tool = tool.map(|(record, call)| ActiveTool {
                    sequence: record.sequence,
                    call_id: call.call_id.clone(),
                    name: inline(&call.name),
                    clock: Clock::new(now, wall_ms, record.timestamp_ms),
                });
            }
            self.responding = responding;
            self.outcome = None;
        } else {
            self.run = None;
            self.tool = None;
            if let Some((status, error)) = records
                .iter()
                .rev()
                .take_while(|record| record.sequence > self.seen)
                .find_map(|record| match &record.event {
                    Event::RunFinished { status, error } => Some((*status, error)),
                    _ => None,
                })
            {
                self.outcome = Some((status, error.clone(), now));
            }
        }
        self.seen = after;
        self.select(self.target(approval, failed, now), now);
    }

    fn target(&self, approval: bool, failed: bool, now: Instant) -> Phase {
        if failed {
            Phase::Error
        } else if approval {
            Phase::Approval
        } else if self.run.is_some() {
            if self.tool.is_some() {
                Phase::Tool
            } else if self.responding {
                Phase::Responding
            } else {
                Phase::Working
            }
        } else {
            match self.outcome.as_ref() {
                Some((RunStatus::Completed, _, at)) if now < *at + DONE => Phase::Done,
                Some((RunStatus::Cancelled, _, _)) => Phase::Cancelled,
                Some((RunStatus::Failed | RunStatus::Interrupted, _, _)) => Phase::Error,
                _ => Phase::Idle,
            }
        }
    }

    fn select(&mut self, phase: Phase, now: Instant) {
        if phase != self.phase {
            self.advance(now);
            self.phase = phase;
            self.phase_since = now;
            self.field.select(
                phase,
                !self.animated || matches!(phase, Phase::Cancelled | Phase::Error),
            );
        }
    }

    pub(super) fn advance(&mut self, now: Instant) {
        if self.phase == Phase::Done
            && let Some((_, _, at)) = self.outcome.as_ref()
            && now >= *at + DONE
        {
            let end = *at + DONE;
            self.advance_field(end);
            self.phase = Phase::Idle;
            self.phase_since = end;
            self.field.select(Phase::Idle, !self.animated);
        }
        self.advance_field(now);
    }

    fn advance_field(&mut self, now: Instant) {
        if self.animated
            && !matches!(self.phase, Phase::Cancelled | Phase::Error)
            && (self.phase != Phase::Idle || self.outcome.is_some())
        {
            let end = if self.phase == Phase::Idle {
                now.min(self.phase_since + IDLE_SETTLE)
            } else {
                now
            };
            self.field
                .advance(end.saturating_duration_since(self.frame_at).as_secs_f64());
        }
        self.frame_at = now;
    }

    pub(super) fn needs_frame(&self, now: Instant) -> bool {
        match self.phase {
            Phase::Done => self.animated, // The state poll also expires static completion frames.
            Phase::Idle => {
                self.animated && self.outcome.is_some() && now < self.phase_since + IDLE_SETTLE
            }
            Phase::Cancelled | Phase::Error => false,
            _ => self.animated,
        }
    }

    pub(super) fn shimmering_tool(&self) -> Option<view::ToolKey<'_>> {
        if self.animated && self.phase == Phase::Tool {
            let (run, _) = self.run.as_ref()?;
            let tool = self.tool.as_ref()?;
            Some((run, &tool.call_id))
        } else {
            None
        }
    }

    fn line(
        &self,
        now: Instant,
        approval: Option<&str>,
        failure: Option<&str>,
        note: &str,
    ) -> Line<'static> {
        let color = match self.phase {
            Phase::Working
            | Phase::Responding
            | Phase::Tool
            | Phase::Approval
            | Phase::Cancelled => WORKING,
            Phase::Idle | Phase::Done => READY,
            Phase::Error => ERROR,
        };
        let style = Style::new().fg(color);
        let mut spans: Vec<_> = self.field.spans().collect();
        spans.push(Span::styled(" assistant", style));
        if let Some(reason) = failure {
            spans.push(Span::styled(format!(" · {}", inline(reason)), style));
            return Line::from(spans);
        }
        match self.phase {
            Phase::Approval => spans.push(Span::styled(
                format!(" · Approval needed: {}", inline(approval.unwrap_or("tool"))),
                style,
            )),
            Phase::Tool => {
                if let Some(tool) = &self.tool {
                    let label =
                        Span::styled(format!("Running {}  /  Esc to cancel", tool.name), style);
                    spans.push(Span::styled(" · ", style));
                    if self.animated {
                        spans.extend(shimmer(&label, tool.clock.age(now)));
                    } else {
                        spans.push(label);
                    }
                }
            }
            Phase::Working | Phase::Responding => {
                let verb = if self.phase == Phase::Working {
                    "Working"
                } else {
                    "Responding"
                };
                spans.push(Span::styled(format!(" · {verb}  /  Esc to cancel"), style));
            }
            Phase::Cancelled => spans.push(Span::styled(" · Cancelled", style)),
            Phase::Error => {
                let reason = self
                    .outcome
                    .as_ref()
                    .map(|(_, error, _)| error.as_str())
                    .filter(|s| !s.is_empty());
                spans.push(Span::styled(
                    format!(" · {}", inline(reason.unwrap_or("Run failed"))),
                    style,
                ));
            }
            Phase::Idle | Phase::Done if !note.is_empty() => {
                spans.push(Span::styled(format!(" · {}", inline(note)), style));
            }
            Phase::Idle | Phase::Done => (),
        }
        if self.phase != Phase::Error
            && let Some((_, clock)) = &self.run
        {
            spans.push(Span::styled(
                format!(" · {}", elapsed(clock.age(now))),
                style,
            ));
            if !note.is_empty() && self.phase != Phase::Approval {
                spans.push(Span::styled(format!(" · {}", inline(note)), style));
            }
        }
        Line::from(spans)
    }
}

fn elapsed(age: Duration) -> String {
    let seconds = age.as_secs();
    let hours = seconds / 3600;
    let minutes = seconds / 60 % 60;
    let seconds = seconds % 60;
    if hours > 0 {
        format!("{hours}:{minutes:02}:{seconds:02}")
    } else {
        format!("{minutes:02}:{seconds:02}")
    }
}

fn inline(text: &str) -> String {
    view::plain(text).replace(['\n', '\t'], " ")
}

fn shimmer(label: &Span<'_>, age: Duration) -> Vec<Span<'static>> {
    let sweep = Sweep::new(label.content.chars().count(), age);
    let mut index = 0;
    label
        .styled_graphemes(Style::default())
        .map(|grapheme| {
            let color = sweep.color(index);
            index += grapheme.symbol.chars().count();
            Span::styled(grapheme.symbol.to_owned(), grapheme.style.fg(color))
        })
        .collect()
}

pub(super) fn fit(line: Line<'static>, width: usize) -> Line<'static> {
    if line.width() <= width {
        return line;
    }
    let Some(mut remaining) = width.checked_sub(1) else {
        return Line::default();
    };
    let mut spans = Vec::new();
    for grapheme in line.styled_graphemes(Style::default()) {
        let span = Span::styled(grapheme.symbol.to_owned(), grapheme.style);
        let Some(left) = remaining.checked_sub(span.width()) else {
            spans.push(Span::styled("…", grapheme.style));
            break;
        };
        remaining = left;
        spans.push(span);
    }
    Line::from(spans)
}

pub(super) fn failure_text(reason: &str) -> Line<'static> {
    // The transcript layout splits long logical lines without treating those
    // display breaks as line breaks in the copied text.
    Line::styled(inline(reason), Style::new().fg(ERROR))
}

impl Activity {
    pub(super) fn draw(
        &self,
        frame: &mut Frame,
        area: Rect,
        app: &super::App,
        config: &ngn_core::Config,
        now: Instant,
    ) {
        if area.is_empty() {
            return;
        }
        // One cell each from compose-area and footer (no painted background).
        let padding = 2.min(area.width.saturating_sub(1) / 2);
        let width = area.width - 2 * padding;
        let failure = app.failure.as_deref();
        let mut line = self.line(
            now,
            app.approval.as_ref().map(|a| a.call.name.as_str()),
            failure,
            &app.status,
        );
        if let Some(reason) = failure
            && line.width() > usize::from(width)
        {
            // Prefer the reason to the signature when even the start of a
            // rejected Enter would otherwise be obscured on a narrow terminal.
            let visible = if width < 30 {
                reason.strip_prefix("Not sent: ").unwrap_or(reason)
            } else {
                reason
            };
            line = Line::styled(inline(visible), Style::new().fg(ERROR));
        }
        let row = Rect::new(area.x + padding, area.y, width, 1);
        frame.render_widget(fit(line, usize::from(width)), row);
        if area.height > 1 {
            let provider_model = if let Ok(provider) = config.selected_provider() {
                Line::from(vec![
                    Span::styled(inline(&config.provider), Style::new().fg(INFO)),
                    Span::styled(" · ", Style::new().fg(INFO).add_modifier(Modifier::DIM)),
                    Span::styled(
                        inline(&provider.model),
                        Style::new().fg(INFO).add_modifier(Modifier::BOLD),
                    ),
                ])
            } else {
                Line::styled("Provider not configured", Style::new().fg(INFO))
            };
            frame.render_widget(
                fit(provider_model, usize::from(width)),
                Rect {
                    y: row.y + 1,
                    ..row
                },
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ngn_protocol::{ProviderConfig, ToolCall, ToolResult, VERSION};
    use ratatui::{Terminal, backend::TestBackend, buffer::Buffer};

    fn event(sequence: i64, run_id: &str, timestamp_ms: u64, event: Event) -> EventRecord {
        EventRecord {
            schema_version: VERSION,
            sequence,
            run_id: run_id.into(),
            session_id: "session".into(),
            timestamp_ms,
            event,
        }
    }

    fn field(activity: &Activity) -> (String, Option<Color>) {
        let mut spans = activity.field.spans();
        let first = spans.next().unwrap();
        let mut text = first.content.into_owned();
        text.extend(spans.map(|span| span.content.into_owned()));
        (text, first.style.fg)
    }

    #[test]
    fn working_done_idle_match_reference_frames_and_stop() {
        let t = Instant::now();
        let ms = Duration::from_millis;
        let mut activity = Activity::new(t, true);
        let mut records = vec![event(
            1,
            "root",
            10_000,
            Event::RunStarted {
                prompt: "go".into(),
            },
        )];
        let expect = |activity: &Activity, expected, rgb| {
            assert_eq!(field(activity), (String::from(expected), Some(rgb)));
        };
        // Reference samples from Python activity.StateField, starting at idle.
        expect(&activity, "⠀⠀⠖⠶⠲⠀⠀", Color::Rgb(158, 179, 196));
        activity.sync(Some("root"), false, false, &records, t, 10_000);
        assert!(activity.needs_frame(t));
        activity.advance(t + ms(200));
        expect(&activity, "⠀⠰⠐⠢⠶⠆⠀", Color::Rgb(158, 167, 207));
        activity.advance(t + ms(800));
        expect(&activity, "⠠⠡⠄⠅⡄⠶⠀", Color::Rgb(152, 158, 220));

        records.push(event(
            2,
            "root",
            10_800,
            Event::RunFinished {
                status: RunStatus::Completed,
                error: String::new(),
            },
        ));
        activity.sync(None, false, false, &records, t + ms(800), 10_800);
        assert_eq!(activity.phase, Phase::Done);
        let ready = activity.line(t + ms(800), None, None, "");
        assert_eq!(
            ready.to_string(),
            format!("{} assistant", field(&activity).0)
        );
        assert_eq!(ready.spans[WIDTH].style.fg, Some(READY)); // No synthetic usage
        activity.advance(t + ms(1200));
        expect(&activity, "⠀⠀⠴⠲⠴⠀⠀", Color::Rgb(145, 190, 181));
        activity.advance(t + ms(2799));
        assert_eq!(activity.phase, Phase::Done);
        activity.advance(t + ms(2800));
        assert_eq!(activity.phase, Phase::Idle);
        expect(&activity, "⠀⠀⠰⠶⠆⠀⠀", Color::Rgb(143, 200, 169));
        activity.advance(t + ms(3100));
        expect(&activity, "⠀⠀⠴⠴⠶⠀⠀", Color::Rgb(146, 186, 181));
        activity.advance(t + ms(4300));
        let settled = field(&activity);
        assert!(!activity.needs_frame(t + ms(4300)));
        activity.advance(t + ms(12_000));
        assert_eq!(field(&activity), settled);
        activity.reset(2, t);
        activity.sync(None, false, false, &records, t, 10_800);
        assert_eq!(activity.phase, Phase::Idle);
        assert!(!activity.needs_frame(t));
    }

    #[test]
    fn tool_shimmer_changes_only_foreground_and_ends_at_completion_or_approval() {
        let t = Instant::now();
        let sweep = t + Duration::from_millis(900);
        let call = ToolCall {
            call_id: "read".into(),
            name: "read_file".into(),
            arguments: serde_json::json!({}),
        };
        let mut records = vec![
            event(
                1,
                "root",
                1_000,
                Event::RunStarted {
                    prompt: "read".into(),
                },
            ),
            event(
                2,
                "root",
                366_000,
                Event::ToolStarted { call: call.clone() },
            ),
            event(
                3,
                "unrelated",
                366_000,
                Event::ToolStarted {
                    call: ToolCall {
                        name: "ignore_this".into(),
                        ..call.clone()
                    },
                },
            ),
        ];
        let mut activity = Activity::new(t, true);
        activity.sync(Some("root"), false, false, &records, t, 366_000);
        assert_eq!(activity.phase, Phase::Tool);
        let start = activity.line(t, None, None, "");
        let middle = activity.line(sweep, None, None, "");
        assert!(
            start
                .to_string()
                .ends_with("Running read_file  /  Esc to cancel · 06:05")
        );
        assert_eq!(start.to_string(), middle.to_string());
        let fg = |line: &Line<'_>, i: usize| line.spans[WIDTH + 2 + i].style.fg;
        assert_eq!(fg(&start, 19), Some(Color::Rgb(136, 136, 136)));
        assert_eq!(fg(&middle, 19), Some(Color::Rgb(152, 183, 204)));
        assert!(middle.spans.iter().all(|span| span.style.bg.is_none()));
        let wrapped = activity.line(t + Duration::from_millis(1800), None, None, "");
        let label_range = WIDTH + 2..start.spans.len() - 1;
        assert_eq!(start.spans[label_range.clone()], wrapped.spans[label_range]);

        activity.sync(Some("root"), true, false, &records, sweep, 366_900);
        let approval = activity.line(sweep, Some("write"), None, "");
        assert!(
            approval
                .to_string()
                .ends_with("Approval needed: write · 06:05")
        );
        assert_eq!(approval.spans[WIDTH].style.fg, Some(Color::Magenta));
        records.push(event(
            4,
            "root",
            366_900,
            Event::ToolCompleted {
                result: ToolResult {
                    call_id: call.call_id,
                    name: call.name,
                    result: serde_json::Value::Null,
                    error: String::new(),
                },
                duration_ms: 900,
            },
        ));
        activity.sync(Some("root"), false, false, &records, sweep, 366_900);
        assert!(
            activity
                .line(sweep, None, None, "")
                .to_string()
                .contains("Working")
        );
        assert!(activity.tool.is_none());
        records.push(event(
            5,
            "root",
            366_900,
            Event::RunFinished {
                status: RunStatus::Cancelled,
                error: String::new(),
            },
        ));
        activity.sync(None, false, false, &records, sweep, 366_900);
        let cancelled = activity.line(sweep, None, None, "");
        assert!(cancelled.to_string().ends_with("assistant · Cancelled"));
        assert_eq!(cancelled.spans[WIDTH].style.fg, Some(Color::Magenta));
        let later = t + Duration::from_secs(10);
        activity.advance(later);
        assert!(!activity.needs_frame(later));
        assert_eq!(cancelled, activity.line(later, None, None, ""));
        assert_eq!(elapsed(Duration::from_secs(3661)), "1:01:01");
    }

    #[test]
    fn styled_model_rows_and_reduced_motion_render_at_cell_width() {
        let t = Instant::now();
        let mut app = super::super::App {
            activity: Activity::new(t, false),
            ..Default::default()
        };
        let config = ngn_core::Config {
            provider: "work".into(),
            providers: [(
                "work".into(),
                ProviderConfig {
                    kind: "openai".into(),
                    model: "模型-test".into(),
                    api: String::new(),
                    base_url: String::new(),
                    api_key_env: "OPENAI_API_KEY".into(),
                    auth: ngn_protocol::ProviderAuth::ApiKey,
                },
            )]
            .into(),
            ..Default::default()
        };
        fn render(
            app: &super::super::App,
            config: &ngn_core::Config,
            now: Instant,
            width: u16,
        ) -> Buffer {
            let mut terminal = Terminal::new(TestBackend::new(width, 2)).unwrap();
            terminal
                .draw(|frame| app.activity.draw(frame, frame.area(), app, config, now))
                .unwrap();
            terminal.backend().buffer().clone()
        }
        let wide = render(&app, &config, t, 38);
        let text = |buffer: &Buffer, y| -> String {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect()
        };
        assert!(text(&wide, 0).starts_with("  ⠀⠀⠖⠶⠲⠀⠀ assistant"));
        assert_eq!(wide[(10, 0)].fg, Color::Green);
        assert!(text(&wide, 1).starts_with("  work · 模 型 -test"));
        assert_eq!(wide[(2, 1)].fg, Color::Cyan);
        assert!(wide[(7, 1)].modifier.contains(Modifier::DIM));
        assert!(wide[(9, 1)].modifier.contains(Modifier::BOLD));
        assert_eq!(wide[(9, 1)].bg, Color::Reset);
        let narrow = render(&app, &config, t, 14);
        assert_eq!(text(&narrow, 1), "  work · 模 …  ");
        assert!(!text(&wide, 0).contains("F2"));

        app.activity.sync(Some("root"), false, false, &[], t, 0);
        let before = field(&app.activity);
        let later = t + Duration::from_secs(3);
        app.activity.advance(later);
        assert_eq!(field(&app.activity), before);
        assert!(!app.activity.needs_frame(later));
        assert!(
            app.activity
                .line(later, None, None, "")
                .to_string()
                .ends_with("03")
        );
        let failure = [event(
            1,
            "root",
            3000,
            Event::RunFinished {
                status: RunStatus::Failed,
                error: "provider rejected the run".into(),
            },
        )];
        app.activity.sync(None, false, false, &failure, later, 3000);
        let reason = app.activity.line(later, None, None, "");
        assert!(
            reason
                .to_string()
                .ends_with("assistant · provider rejected the run")
        );
        assert_eq!(reason.spans[WIDTH].style.fg, Some(Color::Red));
        assert!(!app.activity.needs_frame(later));
    }
}
