use ngn_protocol::{Event, EventRecord};

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
    for record in records {
        match &record.event {
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
                output.push_str(&format!("\n  ◇ {}\n", call.name));
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
                if !result.error.is_empty() {
                    output.push_str(&format!("  ! {}: {}\n", result.name, result.error));
                } else {
                    output.push_str(&format!("  ✓ {} · {duration_ms} ms\n", result.name));
                }
                if details && !result.result.is_null() {
                    output.push_str(&ngn_core::tools::bounded(&result.result.to_string(), 4096));
                    output.push('\n');
                }
            }
            Event::Notice { text } => output.push_str(&format!("\n{text}\n")),
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
