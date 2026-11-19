use std::collections::HashMap;

use ngn_protocol::{Event, EventRecord, RunStatus, ToolCall, ToolResult};

/// Compact, bounded units; the wire continues to carry integer milliseconds.
pub fn duration(milliseconds: u64) -> String {
    if milliseconds < 1000 {
        return format!("{milliseconds}ms");
    }
    let (scale, divisor) = if milliseconds < 10_000 {
        (100, 10)
    } else {
        (10, 100)
    };
    let rounded = milliseconds / divisor + u64::from(milliseconds % divisor >= divisor / 2);
    if rounded < 60 * scale {
        let whole = rounded / scale;
        let remainder = rounded % scale;
        if remainder == 0 {
            return format!("{whole}s");
        }
        let precision = if scale == 100 { 2 } else { 1 };
        let fraction = format!("{remainder:0precision$}")
            .trim_end_matches('0')
            .to_owned();
        return format!("{whole}.{fraction}s");
    }
    let seconds = milliseconds / 1000 + u64::from(milliseconds % 1000 >= 500);
    for (size, unit, smaller, smaller_unit) in [
        (86_400, "d", 3600, "h"),
        (3600, "h", 60, "m"),
        (60, "m", 1, "s"),
    ] {
        if seconds < size {
            continue;
        }
        let major = seconds / size;
        let minor = (seconds % size) / smaller;
        let compact = format!("{major}{unit}");
        let text = if minor == 0 {
            compact.clone()
        } else {
            format!("{compact} {minor}{smaller_unit}")
        };
        if text.len() <= 8 {
            return text;
        }
        return if compact.len() <= 8 {
            compact
        } else {
            ">999999d".into()
        };
    }
    "1m".into()
}

pub fn target(call: &ToolCall) -> String {
    let key = match call.name.as_str() {
        "shell" => "command",
        "skill" => "name",
        "read_file" | "list_files" | "write" | "edit" => "path",
        "search" => "query",
        _ => return String::new(),
    };
    let text = call.arguments[key].as_str().unwrap_or_default();
    let text = plain(text).split_whitespace().collect::<Vec<_>>().join(" ");
    let hint: String = text.chars().take(96).collect();
    if hint.is_empty() {
        String::new()
    } else {
        format!(
            " · {hint}{}",
            if text.chars().count() > 96 { "…" } else { "" }
        )
    }
}

/// Never let remote/provider/tool data emit terminal control sequences.
pub fn plain(text: &str) -> String {
    text.chars()
        .filter(|character| {
            matches!(character, '\n' | '\t')
                || (!character.is_control() && !matches!(*character as u32, 0x7f..=0x9f))
        })
        .collect()
}

enum ToolState<'a> {
    Running {
        since_ms: u64,
    },
    Waiting {
        resume_ms: Option<u64>,
    },
    Denied,
    Completed {
        result: &'a ToolResult,
        elapsed_ms: u64,
        denied: bool,
    },
}

struct ToolRow<'a> {
    index: usize,
    call: Option<&'a ToolCall>,
    state: ToolState<'a>,
    active_ms: u64,
}

pub type ToolKey<'a> = (&'a str, &'a str);

pub struct ToolCard<'a> {
    pub key: ToolKey<'a>,
    pub label: String,
    pub status: &'static str,
    pub elapsed: Option<u64>,
    call: Option<&'a ToolCall>,
    result: Option<&'a ToolResult>,
}

impl ToolCard<'_> {
    pub fn details(&self) -> impl Iterator<Item = String> {
        use ngn_core::tools::bounded;

        let arguments = self.call.map(|call| {
            format!(
                "  Arguments: {}",
                bounded(&call.arguments.to_string(), 4096)
            )
        });
        let result = self
            .result
            .filter(|result| !result.result.is_null())
            .map(|result| format!("  Result: {}", bounded(&result.result.to_string(), 4096)));
        let error = self
            .result
            .filter(|result| !result.error.is_empty())
            .map(|result| format!("  Error: {}", bounded(&result.error, 4096)));
        [arguments, result, error]
            .into_iter()
            .flatten()
            .map(|text| plain(&text))
    }
}

impl<'a> ToolRow<'a> {
    fn new(index: usize, call: Option<&'a ToolCall>, since_ms: u64) -> Self {
        Self {
            index,
            call,
            state: ToolState::Running { since_ms },
            active_ms: 0,
        }
    }

