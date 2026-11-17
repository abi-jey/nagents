use std::collections::HashMap;

use ngn_protocol::{Event, EventRecord, ToolCall};

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

pub fn transcript(records: &[EventRecord], details: bool) -> String {
    let mut output = String::new();
    let mut streamed = false;
    let mut calls = HashMap::new();
    for record in records {
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
            Event::ToolStarted { call } => {
                let hint = target(call);
                calls.insert((&record.run_id, &call.call_id), hint.clone());
                output.push_str(&format!("\n  ◇ {}{hint}\n", call.name));
                if details {
                    output.push_str(&ngn_core::tools::bounded(&call.arguments.to_string(), 4096));
                    output.push('\n');
                }
                streamed = false;
            }
            Event::ToolCompleted {
                result,
                duration_ms,
            } => {
                let hint = calls
                    .get(&(&record.run_id, &result.call_id))
                    .cloned()
                    .unwrap_or_default();
                if !result.error.is_empty() {
                    output.push_str(&format!(
                        "  ! {}{hint} · {}: {}\n",
                        result.name,
                        duration(*duration_ms),
                        result.error
                    ));
                } else {
                    output.push_str(&format!(
                        "  ✓ {}{hint} · {}\n",
                        result.name,
                        duration(*duration_ms)
                    ));
                }
                if details && !result.result.is_null() {
                    output.push_str(&ngn_core::tools::bounded(&result.result.to_string(), 4096));
                    output.push('\n');
                }
            }
            Event::ToolApprovalTiming {
                elapsed_ms,
                waiting: true,
                ..
            } => {
                output.push_str(&format!(
                    "  [Approval waiting · active {}]\n",
                    duration(*elapsed_ms)
                ));
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
    plain(&output[start..])
}

#[cfg(test)]
mod tests {
    use super::*;
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
        let output = transcript(&[record], false);
        assert!(output.ends_with("LATEST\n"));
        assert!(output.len() <= 512_000);
    }
}
