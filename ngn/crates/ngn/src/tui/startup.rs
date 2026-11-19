//! The POSIX entrance owns only its own normal-screen rows and stdin until
//! handoff. It does not enter the alternate screen or start a second host.

use std::{cell::Cell, future::Future, io, pin::Pin};

use anyhow::Result;

pub(super) fn shutdown_signal() -> io::Result<impl Future<Output = ()>> {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};

        // Install every handler before changing termios or hiding the cursor.
        // Keep these receivers through the alternate-screen session as well.
        let mut interrupt = signal(SignalKind::interrupt())?;
        let mut terminate = signal(SignalKind::terminate())?;
        let mut hangup = signal(SignalKind::hangup())?;
        let mut quit = signal(SignalKind::quit())?;
        Ok(async move {
            tokio::select! {
                _ = interrupt.recv() => (),
                _ = terminate.recv() => (),
                _ = hangup.recv() => (),
                _ = quit.recv() => (),
            }
        })
    }
    #[cfg(not(unix))]
    Ok(async {
        let _ = crate::shutdown_signal().await;
    })
}

pub(super) async fn prepare(
    animate: bool,
    initialize: impl Future<Output = Result<()>>,
    signal: Pin<&mut impl Future<Output = ()>>,
) -> Result<Option<String>> {
    #[cfg(unix)]
    let opening = posix::run(animate);
    #[cfg(not(unix))]
    let opening = async {
        let _ = animate;
        Ok::<_, io::Error>(Some(String::new()))
    };
    handoff(opening, initialize, signal).await
}

/// Drive initialization once, alongside the animation. Store work may already
/// be running on a blocking worker when cancelled; finish setup before the host
/// shuts down, but drop the prelude immediately to restore the terminal.
async fn handoff(
    opening: impl Future<Output = io::Result<Option<String>>>,
    initialize: impl Future<Output = Result<()>>,
    mut signal: Pin<&mut impl Future<Output = ()>>,
) -> Result<Option<String>> {
    let started = Cell::new(false);
    let init = async {
        started.set(true);
        initialize.await
    };
    tokio::pin!(init);
    let draft = {
        tokio::pin!(opening);
        tokio::select! {
            biased;
            _ = &mut signal => Ok(None),
            draft = &mut opening => draft,
            result = &mut init => {
                result?;
                return Ok(tokio::select! {
                    biased;
                    _ = &mut signal => None,
                    draft = &mut opening => draft?,
                });
            }
        }
    }; // cbreak/cursor guard has dropped, even if we must wait for the store.
    if matches!(draft, Ok(Some(_))) {
        tokio::select! {
            biased;
            _ = &mut signal => {
                if started.get() { let _ = init.await; }
                return Ok(None);
            }
            result = &mut init => result?,
        }
    } else if started.get() {
        let _ = init.await;
    }
    Ok(draft?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::future::pending;

    #[tokio::test]
    async fn signal_drops_prelude_before_waiting_for_inflight_initialization() {
        struct TerminalOwner<'a>(&'a Cell<bool>);
        impl Drop for TerminalOwner<'_> {
            fn drop(&mut self) {
                self.0.set(false);
            }
        }
        let active = Cell::new(false);
        let finished = Cell::new(false);
        let (notify, interrupted) = tokio::sync::oneshot::channel();
        let signal = async { interrupted.await.unwrap() };
        tokio::pin!(signal);
        let opening = async {
            let _owner = TerminalOwner(&active);
            active.set(true);
            pending().await
        };
        let initialize = async {
            assert!(active.get());
            notify.send(()).unwrap();
            tokio::task::yield_now().await;
            assert!(!active.get());
            finished.set(true);
            Ok(())
        };
        assert_eq!(handoff(opening, initialize, signal).await.unwrap(), None);
        assert!(finished.get());
    }

    #[tokio::test]
    async fn successful_handoff_waits_for_one_initialization() {
        let calls = Cell::new(0);
        let opening = async {
            tokio::task::yield_now().await;
            Ok(Some("draft".to_owned()))
        };
        let initialize = async {
            calls.set(calls.get() + 1);
            Ok(())
        };
        let draft = handoff(opening, initialize, std::pin::pin!(pending()))
            .await
            .unwrap();
        assert_eq!(draft.as_deref(), Some("draft"));
        assert_eq!(calls.get(), 1);
    }
}