    fn card(&self, key: ToolKey<'a>, now_ms: u64, finished: Option<RunStatus>) -> ToolCard<'a> {
        let (status, elapsed, result) = match self.state {
            ToolState::Completed {
                result,
                elapsed_ms,
                denied,
            } => (
                if denied {
                    "denied"
                } else if result.error.is_empty() {
                    "complete"
                } else {
                    "failed"
                },
                Some(elapsed_ms),
                Some(result),
            ),
            ToolState::Denied => ("denied", Some(self.active_ms), None),
            _ if finished.is_some() => (
                match finished {
                    Some(RunStatus::Cancelled) => "cancelled",
                    Some(RunStatus::Failed) => "failed",
                    _ => "interrupted",
                },
                None,
                None,
            ),
            ToolState::Waiting { .. } => ("Approval waiting", Some(self.active_ms), None),
            ToolState::Running { since_ms } => (
                "running",
                Some(
                    self.active_ms
                        .saturating_add(now_ms.saturating_sub(since_ms)),
                ),
                None,
            ),
        };
        let (name, hint) = self.call.map_or_else(
            || {
                (
                    result.map_or("", |result| result.name.as_str()),
                    String::new(),
                )
            },
            |call| (call.name.as_str(), target(call)),
        );
        let name = plain(name);
        ToolCard {
            key,
            label: format!(
                "{}{hint}",
                name.split_whitespace().collect::<Vec<_>>().join(" ")
            ),
            status,
            elapsed,
            call: self.call,
            result,
        }
    }
}

/// Project each tool into the position of its first retained event, not a new
/// transcript row at every transition. The durable event history is unchanged.
struct ProjectedTool<'a> {
    key: ToolKey<'a>,
    row: ToolRow<'a>,
    finished: Option<RunStatus>,
}

fn tool_rows(records: &[EventRecord]) -> HashMap<usize, ProjectedTool<'_>> {
    let mut calls = HashMap::new();
    let mut approvals = HashMap::new();
    let mut finished = HashMap::new();
    for (index, record) in records.iter().enumerate() {
        let run = record.run_id.as_str();
        let timestamp = record.timestamp_ms;
        match &record.event {
            Event::ToolStarted { call } => {
                calls
                    .entry((run, call.call_id.as_str()))
                    .or_insert_with(|| ToolRow::new(index, Some(call), timestamp));
            }
            Event::ApprovalRequested {
                approval_id, call, ..
            } => {
                let key = (run, call.call_id.as_str());
                approvals.insert((run, approval_id.as_str()), key);
                let row = calls
                    .entry(key)
                    .or_insert_with(|| ToolRow::new(index, Some(call), timestamp));
                if let ToolState::Running { since_ms } = row.state {
                    row.active_ms = row
                        .active_ms
                        .saturating_add(timestamp.saturating_sub(since_ms));
                    row.state = ToolState::Waiting { resume_ms: None };
                }
            }
            Event::ToolApprovalTiming {
                call_id,
                elapsed_ms,
                waiting,
            } => {
                if let Some(row) = calls.get_mut(&(run, call_id.as_str()))
                    && matches!(
                        row.state,
                        ToolState::Running { .. } | ToolState::Waiting { .. }
                    )
                {
                    row.active_ms = *elapsed_ms;
                    // Resume timing is emitted even on denial. Let the exact
                    // approval decision, not this clock sample, release the row.
                    if *waiting {
                        row.state = ToolState::Waiting { resume_ms: None };
                    } else if let ToolState::Waiting { resume_ms } = &mut row.state {
                        *resume_ms = Some(timestamp);
                    }
                }
            }
            Event::ApprovalResolved {
                approval_id,
                allowed,
            } => {
                if let Some(key) = approvals.remove(&(run, approval_id.as_str()))
                    && let Some(row) = calls.get_mut(&key)
                    && let ToolState::Waiting { resume_ms } = row.state
                {
                    row.state = if *allowed {
                        ToolState::Running {
                            since_ms: resume_ms.unwrap_or(timestamp),
                        }
                    } else {
                        ToolState::Denied
                    };
                }
            }
            Event::ToolCompleted {
                result,
                duration_ms,
            } => {
                let row = calls
                    .entry((run, result.call_id.as_str()))
                    .or_insert_with(|| ToolRow::new(index, None, timestamp));
                if !matches!(row.state, ToolState::Completed { .. }) {
                    row.state = ToolState::Completed {
                        result,
                        elapsed_ms: *duration_ms,
                        denied: matches!(row.state, ToolState::Denied),
                    };
                }
            }
            Event::RunFinished { status, .. } => {
                finished.insert(run, *status);
            }
            _ => (),
        }
    }
    calls
        .into_iter()
        .map(|(key, row)| {
            (
                row.index,
                ProjectedTool {
                    key,
                    row,
                    finished: finished.get(key.0).copied(),
                },
            )
        })
        .collect()
}

