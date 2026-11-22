use std::io::{self, Write};

use base64::{Engine as _, engine::general_purpose::STANDARD};

pub(super) const MAX_COPY_BYTES: usize = 64 * 1024;

fn validate_text(text: &str) -> io::Result<()> {
    if text.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "selection is empty",
        ));
    }
    if text.len() > MAX_COPY_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "selection exceeds the 64 KiB copy limit",
        ));
    }
    Ok(())
}

fn osc52(text: &str) -> String {
    format!("\x1b]52;c;{}\x1b\\", STANDARD.encode(text))
}

/// Request a clipboard write. The terminal does not acknowledge acceptance.
pub(super) fn request_copy(text: &str) -> io::Result<()> {
    validate_text(text)?;
    let sequence = osc52(text);
    let mut output = io::stdout().lock();
    output.write_all(sequence.as_bytes())?;
    output.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_copy_limits_in_bytes_without_trimming() {
        assert_eq!(
            validate_text("").unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(validate_text(" \n").is_ok());
        let max = "é".repeat(MAX_COPY_BYTES / 2);
        assert_eq!(max.len(), MAX_COPY_BYTES);
        assert!(validate_text(&max).is_ok());
        assert_eq!(
            validate_text(&(max + "a")).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn osc52_targets_clipboard_uses_st_and_encodes_all_text() {
        assert_eq!(osc52("Hi"), "\x1b]52;c;SGk=\x1b\\");
        let text = "界\n\x1b]52;c;?\x07\x1b\\\0";
        let sequence = osc52(text);
        let payload = sequence
            .strip_prefix("\x1b]52;c;")
            .unwrap()
            .strip_suffix("\x1b\\")
            .unwrap();
        assert_eq!(STANDARD.decode(payload).unwrap(), text.as_bytes());
    }
}