#[cfg(unix)]
mod posix {
    use std::{
        io::{self, IsTerminal, Write},
        os::fd::AsRawFd,
        time::Duration,
    };

    use crossterm::terminal::size;
    use nix::{
        errno::Errno,
        sys::termios::{
            InputFlags, LocalFlags, OutputFlags, SetArg, SpecialCharacterIndices, Termios,
            tcgetattr, tcsetattr,
        },
        unistd::read,
    };
    use tokio::time::{Instant, sleep_until};

    const POLL: Duration = Duration::from_millis(10);
    const HIDE: &str = "\x1b[?25l";
    const SHOW: &str = "\x1b[?25h";
    const SAVE: &str = "\x1b7";
    const RESTORE: &str = "\x1b8";
    const LABELS: [&str; 3] = [
        concat!("ngn ", env!("CARGO_PKG_VERSION")),
        "Opening workspace...",
        "Enter / Space: skip   Esc / Ctrl+C / Ctrl+Q: exit",
    ];

    #[derive(Clone, Copy)]
    enum Frame {
        Start,
        Blank,
        Seal,
        Reveal(usize),
        Ready,
    }

    const FRAMES: [(u64, Frame); 8] = [
        (0, Frame::Start),
        (35, Frame::Blank),
        (70, Frame::Blank),
        (105, Frame::Seal),
        (150, Frame::Reveal(1)),
        (210, Frame::Reveal(2)),
        (270, Frame::Reveal(3)),
        (320, Frame::Ready),
    ];

    fn frame_width(columns: u16, rows: u16) -> Option<usize> {
        let width = columns.saturating_sub(3).min(70);
        (width >= 16 && rows >= 7).then_some(usize::from(width))
    }

    struct TerminalMode(Termios);

    impl TerminalMode {
        fn cbreak() -> io::Result<Self> {
            let stdin = io::stdin();
            let mode = Self(tcgetattr(&stdin)?);
            let mut cbreak = mode.0.clone();
            cbreak.input_flags.remove(InputFlags::IXON);
            cbreak.local_flags.remove(
                LocalFlags::ECHO | LocalFlags::ECHONL | LocalFlags::ICANON | LocalFlags::ISIG,
            );
            // Nonblocking reads without changing flags on the shared stdin fd.
            cbreak.control_chars[SpecialCharacterIndices::VMIN as usize] = 0;
            cbreak.control_chars[SpecialCharacterIndices::VTIME as usize] = 0;
            tcsetattr(&stdin, SetArg::TCSANOW, &cbreak)?;
            Ok(mode)
        }
    }

    impl Drop for TerminalMode {
        fn drop(&mut self) {
            let _ = tcsetattr(io::stdin(), SetArg::TCSANOW, &self.0);
        }
    }

    struct BoxWriter<W: Write> {
        out: W,
        width: usize,
        newline: &'static str,
        saved: bool,
    }

    impl<W: Write> BoxWriter<W> {
        fn border(&mut self) -> io::Result<()> {
            write!(
                self.out,
                "+{:-<width$}+{newline}",
                "",
                newline = self.newline,
                width = self.width
            )
        }

        fn row(&mut self, text: &str) -> io::Result<()> {
            let text = &text[..text.len().min(self.width)]; // All labels are ASCII.
            write!(
                self.out,
                "|{text:<width$}|{newline}",
                newline = self.newline,
                width = self.width
            )
        }