pub enum TranscriptPart<'a> {
    Text(String),
    Card(ToolCard<'a>),
}

pub fn transcript(records: &[EventRecord], now_ms: u64) -> Vec<TranscriptPart<'_>> {
    let mut output = String::new();
    let mut cards = Vec::new();
    let mut streamed = false;
    let rows = tool_rows(records);
    for (index, record) in records.iter().enumerate() {
        if let Some(tool) = rows.get(&index) {
            output.push('\n');
            cards.push((output.len(), tool));
            output.push('\n'); // One logical line, replaced by the card below.
        }
        match &record.event {
            Event::QueueUpdated { paused } => output.push_str(if *paused {
                "\n[Input queue paused · resume explicitly]\n"
            } else {
                "\n[Input queue resumed]\n"
            }),
            Event::RunStarted { prompt } => {
                output.push_str(&format!("\nYOU\n{prompt}\n\nNGN\n"));
                streamed = false;
            }
            Event::TextDelta { text } => {
                output.push_str(text);
                streamed = true;
            }
            Event::TextCompleted { text } => {
                if !streamed {
                    output.push_str(text);
                }
                output.push('\n');
                streamed = false;
            }
            Event::ToolStarted { .. } => streamed = false,
            Event::Notice { text } => output.push_str(&format!("\n{text}\n")),
            Event::TaskStarted { task } => output.push_str(&format!(
                "\n  ↳ Agent {} [{}] started · depth {}{}\n",
                task.title,
                task.run_id,
                task.depth,
                if task.read_only { " · read only" } else { "" }
            )),
            Event::TaskFinished { task, error } => output.push_str(&format!(
                "\n  ↳ Agent {} [{}]: {:?}{}\n",
                task.title,
                task.run_id,
                task.status,
                if error.is_empty() {
                    String::new()
                } else {
                    format!(": {error}")
                }
            )),
            Event::RunFinished { status, error } => output.push_str(&format!(
                "\n— {status:?}{}\n",
                if error.is_empty() {
                    String::new()
                } else {
                    format!(": {error}")
                }
            )),
            _ => (),
        }
    }
    // Keep the most recent activity visible in long conversations, not their
    // first half-megabyte. Durable history remains available in the event store.
    let mut start = output.len().saturating_sub(512_000);
    while !output.is_char_boundary(start) {
        start += 1;
    }
    let mut parts = Vec::new();
    for (offset, tool) in cards {
        if offset < start {
            continue;
        }
        if start < offset {
            parts.push(TranscriptPart::Text(plain(&output[start..offset])));
        }
        parts.push(TranscriptPart::Card(tool.row.card(
            tool.key,
            now_ms,
            tool.finished,
        )));
        start = offset + 1;
    }
    if start < output.len() {
        parts.push(TranscriptPart::Text(plain(&output[start..])));
    }
    parts
}

#[cfg(test)]
mod tests {
    use super::{transcript as project, *};

    fn transcript(records: &[EventRecord], details: bool, now_ms: u64) -> String {
        use std::fmt::Write as _;

        let mut output = String::new();
        for part in project(records, now_ms) {
            match part {
                TranscriptPart::Text(text) => output.push_str(&text),
                TranscriptPart::Card(card) => {
                    let _ = write!(output, "> {} · {}", card.label, card.status);
                    if let Some(elapsed) = card.elapsed {
                        let active = if card.status == "Approval waiting" {
                            "active "
                        } else {
                            ""
                        };
                        let _ = write!(output, " · {active}{}", duration(elapsed));
                    }
                    output.push('\n');
                    if details {
                        for line in card.details() {
                            let _ = writeln!(output, "{line}");
                        }
                    }
                }
            }
        }
        output
    }

