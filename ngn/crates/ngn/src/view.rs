use std::{collections::HashMap, iter::Peekable, ops::Range};

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
    plain_chars(text).collect()
}

fn plain_chars(text: &str) -> impl Iterator<Item = char> {
    text.chars().filter(|character| {
        matches!(character, '\n' | '\t')
            || (!character.is_control() && !matches!(*character as u32, 0x7f..=0x9f))
    })
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
        transition: Option<i64>,
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
    pub success: bool,
    pub success_transition: Option<i64>,
    call: Option<&'a ToolCall>,
    result: Option<&'a ToolResult>,
}

impl ToolCard<'_> {
    pub fn error_detail(&self) -> Option<String> {
        self.result
            .filter(|result| !result.error.is_empty())
            .map(|result| {
                let error = ngn_core::tools::bounded(&result.error, 4096);
                format!("  Error: {}", plain(&error))
            })
    }

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
        [arguments, result]
            .into_iter()
            .flatten()
            .map(|text| plain(&text))
            .chain(self.error_detail())
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
                ..
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
        // A native shell can finish normally with a nonzero exit status.
        // Keep the existing transcript text, but don't give it success ink.
        let success = status == "complete"
            && result.is_some_and(|result| {
                result.name != "shell"
                    || (result.result["success"].as_bool() != Some(false)
                        && result.result["timed_out"].as_bool() != Some(true)
                        && result.result["exit_code"]
                            .as_i64()
                            .is_none_or(|code| code == 0))
            });
        let success_transition = match self.state {
            ToolState::Completed { transition, .. } if success => transition,
            _ => None,
        };
        ToolCard {
            key,
            label: format!(
                "{}{hint}",
                name.split_whitespace().collect::<Vec<_>>().join(" ")
            ),
            status,
            elapsed,
            success,
            success_transition,
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
                let key = (run, result.call_id.as_str());
                // An orphaned result can still render its settled style, but
                // only the first result of a started call can trigger motion.
                let transition = (!finished.contains_key(run)
                    && calls.get(&key).is_some_and(|row| {
                        matches!(records[row.index].event, Event::ToolStarted { .. })
                            && matches!(row.state, ToolState::Running { .. })
                    }))
                .then_some(record.sequence);
                let row = calls
                    .entry(key)
                    .or_insert_with(|| ToolRow::new(index, None, timestamp));
                if !matches!(row.state, ToolState::Completed { .. }) {
                    row.state = ToolState::Completed {
                        result,
                        elapsed_ms: *duration_ms,
                        denied: matches!(row.state, ToolState::Denied),
                        transition,
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

// Session-scoped first fragment event sequence and fence ordinal. Neither
// the global output byte position nor the rendered row survives projection.
pub type FenceKey = (i64, usize);

pub enum TranscriptPart<'a> {
    Text(String),
    User(String),
    Code(FenceKey, String),
    Card(ToolCard<'a>),
}

/// Recognize only top-level fence lines, before sanitization so that removing
/// a control character cannot turn a non-delimiter into a delimiter.
fn fence_line(line: &str) -> Option<(usize, u8, usize, &str)> {
    let trimmed = line.trim_start_matches(' ');
    let indent = line.len() - trimmed.len();
    if indent > 3 {
        return None;
    }
    let marker = *trimmed.as_bytes().first()?;
    if !matches!(marker, b'`' | b'~') {
        return None;
    }
    let length = trimmed.bytes().take_while(|byte| *byte == marker).count();
    (length >= 3).then(|| (indent, marker, length, &trimmed[length..]))
}

struct Fence {
    source: Range<usize>,
    body: Range<usize>,
    indent: usize,
}

/// Return each raw Markdown line and the byte length of its ending. Keep
/// offsets into the original text; normalize carriage returns only in code.
fn fence_lines(text: &str) -> impl Iterator<Item = (&str, usize)> {
    let mut lines = text.split_inclusive(['\r', '\n']).peekable();
    std::iter::from_fn(move || {
        let raw = lines.next()?;
        Some(if let Some(line) = raw.strip_suffix('\r') {
            (line, 1 + usize::from(lines.next_if_eq(&"\n").is_some()))
        } else if let Some(line) = raw.strip_suffix('\n') {
            (line, 1)
        } else {
            (raw, 0)
        })
    })
}

fn fences(text: &str) -> impl Iterator<Item = Fence> {
    let mut offset = 0;
    let mut lines = fence_lines(text);
    std::iter::from_fn(move || {
        let mut open = None;
        for (line, ending) in lines.by_ref() {
            let start = offset;
            offset += line.len() + ending;
            if let Some((indent, marker, length, rest)) = fence_line(line) {
                if let Some((fence_start, body_start, indent, delimiter, minimum)) = open {
                    if marker == delimiter
                        && length >= minimum
                        && rest.trim_matches([' ', '\t']).is_empty()
                    {
                        return Some(Fence {
                            source: fence_start..offset,
                            body: body_start..start,
                            indent,
                        });
                    }
                } else if marker == b'~' || !rest.contains('`') {
                    open = Some((start, offset, indent, marker, length));
                }
            }
        }
        open.map(|(start, body, indent, _, _)| Fence {
            source: start..offset,
            body: body..offset,
            indent,
        })
    })
}

fn fence_code(raw: &str, indent: usize, partial_first_line: bool) -> String {
    let mut code = String::with_capacity(raw.len());
    for (index, (mut line, ending)) in fence_lines(raw).enumerate() {
        if !partial_first_line || index != 0 {
            let spaces = line
                .bytes()
                .take(indent)
                .take_while(|byte| *byte == b' ')
                .count();
            line = &line[spaces..];
            if spaces < indent
                && let Some(rest) = line.strip_prefix('\t')
            {
                // Markdown's indentation tab stops are four columns wide.
                code.push_str(&"    "[..4 - indent]);
                line = rest;
            }
        }
        code.extend(plain_chars(line));
        if ending != 0 {
            code.push('\n');
        }
    }
    code
}

fn text_parts(
    output: &str,
    mut start: usize,
    end: usize,
    assistant: &mut Peekable<impl Iterator<Item = (i64, Range<usize>)>>,
    parts: &mut Vec<TranscriptPart<'_>>,
) {
    while let Some((sequence, message)) = assistant.next_if(|(_, range)| range.start < end) {
        if message.end <= start {
            continue;
        }
        // Scan from the start of an overlapping message, not the display
        // cutoff: it may have dropped the opener but retained the code.
        for (ordinal, fence) in fences(&output[message.clone()]).enumerate() {
            let fence_start = message.start + fence.source.start;
            let fence_end = message.start + fence.source.end;
            if fence_end <= start {
                continue;
            }
            if start < fence_start {
                parts.push(TranscriptPart::Text(plain(&output[start..fence_start])));
            }
            let body_end = message.start + fence.body.end;
            if start < body_end || start <= fence_start {
                let mut body_start = (message.start + fence.body.start).max(start);
                if body_start < body_end
                    && output[..body_start].ends_with('\r')
                    && output.as_bytes().get(body_start) == Some(&b'\n')
                {
                    // The byte cutoff fell between the two halves of CRLF.
                    body_start += 1;
                }
                let partial = body_start > message.start + fence.body.start
                    && !output[..body_start].ends_with(['\r', '\n']);
                parts.push(TranscriptPart::Code(
                    (sequence, ordinal),
                    fence_code(&output[body_start..body_end], fence.indent, partial),
                ));
            }
            start = fence_end;
            if start < end
                && !output[..start].ends_with(['\r', '\n'])
                && output.as_bytes()[start] == b'\n'
            {
                // A separator supplied by the next event terminates the
                // last fence line; it isn't an extra empty transcript row.
                start += 1;
            }
        }
    }
    if start < end {
        parts.push(TranscriptPart::Text(plain(&output[start..end])));
    }
}

fn assistant_boundary(record: &EventRecord, new_card: bool) -> bool {
    new_card
        || matches!(
            record.event,
            Event::RunStarted { .. } | Event::ToolStarted { .. } | Event::RunFinished { .. }
        )
}

/// A final snapshot belongs to the open response in this run, never to a
/// previous tool round or to another run. Apply it before assigning byte
/// offsets so cards and any interleaved events keep their positions.
fn assistant_edits<'a>(
    records: &'a [EventRecord],
    tools: &HashMap<usize, ProjectedTool<'_>>,
) -> Vec<Option<(&'a str, bool)>> {
    let mut edits = vec![None; records.len()];
    let mut pending = HashMap::<_, Vec<usize>>::new();
    for (index, record) in records.iter().enumerate() {
        let run = (record.session_id.as_str(), record.run_id.as_str());
        if assistant_boundary(record, tools.contains_key(&index)) {
            pending.remove(&run);
        }
        match &record.event {
            Event::TextDelta { text } if !text.is_empty() => {
                pending.entry(run).or_default().push(index);
            }
            Event::TextCompleted { text } => {
                let Some(deltas) = pending.remove(&run) else {
                    continue;
                };
                edits[index] = Some(("", false));
                let mut rest = text.as_str();
                let mut last_visible = None;
                for (position, &delta) in deltas.iter().enumerate() {
                    let Event::TextDelta { text: original } = &records[delta].event else {
                        unreachable!("only text delta indices are recorded");
                    };
                    let replacement = if text.is_empty() {
                        // As in Python, an empty final does not erase the stream.
                        original
                    } else {
                        // Preserve the earlier visual fragments' character
                        // lengths if a notice or another run split the stream.
                        // The last delta takes any remaining snapshot text.
                        let split = if rest.is_empty() || position + 1 == deltas.len() {
                            rest.len()
                        } else if rest.starts_with(original.as_str()) {
                            original.len()
                        } else {
                            rest.char_indices()
                                .nth(original.chars().count())
                                .map_or(rest.len(), |(offset, _)| offset)
                        };
                        let (fragment, tail) = rest.split_at(split);
                        rest = tail;
                        fragment
                    };
                    edits[delta] = Some((replacement, false));
                    if !replacement.is_empty() {
                        last_visible = Some((delta, replacement));
                    }
                }
                if let Some((delta, replacement)) = last_visible {
                    edits[delta] = Some((replacement, true));
                }
            }
            _ => {}
        }
    }
    edits
}

/// How a finished turn reads in the conversation. Plain words, not enum names.
fn outcome_label(status: RunStatus) -> &'static str {
    match status {
        RunStatus::Completed => "✓ Done",
        RunStatus::Failed => "✗ Failed",
        RunStatus::Cancelled => "⊘ Stopped",
        RunStatus::Interrupted => "⚠ Interrupted before the turn finished",
        _ => "· Ended",
    }
}

pub enum TranscriptScope {
    Main,
    AgentInspector,
}

enum TranscriptBoundary<'projection, 'event> {
    User { end: usize },
    Card(&'projection ProjectedTool<'event>),
}

pub fn transcript(
    records: &[EventRecord],
    now_ms: u64,
    scope: TranscriptScope,
) -> Vec<TranscriptPart<'_>> {
    let mut output = String::new();
    let mut boundaries = Vec::new();
    let mut assistant: Vec<(i64, Range<usize>)> = Vec::new();
    let mut continuing = None;
    let rows = tool_rows(records);
    let edits = assistant_edits(records, &rows);
    for (index, record) in records.iter().enumerate() {
        let run = (record.session_id.as_str(), record.run_id.as_str());
        let tool = rows.get(&index);
        if continuing == Some(run) && assistant_boundary(record, tool.is_some()) {
            continuing = None;
        }
        let before_card = output.len();
        if let Some(tool) = tool {
            output.push('\n');
            boundaries.push((output.len(), TranscriptBoundary::Card(tool)));
            output.push('\n'); // One logical line, replaced by the card below.
        }
        let start = output.len();
        let assistant_text = edits[index].or(match &record.event {
            Event::TextDelta { text } => Some((text.as_str(), false)),
            Event::TextCompleted { text } => Some((text.as_str(), true)),
            _ => None,
        });
        if let Some((text, complete)) = assistant_text
            && !text.is_empty()
        {
            output.push_str(text);
            if complete {
                output.push('\n');
            }
            if before_card == start
                && continuing == Some(run)
                && let Some((_, message)) = assistant.last_mut()
            {
                message.end = output.len();
            } else {
                assistant.push((record.sequence, start..output.len()));
            }
            continuing = (!complete).then_some(run);
            continue;
        }
        match &record.event {
            Event::QueueUpdated { paused } => output.push_str(if *paused {
                "\n[Input queue paused · resume explicitly]\n"
            } else {
                "\n[Input queue resumed]\n"
            }),
            Event::RunStarted { prompt } => {
                // Preserve the existing separators, but mark only the prompt
                // rows (including explicit blank lines within the prompt).
                output.push('\n');
                let user_start = output.len();
                output.push_str(prompt);
                output.push('\n');
                boundaries.push((user_start, TranscriptBoundary::User { end: output.len() }));
                output.push('\n');
            }
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
            Event::RunFinished {
                status: RunStatus::Completed,
                error,
            } if matches!(scope, TranscriptScope::Main) && error.is_empty() => {
                // End any unfinalized prose line without adding a status row.
                if !output.is_empty() && !output.ends_with('\n') {
                    output.push('\n');
                }
            }
            Event::RunFinished { status, error } => output.push_str(&format!(
                "\n{}{}\n",
                outcome_label(*status),
                if error.is_empty() {
                    String::new()
                } else {
                    format!(": {error}")
                }
            )),
            _ => (),
        }
        if before_card < output.len() {
            continuing = None;
        }
    }
    // Keep the most recent activity visible in long conversations, not their
    // first half-megabyte. Durable history remains available in the event store.
    let mut start = output.len().saturating_sub(512_000);
    while !output.is_char_boundary(start) {
        start += 1;
    }
    let mut parts = Vec::new();
    let mut assistant = assistant.into_iter().peekable();
    for (offset, part) in boundaries {
        let end = match part {
            TranscriptBoundary::User { end } => end,
            TranscriptBoundary::Card(_) => offset + 1,
        };
        if end <= start {
            continue;
        }
        if start < offset {
            text_parts(&output, start, offset, &mut assistant, &mut parts);
        }
        parts.push(match part {
            TranscriptBoundary::User { .. } => {
                TranscriptPart::User(plain(&output[start.max(offset)..end]))
            }
            TranscriptBoundary::Card(tool) => {
                TranscriptPart::Card(tool.row.card(tool.key, now_ms, tool.finished))
            }
        });
        start = end;
    }
    text_parts(&output, start, output.len(), &mut assistant, &mut parts);
    parts
}