        fn paint(&mut self, frame: Frame) -> io::Result<()> {
            match frame {
                Frame::Start => {
                    write!(self.out, "{HIDE}{}", self.newline)?;
                    self.border()?;
                    self.row("")?;
                }
                Frame::Blank => self.row("")?,
                Frame::Seal => {
                    self.border()?;
                    write!(self.out, "{SAVE}")?;
                    self.saved = true;
                }
                Frame::Reveal(rows) => {
                    // The cursor was saved below our box, even when newlines
                    // scrolled the screen. Never address prior shell output.
                    write!(self.out, "{RESTORE}\x1b[4A\r")?;
                    for (index, label) in LABELS.iter().enumerate() {
                        self.row(if index < rows { label } else { "" })?;
                    }
                    write!(self.out, "{RESTORE}")?;
                }
                Frame::Ready => (),
            }
            self.out.flush()
        }
    }

    impl<W: Write> Drop for BoxWriter<W> {
        fn drop(&mut self) {
            if self.saved {
                let _ = write!(self.out, "{RESTORE}");
            }
            let _ = write!(self.out, "{SHOW}");
            let _ = self.out.flush();
        }
    }

    #[derive(Default)]
    struct Keys {
        first: Option<char>,
        draft: String,
        pending: Vec<u8>,
    }

    impl Keys {
        fn push(&mut self, bytes: &[u8]) {
            self.pending.extend_from_slice(bytes);
            // An incomplete code point may span reads, even within a paste
            // or type-ahead burst. Only the first decoded key controls startup.
            let mut complete = 0;
            loop {
                match std::str::from_utf8(&self.pending[complete..]) {
                    Ok(_) => {
                        complete = self.pending.len();
                        break;
                    }
                    Err(error) => {
                        complete += error.valid_up_to();
                        let Some(invalid) = error.error_len() else {
                            break;
                        };
                        complete += invalid;
                    }
                }
            }
            for key in String::from_utf8_lossy(&self.pending[..complete]).chars() {
                let first = self.first.is_none();
                self.first.get_or_insert(key);
                if !key.is_control()
                    && (if key == ' ' {
                        !first
                    } else {
                        !key.is_whitespace()
                    })
                {
                    self.draft.push(key);
                }
            }
            self.pending.drain(..complete);
        }

        fn cancelled(&self) -> bool {
            matches!(self.first, Some('\x1b' | '\x03' | '\x11'))
        }
    }

    async fn wait_until(deadline: Instant, keys: &mut Keys) -> io::Result<()> {
        let mut bytes = [0; 4096];
        let fd = io::stdin().as_raw_fd();
        loop {
            match read(fd, &mut bytes) {
                Ok(count) => keys.push(&bytes[..count]),
                Err(Errno::EINTR | Errno::EAGAIN) => (),
                Err(error) => return Err(error.into()),
            }
            let now = Instant::now();
            if keys.first.is_some() || now >= deadline {
                return Ok(());
            }
            sleep_until((now + POLL).min(deadline)).await;
        }
    }