    fn event(run: &str, timestamp_ms: u64, event: Event) -> EventRecord {
        EventRecord {
            schema_version: 1,
            sequence: timestamp_ms as i64,
            run_id: run.into(),
            session_id: "ngn-test".into(),
            timestamp_ms,
            event,
        }
    }

    fn call(id: &str, command: &str) -> ToolCall {
        ToolCall {
            call_id: id.into(),
            name: "shell".into(),
            arguments: serde_json::json!({"command": command}),
        }
    }

    fn timing(call_id: &str, elapsed_ms: u64, waiting: bool) -> Event {
        Event::ToolApprovalTiming {
            call_id: call_id.into(),
            elapsed_ms,
            waiting,
        }
    }

    fn request(call: &ToolCall, approval_id: &str) -> Event {
        Event::ApprovalRequested {
            approval_id: approval_id.into(),
            call: call.clone(),
            preview: "shell preview".into(),
        }
    }

    fn decision(approval_id: &str, allowed: bool) -> Event {
        Event::ApprovalResolved {
            approval_id: approval_id.into(),
            allowed,
        }
    }

    fn complete(call: &ToolCall, duration_ms: u64, error: &str) -> Event {
        Event::ToolCompleted {
            result: ToolResult {
                call_id: call.call_id.clone(),
                name: call.name.clone(),
                result: if error.is_empty() {
                    serde_json::json!({"stdout": "tool output"})
                } else {
                    serde_json::Value::Null
                },
                error: error.into(),
            },
            duration_ms,
        }
    }

    #[test]
    fn approval_freezes_only_its_call_and_running_reuses_the_same_row() {
        let first = call("shared", "first");
        let other = call("other", "other");
        let second_run = call("shared", "second-run");
        let mut records = vec![
            event(
                "r1",
                1000,
                Event::ToolStarted {
                    call: first.clone(),
                },
            ),
            event("r1", 1001, request(&first, "approval")),
            event("r1", 1002, timing("shared", 8, true)),
            event("r1", 1003, Event::ToolStarted { call: other }),
            // Even a reused approval ID must not switch the other run.
            event("r2", 1004, request(&second_run, "approval")),
            event("r2", 1005, timing("shared", 12, true)),
        ];
        let waiting = transcript(&records, false, 9100);
        assert_eq!(
            waiting,
            "\n> shell · first · Approval waiting · active 8ms\n\
             \n> shell · other · running · 8.1s\n\
             \n> shell · second-run · Approval waiting · active 12ms\n"
        );
        records.push(event("r1", 9100, timing("shared", 8, false)));
        assert_eq!(transcript(&records, false, 9100), waiting);
        records.push(event("r1", 9101, decision("approval", true)));
        let running = transcript(&records, false, 9300);
        assert!(running.starts_with("\n> shell · first · running · 208ms\n"));
        assert!(running.ends_with("second-run · Approval waiting · active 12ms\n"));
        assert!(transcript(&records, false, 10342).contains("first · running · 1.25s"));
        records.push(event("r1", 14112, complete(&first, 5012, "")));
        let terminal = transcript(&records, false, 17000);
        assert!(terminal.starts_with("\n> shell · first · complete · 5.01s\n"));
        assert_eq!(terminal.matches("> shell · first").count(), 1);
        assert_eq!(terminal.lines().count(), running.lines().count());
        assert!(!terminal.contains("tool output"));
        assert!(transcript(&records, true, 17000).starts_with(
            "\n> shell · first · complete · 5.01s\n  Arguments: {\"command\":\"first\"}\n  Result: {\"stdout\":\"tool output\"}\n"
        ));
    }

    #[test]
    fn denial_does_not_run_even_between_clock_resume_and_completion() {
        let call = call("call", "denial");
        let mut records = vec![
            event("r1", 0, request(&call, "approval")),
            event("r1", 8, timing("call", 8, true)),
            event("r1", 5000, timing("call", 8, false)),
        ];
        assert!(transcript(&records, false, 5001).contains("Approval waiting · active 8ms"));
        records.push(event("r1", 5001, decision("approval", false)));
        assert_eq!(
            transcript(&records, false, 6000),
            "\n> shell · denial · denied · 8ms\n"
        );
        records.push(event(
            "r1",
            5002,
            complete(&call, 10, "Tool approval denied"),
        ));
        let denied = "\n> shell · denial · denied · 10ms\n";
        assert_eq!(transcript(&records, false, 6000), denied);
        assert_eq!(transcript(&records, false, 60000), denied);
        assert!(transcript(&records, true, 60000).contains("Error: Tool approval denied"));
    }