#[cfg(test)]
mod tests {
    use super::*;

    fn project(records: &[EventRecord], now_ms: u64) -> Vec<TranscriptPart<'_>> {
        super::transcript(records, now_ms, TranscriptScope::Main)
    }

    fn transcript(records: &[EventRecord], details: bool, now_ms: u64) -> String {
        use std::fmt::Write as _;

        let mut output = String::new();
        for part in project(records, now_ms) {
            match part {
                TranscriptPart::Text(text)
                | TranscriptPart::User(text)
                | TranscriptPart::Code(_, text) => output.push_str(&text),
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
    fn fences_are_assistant_only_and_survive_split_deltas() {
        let records = [
            event(
                "r",
                1,
                Event::RunStarted {
                    prompt: "```user\nplain\n```".into(),
                },
            ),
            event(
                "r",
                2,
                Event::TextDelta {
                    text: "# literal heading\n``".into(),
                },
            ),
            event(
                "r",
                3,
                Event::TextDelta {
                    text: "``rust\r\n  **literal**\x1b[31m\r\n```\r\n".into(),
                },
            ),
            event(
                "r",
                4,
                Event::TextDelta {
                    text: "`````\t\r\n[plain](https://example.test)".into(),
                },
            ),
            event(
                "r",
                5,
                Event::TextCompleted {
                    text: concat!(
                        "# literal heading\n````rust\r\n",
                        "  **literal**\x1b[31m\r\n```\r\n",
                        "`````\t\r\n[plain](https://example.test)"
                    )
                    .into(),
                },
            ),
            event(
                "r",
                6,
                Event::ToolStarted {
                    call: call("c", "~~~tool"),
                },
            ),
            event("r", 7, complete(&call("c", ""), 1, "~~~\ntool error\n~~~")),
        ];
        let pending = project(&records[..3], 10);
        assert!(matches!(&pending[..], [
            TranscriptPart::Text(gap),
            TranscriptPart::User(user),
            TranscriptPart::Text(prose),
            TranscriptPart::Code(_, code),
        ] if gap == "\n" && user == "```user\nplain\n```\n" && prose == "\n# literal heading\n"
            && code == "  **literal**[31m\n```\n"));

        let parts = project(&records, 10);
        assert!(matches!(&parts[..], [
            TranscriptPart::Text(_),
            TranscriptPart::User(_),
            TranscriptPart::Text(_),
            TranscriptPart::Code(_, code),
            TranscriptPart::Text(prose),
            TranscriptPart::Card(card),
        ] if code == "  **literal**[31m\n```\n"
            && prose == "[plain](https://example.test)\n\n"
            && card.error_detail().as_deref() == Some("  Error: ~~~\ntool error\n~~~")));
    }

    #[test]
    fn fence_grammar_and_event_boundaries_leave_other_text_plain() {
        let records = [
            event("r", 1, Event::TextCompleted {
                text: "inline ```not a block\n    ```indented\n```bad`info\n\x1b```\n   ~~~lang`info\n  keep indent\n```\n~~~not-a-close\n   ~~~~ \t\noutside".into(),
            }),
            event("r", 2, Event::TextDelta { text: "```\nunfinished".into() }),
            event("r", 3, Event::Notice { text: "~~~notice\nnot code".into() }),
            event("r", 4, Event::TextCompleted { text: String::new() }),
            event("r", 5, Event::TextCompleted { text: "new assistant text".into() }),
            event("next", 6, Event::RunStarted { prompt: "~~~user".into() }),
        ];
        let parts = project(&records, 10);
        assert!(matches!(&parts[..], [
            TranscriptPart::Text(before),
            TranscriptPart::Code(_, tilde),
            TranscriptPart::Text(between),
            TranscriptPart::Code(_, unfinished),
            TranscriptPart::Text(after),
            TranscriptPart::User(user),
            TranscriptPart::Text(gap),
        ] if before == "inline ```not a block\n    ```indented\n```bad`info\n```\n"
            && tilde == "keep indent\n```\n~~~not-a-close\n"
            && between == "outside\n"
            && unfinished == "unfinished\n"
            && after == "\n~~~notice\nnot code\nnew assistant text\n\n"
            && user == "~~~user\n" && gap == "\n"));
    }

    #[test]
    fn only_the_main_transcript_omits_successful_turn_markers() {
        let finished = |run, at, status, error: &str| {
            event(
                run,
                at,
                Event::RunFinished {
                    status,
                    error: error.into(),
                },
            )
        };
        let records = [
            event(
                "first",
                1,
                Event::TextDelta {
                    text: "Answer without a closing snapshot".into(),
                },
            ),
            finished("first", 2, RunStatus::Completed, ""),
            event(
                "second",
                3,
                Event::RunStarted {
                    prompt: "Next prompt".into(),
                },
            ),
            finished("second", 4, RunStatus::Failed, "try a different input"),
            finished("third", 5, RunStatus::Interrupted, ""),
            finished("fourth", 6, RunStatus::Cancelled, ""),
        ];

        assert_eq!(
            transcript(&records, false, 0),
            concat!(
                "Answer without a closing snapshot\n\nNext prompt\n\n",
                "\n✗ Failed: try a different input\n",
                "\n⚠ Interrupted before the turn finished\n\n⊘ Stopped\n",
            ),
        );
        assert!(
            matches!(&super::transcript(&records[..2], 0, TranscriptScope::AgentInspector)[..],
                [TranscriptPart::Text(text)] if text.ends_with("\n✓ Done\n"))
        );
    }

    #[test]
    fn final_snapshot_edits_and_closes_the_exact_tilde_stream() {
        let delta =
            |sequence, text: &str| event("r", sequence, Event::TextDelta { text: text.into() });
        let records = [
            delta(1, "PROSE-TILDE\n~~"),
            delta(2, "~~\nOPEN-WAIT 界界\n~~~\n```\n"),
            delta(3, "STREAM-OLD 界\n"),
            delta(4, "~~"),
            delta(5, ""),
            event("r", 6, Event::TextCompleted {
                text: "PROSE-TILDE\n~~~~\nOPEN-WAIT 界界\n~~~\n```\nFINAL-NEW 界\n~~~~~\nPOST-CLOSE".into(),
            }),
            event("r", 7, Event::RunFinished { status: RunStatus::Completed, error: String::new() }),
        ];
        assert!(
            matches!(&project(&records[..1], 0)[..], [TranscriptPart::Text(text)]
            if text == "PROSE-TILDE\n~~")
        );
        assert!(
            matches!(&project(&records[..2], 0)[..], [TranscriptPart::Text(_), TranscriptPart::Code(_, text)]
            if text == "OPEN-WAIT 界界\n~~~\n```\n")
        );
        assert!(
            matches!(&project(&records[..5], 0)[..], [TranscriptPart::Text(_), TranscriptPart::Code(_, text)]
            if text.ends_with("STREAM-OLD 界\n~~"))
        );
        assert!(matches!(&project(&records, 0)[..], [
            TranscriptPart::Text(prose), TranscriptPart::Code(_, code), TranscriptPart::Text(after),
        ] if prose == "PROSE-TILDE\n" && code == "OPEN-WAIT 界界\n~~~\n```\nFINAL-NEW 界\n"
            && after == "POST-CLOSE\n"));
    }

    #[test]
    fn completions_keep_tool_rounds_other_runs_and_empty_finals_separate() {
        let delta = |run, sequence, text: &str| {
            event(run, sequence, Event::TextDelta { text: text.into() })
        };
        let done = |run, sequence, text: &str| {
            event(run, sequence, Event::TextCompleted { text: text.into() })
        };
        let first = call("first", "one");
        let second = call("second", "two");
        let records = [
            delta("r", 1, "draft before tool"),
            done("r", 2, "final before tool"),
            event(
                "r",
                3,
                Event::ToolStarted {
                    call: first.clone(),
                },
            ),
            delta("r", 4, "no final before next tool"),
            event(
                "r",
                5,
                Event::ToolStarted {
                    call: second.clone(),
                },
            ),
            event("r", 6, complete(&first, 1000, "")),
            delta("r", 7, "前 old\n"),
            event(
                "other",
                8,
                Event::Notice {
                    text: "interleaved notice".into(),
                },
            ),
            delta("r", 9, "old end"),
            delta("other", 10, "other stream"),
            done("r", 11, "界 new\nnew end"),
            done("other", 12, ""),
            event("r", 13, complete(&second, 1000, "")),
            done("r", 14, ""),
            done("r", 15, "standalone"),
        ];
        assert_eq!(
            transcript(&records, false, 0),
            "final before tool\n\n> shell · one · complete · 1s\n\
             no final before next tool\n> shell · two · complete · 1s\n\
             界 new\n\ninterleaved notice\nnew end\nother stream\nstandalone\n"
        );
    }

    #[test]
    fn code_dedents_by_the_opener_and_normalizes_cr_without_changing_plain_text() {
        let records = [
            event("r", 1, Event::RunStarted { prompt: "user\rliteral".into() }),
            event("r", 2, Event::TextCompleted {
                text: "plain\rprose\n   ~~~~\r   SAME\r    EXTRA\r\n  FEWER\n\tTAB\n   \tFULL-TAB\n   \x1b[999~\r   ~~~~~ \t\rPOST\rPLAIN".into(),
            }),
        ];
        assert!(matches!(&project(&records, 0)[..], [
            TranscriptPart::Text(gap), TranscriptPart::User(user),
            TranscriptPart::Text(before), TranscriptPart::Code(_, code), TranscriptPart::Text(after),
        ] if gap == "\n" && user == "userliteral\n" && before == "\nplainprose\n"
            && code == "SAME\n EXTRA\nFEWER\n TAB\n\tFULL-TAB\n[999~\n"
            && after == "POSTPLAIN\n"));
    }

    #[test]
    fn retained_tail_can_start_inside_a_user_prompt() {
        let records = [
            event(
                "r",
                1,
                Event::RunStarted {
                    prompt: "界".repeat(11),
                },
            ),
            event(
                "r",
                2,
                Event::TextCompleted {
                    text: "x".repeat(511_989),
                },
            ),
        ];
        assert!(matches!(&project(&records, 0)[..], [
            TranscriptPart::User(prompt), TranscriptPart::Text(prose),
        ] if prompt == "界界\n" && prose.starts_with("\nx")));
    }

    #[test]
    fn snapshot_replacement_applies_before_the_retained_tail_cutoff() {
        let records = [
            event(
                "r",
                1,
                Event::TextCompleted {
                    text: "earlier reply".into(),
                },
            ),
            event(
                "r",
                2,
                Event::TextDelta {
                    text: format!("~~~\r{}", "界".repeat(180_000)),
                },
            ),
            event(
                "r",
                3,
                Event::TextCompleted {
                    text: "~~~\rfinal code\r~~~\rplain".into(),
                },
            ),
        ];
        assert!(
            matches!(&project(&records[..2], 0)[..], [TranscriptPart::Code(_, code)]
            if code.starts_with('界') && code.len() <= 512_000)
        );
        assert!(matches!(&project(&records, 0)[..], [
            TranscriptPart::Text(before), TranscriptPart::Code(_, code), TranscriptPart::Text(after),
        ] if before == "earlier reply\n" && code == "final code\n" && after == "plain\n"));
    }

    #[test]
    fn retained_tail_keeps_code_when_the_opener_is_outside_the_byte_limit() {
        let records = [event(
            "r",
            1,
            Event::TextDelta {
                text: format!(
                    "~~~\n{}\nlast code line\n~~~\nplain tail",
                    "界".repeat(180_000)
                ),
            },
        )];
        let parts = project(&records, 10);
        assert!(
            matches!(&parts[..], [TranscriptPart::Code(_, code), TranscriptPart::Text(tail)]
            if code.starts_with('界') && code.ends_with("\nlast code line\n")
                && tail == "plain tail" && code.len() + tail.len() <= 512_000)
        );
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