    pub(super) async fn run(animate: bool) -> io::Result<Option<String>> {
        if !animate || !io::stdin().is_terminal() || !io::stdout().is_terminal() {
            return Ok(Some(String::new()));
        }
        let Ok(dimensions @ (columns, rows)) = size() else {
            return Ok(Some(String::new()));
        };
        let Some(width) = frame_width(columns, rows) else {
            return Ok(Some(String::new()));
        };
        let mode = TerminalMode::cbreak()?;
        let newline = if mode
            .0
            .output_flags
            .contains(OutputFlags::OPOST | OutputFlags::ONLCR)
        {
            "\n"
        } else {
            "\r\n"
        };
        let mut screen = BoxWriter {
            out: io::stdout(),
            width,
            newline,
            saved: false,
        };
        let start = Instant::now();
        let mut keys = Keys::default();
        for (millis, frame) in FRAMES {
            if millis != 0 {
                wait_until(start + Duration::from_millis(millis), &mut keys).await?;
            }
            // Do not rewind into the shell if the terminal reflowed our rows.
            if size().ok() != Some(dimensions) {
                screen.saved = false;
                break;
            }
            if keys.first.is_some() {
                if !screen.saved {
                    screen.border()?;
                }
                // Give the suffix of an already-read UTF-8 chunk one poll
                // to arrive, without holding up an ordinary skip or cancel.
                if !keys.cancelled() && !keys.pending.is_empty() {
                    sleep_until(Instant::now() + POLL).await;
                    wait_until(Instant::now(), &mut keys).await?;
                }
                break;
            }
            screen.paint(frame)?;
        }
        Ok((!keys.cancelled()).then_some(keys.draft))
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn normal_screen_storyboard_at_fixed_offsets() {
            let width = frame_width(80, 24).unwrap();
            let mut output = Vec::new();
            let mut screen = BoxWriter {
                out: &mut output,
                width,
                newline: "\n",
                saved: false,
            };
            let edge = format!("+{}+\n", "-".repeat(70));
            let row = |label: &str| format!("|{label:70}|\n");
            let blank = row("");
            let version = row(&format!("ngn {}", env!("CARGO_PKG_VERSION")));
            let opening = row("Opening workspace...");
            let help = row("Enter / Space: skip   Esc / Ctrl+C / Ctrl+Q: exit");
            let expected = [
                (0, format!("\x1b[?25l\n{edge}{blank}")),
                (35, blank.clone()),
                (70, blank.clone()),
                (105, format!("{edge}\x1b7")),
                (150, format!("\x1b8\x1b[4A\r{version}{blank}{blank}\x1b8")),
                (210, format!("\x1b8\x1b[4A\r{version}{opening}{blank}\x1b8")),
                (270, format!("\x1b8\x1b[4A\r{version}{opening}{help}\x1b8")),
                (320, String::new()),
            ];
            for ((time, frame), (expected_time, expected_bytes)) in FRAMES.into_iter().zip(expected)
            {
                assert_eq!(time, expected_time);
                screen.paint(frame).unwrap();
                assert_eq!(screen.out.as_slice(), expected_bytes.as_bytes());
                screen.out.clear();
            }
            drop(screen);
            assert_eq!(output, b"\x1b8\x1b[?25h");
        }

        #[test]
        fn terminal_thresholds_and_ascii_clipping() {
            assert_eq!(frame_width(18, 7), None);
            assert_eq!(frame_width(80, 6), None);
            assert_eq!(frame_width(19, 7), Some(16));
            assert_eq!(frame_width(72, 24), Some(69));
            assert_eq!(frame_width(240, 24), Some(70));
            let mut output = Vec::new();
            let mut screen = BoxWriter {
                out: &mut output,
                width: 16,
                newline: "\n",
                saved: false,
            };
            screen.row(LABELS[2]).unwrap();
            drop(screen);
            assert_eq!(output, b"|Enter / Space: s|\n\x1b[?25h");
        }

        #[test]
        fn first_key_is_consumed_without_submitting_and_utf8_drains_to_draft() {
            for (input, draft) in [
                (&b" "[..], ""),
                (b"\r\n\t", ""),
                (b" word  two\r", "word  two"),
                (b"\r/quit\r", "/quit"),
                (b"/quit\r", "/quit"),
                (b"\x7fother", "other"),
            ] {
                let mut keys = Keys::default();
                keys.push(input);
                assert!(!keys.cancelled());
                assert_eq!(keys.draft, draft);
            }
            let mut keys = Keys::default();
            keys.push(&[0xc3]);
            assert_eq!(keys.first, None);
            keys.push(&[0xa9, b' ', 0xf0, 0x9f]);
            assert_eq!(keys.first, Some('é'));
            assert_eq!(keys.draft, "é ");
            keys.push(&[0x8c, 0xb1, 0xff, b'z']);
            assert_eq!(keys.draft, "é 🌱�z");
        }

        #[test]
        fn escape_and_control_keys_cancel_in_any_frame() {
            for key in [0x1b, 0x03, 0x11] {
                let mut keys = Keys::default();
                keys.push(&[key, b'x']);
                assert!(keys.cancelled());
            }
        }
    }
}