    #[test]
    fn unfinished_tools_stop_at_their_run_boundary() {
        let running = call("a", "running");
        let waiting = call("b", "waiting");
        for (status, label) in [
            (RunStatus::Cancelled, "cancelled"),
            (RunStatus::Failed, "failed"),
            (RunStatus::Interrupted, "interrupted"),
            (RunStatus::Completed, "interrupted"),
        ] {
            let records = [
                event(
                    "r1",
                    1,
                    Event::ToolStarted {
                        call: running.clone(),
                    },
                ),
                event("r1", 2, request(&waiting, "approval")),
                event(
                    "r2",
                    3,
                    Event::ToolStarted {
                        call: call("a", "survivor"),
                    },
                ),
                event(
                    "r1",
                    4,
                    Event::RunFinished {
                        status,
                        error: String::new(),
                    },
                ),
            ];
            let output = transcript(&records, false, 10000);
            assert!(output.contains(&format!("shell · running · {label}\n")));
            assert!(output.contains(&format!("shell · waiting · {label}\n")));
            assert!(output.contains("shell · survivor · running · 10s\n"));
            assert!(!output.contains("Approval waiting"));
        }
    }

    #[test]
    fn failed_completion_is_rendered_even_without_a_retained_start() {
        let call = call("a", "lost");
        let records = [event("r1", 1, complete(&call, 1220, "execution failed"))];
        assert_eq!(
            transcript(&records, false, 10000),
            "\n> shell · failed · 1.22s\n"
        );
        assert!(transcript(&records, true, 10000).contains("Error: execution failed"));
    }

    #[test]
    fn control_sequences_are_removed() {
        assert_eq!(plain("hello\x1b[2J\x00\u{9f}\nworld"), "hello[2J\nworld");
    }

    #[test]
    fn durations_are_compact_adjacent_units_in_a_bounded_slot() {
        for (milliseconds, expected) in [
            (0, "0ms"),
            (999, "999ms"),
            (1000, "1s"),
            (1250, "1.25s"),
            (9990, "9.99s"),
            (10000, "10s"),
            (12340, "12.3s"),
            (59900, "59.9s"),
            (59960, "1m"),
            (60000, "1m"),
            (61400, "1m 1s"),
            (61600, "1m 2s"),
            (3599600, "1h"),
            (3661000, "1h 1m"),
            (86399600, "1d"),
            (90061000, "1d 1h"),
            (86400000 * 9999 + 3600000 * 23, "9999d"),
            (u64::MAX, ">999999d"),
        ] {
            assert_eq!(duration(milliseconds), expected);
            assert!(duration(milliseconds).len() <= 8);
        }
    }

    #[test]
    fn recognized_targets_are_plain_and_do_not_expose_arbitrary_fields() {
        let call = ToolCall {
            call_id: "c".into(),
            name: "skill".into(),
            arguments: serde_json::json!({"name":"ngn-\ncustomize\u{0}"}),
        };
        assert_eq!(target(&call), " · ngn- customize");
        let call = ToolCall {
            name: "custom".into(),
            ..call
        };
        assert!(target(&call).is_empty());
        let call = ToolCall {
            name: "shell".into(),
            arguments: serde_json::json!({"command":"x".repeat(200)}),
            ..call
        };
        assert_eq!(target(&call).chars().count(), 100);
    }

    #[test]
    fn long_transcripts_keep_the_latest_activity() {
        let record = EventRecord {
            schema_version: 1,
            sequence: 1,
            run_id: "run-test".into(),
            session_id: "ngn-test".into(),
            timestamp_ms: 0,
            event: Event::TextCompleted {
                text: format!("{}LATEST", "🦀".repeat(150_000)),
            },
        };
        let output = transcript(&[record], false, 0);
        assert!(output.ends_with("LATEST\n"));
        assert!(output.len() <= 512_000);
    }
}
