mod install;
#[cfg(test)]
#[path = "../../ngn-core/src/test_support.rs"]
mod test_support;
mod tui;
mod view;
mod web;

use std::{
    ffi::OsString,
    io::{IsTerminal, Read, Write},
    net::{IpAddr, SocketAddr},
    path::PathBuf,
    sync::Arc,
};

use anyhow::{Result, ensure};
use clap::{ArgAction, CommandFactory, FromArgMatches, Parser, Subcommand, error::ErrorKind};
use ngn_core::auth::{self, Operation};
use ngn_core::{ApprovalMode, Config, Host};
use ngn_protocol::{Event, EventRecord, RunStatus};
use uuid::Uuid;

pub(crate) async fn shutdown_signal() -> std::io::Result<()> {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => result,
            _ = terminate.recv() => Ok(()),
        }
    }
    #[cfg(not(unix))]
    tokio::signal::ctrl_c().await
}

#[derive(Parser)]
#[command(
    name = "ngn",
    version,
    about = "Rust agent harness · terminal and local web host"
)]
struct Cli {
    #[arg(short = 'C', long, global = true)]
    workspace: Option<PathBuf>,
    /// Trusted Rust configuration file; omit to use existing Python ngn provider settings.
    #[arg(long, global = true)]
    config: Option<PathBuf>,
    #[arg(long, global = true)]
    data_dir: Option<PathBuf>,
    #[arg(long, global = true)]
    read_only: bool,
    #[arg(long, global = true)]
    provider_id: Option<String>,
    #[arg(short, long, global = true)]
    model: Option<String>,
    #[arg(long, global = true, conflicts_with = "continue_session")]
    resume: Option<String>,
    #[arg(short = 'c', long = "continue", global = true)]
    continue_session: bool,
    /// Disable TUI activity and caret animations for this invocation.
    #[arg(
        long,
        global = true,
        aliases = ["no-a", "no-an", "no-ani", "no-anim", "no-anima", "no-animat",
                   "no-animati", "no-animatio", "no-animation"],
        // Keep every occurrence's position without consuming an argument value.
        action = ArgAction::Append,
        num_args = 0,
        default_missing_value = "true",
    )]
    no_animations: Vec<bool>,
    /// Capture sensitive provider HTTP payloads in a private local log (off by default).
    #[arg(long, global = true, overrides_with_all = ["wire_log", "no_wire_log"])]
    wire_log: bool,
    /// Disable provider payload logging, even if NGN_WIRE_LOG is set.
    #[arg(long, global = true, overrides_with_all = ["wire_log", "no_wire_log"])]
    no_wire_log: bool,
    #[command(subcommand)]
    command: Option<Commands>,
}

fn wire_log_enabled(wire_log_override: Option<bool>) -> Result<bool> {
    if let Some(enabled) = wire_log_override {
        return Ok(enabled);
    }
    match std::env::var("NGN_WIRE_LOG") {
        Ok(value) => match value.to_ascii_lowercase().as_str() {
            "true" | "1" => Ok(true),
            "false" | "0" => Ok(false),
            _ => anyhow::bail!("NGN_WIRE_LOG must be true/false or 1/0"),
        },
        Err(std::env::VarError::NotPresent) => Ok(false),
        Err(std::env::VarError::NotUnicode(_)) => {
            anyhow::bail!("NGN_WIRE_LOG must be true/false or 1/0")
        }
    }
}

#[derive(Subcommand)]
enum Commands {
    /// Install a verified self-contained bundle, without loading config or state.
    Install(install::Install),
    /// Update only this managed installation, without touching running hosts/state.
    Update(install::Update),
    /// Import a Python sessions backup as read-only history into fresh Rust workspace state.
    Upgrade {
        /// Offline, single-file SQLite backup (not the live sessions.db).
        #[arg(long = "from", value_name = "BACKUP")]
        backup: PathBuf,
    },
    /// Explicit ChatGPT device login and local status/logout. No browser is opened.
    Auth {
        #[command(subcommand)]
        operation: AuthCommand,
    },
    /// Run one prompt. All modifying tools fail closed in this mode.
    Run {
        #[arg(long)]
        json: bool,
        #[arg(num_args = 0..)]
        prompt: Vec<String>,
    },
    /// Serve the bundled browser client and authenticated same-origin API.
    Serve {
        #[arg(long, default_value = "127.0.0.1")]
        host: IpAddr,
        #[arg(long, default_value_t = 8765)]
        port: u16,
    },
    /// List Rust-host sessions for this workspace.
    Sessions,
    /// Show local configuration diagnostics without starting Python or making requests.
    Doctor,
}

#[derive(Subcommand)]
enum AuthCommand {
    Login,
    Status,
    Logout,
}

impl AuthCommand {
    fn operation(&self) -> Operation {
        match self {
            Self::Login => Operation::Login,
            Self::Status => Operation::Status,
            Self::Logout => Operation::Logout,
        }
    }
}

async fn session(host: &Host, resume: Option<&str>, continue_session: bool) -> Result<String> {
    if let Some(resume) = resume {
        return Ok(host.store.session(resume).await?.id);
    }
    if continue_session && let Some(id) = host.store.latest_native_session().await? {
        return Ok(id);
    }
    Ok(host.store.create_session().await?.id)
}

fn print_record(record: &EventRecord, json: bool, streamed: &mut bool) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string(record)?);
    } else {
        match &record.event {
            Event::TextDelta { text } => {
                print!("{}", view::plain(text));
                *streamed = true;
            }
            Event::TextCompleted { text } => {
                if !*streamed {
                    print!("{}", view::plain(text));
                }
                println!();
                *streamed = false;
            }
            Event::ToolStarted { call } => eprintln!("[{}]", view::plain(&call.name)),
            Event::ToolCompleted { result, .. } if !result.error.is_empty() => eprintln!(
                "[{}: {}]",
                view::plain(&result.name),
                view::plain(&result.error)
            ),
            Event::Notice { text } => eprintln!("{}", view::plain(text)),
            Event::RunFinished { status, error } => {
                if !error.is_empty() {
                    eprintln!("{}", view::plain(error));
                }
                eprintln!("Session: {} · {status:?}", record.session_id);
            }
            _ => (),
        }
    }
    std::io::stdout().flush()?;
    Ok(())
}

async fn headless(host: Arc<Host>, session: &str, prompt: &str, json: bool) -> Result<i32> {
    let mut receiver = host.events.subscribe();
    let run = host
        .start(
            session,
            prompt,
            &Uuid::new_v4().to_string(),
            ApprovalMode::Deny,
        )
        .await?;
    let mut after = 0;
    let mut streamed = false;
    let mut cancelled = false;
    loop {
        // Cursor replay also closes the subscribe/admission race and recovers lag.
        let records = host.store.events(session, after).await?;
        let count = records.len();
        for record in records {
            after = record.sequence;
            if record.run_id != run.id {
                continue;
            }
            print_record(&record, json, &mut streamed)?;
            if let Event::RunFinished { status, .. } = record.event {
                return Ok(if status == RunStatus::Completed {
                    0
                } else if status == RunStatus::Cancelled {
                    130
                } else {
                    1
                });
            }
        }
        if count == 1000 {
            continue;
        }
        tokio::select! {
            _ = shutdown_signal(), if !cancelled => { cancelled = true; let _ = host.cancel(&run.id).await; }
            event = receiver.recv() => { if matches!(event, Err(tokio::sync::broadcast::error::RecvError::Closed)) { anyhow::bail!("Host event stream closed before completion"); } }
        }
    }
}

async fn execute(cli: Cli, wire_log_override: Option<bool>) -> Result<i32> {
    if let Some(Commands::Upgrade { backup }) = &cli.command {
        ensure!(
            cli.config.is_none()
                && !cli.read_only
                && cli.provider_id.is_none()
                && cli.model.is_none()
                && cli.resume.is_none()
                && !cli.continue_session
                && wire_log_override.is_none(),
            "Host configuration and run options cannot be used with upgrade; select paths with -C and --data-dir"
        );
        let report = ngn_core::upgrade::import_python_backup(
            backup,
            cli.workspace.as_deref(),
            cli.data_dir.as_deref(),
        )
        .await?;
        println!(
            "Imported {} read-only Python archives ({} text messages, {} skipped rows).\n\
             Rust state: {}\nOpen the native session picker to inspect them; start a new session to run prompts.",
            report.sessions,
            report.messages,
            report.skipped_messages,
            view::plain(&report.destination.display().to_string()),
        );
        if !report.directory_synced {
            eprintln!("Archive published, but workspace parent could not be synced to disk.");
        }
        return Ok(0);
    }
    if let Some(command @ (Commands::Install(_) | Commands::Update(_))) = &cli.command {
        let cancel = tokio_util::sync::CancellationToken::new();
        let operation = async {
            match command {
                Commands::Install(arguments) => install::install(arguments, &cancel).await,
                Commands::Update(arguments) => install::update(arguments, &cancel).await,
                _ => unreachable!(),
            }
        };
        tokio::pin!(operation);
        return tokio::select! {
            result = &mut operation => { result?; Ok(0) },
            _ = shutdown_signal() => {
                cancel.cancel();
                let _ = operation.await;
                eprintln!("Installation cancelled. Inspect `current` if cancellation raced with successful activation; existing hosts and state were not changed.");
                Ok(130)
            }
        };
    }
    let mut config = Config::load(cli.config.as_deref())?;
    if let Some(workspace) = cli.workspace {
        config.workspace = workspace;
    }
    if let Some(data) = cli.data_dir {
        config.data_dir = data;
    }
    if cli.read_only {
        config.read_only = true;
    }
    if let Some(provider) = cli.provider_id {
        config.provider = provider;
    }
    let config = config.resolve_with_model(cli.model)?;
    if let Some(Commands::Auth { operation }) = &cli.command {
        let cancel = tokio_util::sync::CancellationToken::new();
        let command = auth::run(
            &config,
            operation.operation(),
            cancel.clone(),
            |challenge| {
                eprintln!(
                    "Open {} manually and enter code: {}\nNo browser approval is automated. Waiting for authorization…",
                    auth::VERIFICATION_URL,
                    challenge.user_code
                );
            },
        );
        tokio::pin!(command);
        let result = tokio::select! {
            result = &mut command => result,
            _ = shutdown_signal() => {
                cancel.cancel();
                let _ = command.await;
                eprintln!("Authentication cancelled. Check ngn auth status; completed identity changes are not rolled back.");
                return Ok(130);
            }
        }?;
        println!("{}", result.description());
        return Ok(if result == auth::AuthStatus::Unusable {
            1
        } else {
            0
        });
    }
    if matches!(cli.command, Some(Commands::Doctor)) {
        let source = if let Some(path) = &config.source {
            path.display().to_string()
        } else if let Some(directory) = &config.legacy_directory {
            format!(
                "built-in defaults; checking Python ngn settings in {} (read-only)",
                directory.display()
            )
        } else {
            "built-in defaults (no Python ngn settings directory found)".into()
        };
        let selected = config.selected_provider();
        println!(
            "ngn {}\nAgent Runtime Protocol: {}\nConfig: {}\nWorkspace: {}\nRust state: {}\nBackend: {:?}\nProvider: {}\nRead only: {}\nRunner: {}\nPython ngn settings are only read, never modified or migrated. No runner or network request was started.",
            env!("CARGO_PKG_VERSION"),
            ngn_protocol::VERSION,
            view::plain(&source),
            config.workspace.display(),
            config.state_dir().display(),
            config.backend,
            selected
                .as_ref()
                .map_or_else(|_| "none".into(), |_| view::plain(&config.provider)),
            config.read_only,
            config.runner.first().map(String::as_str).unwrap_or("unset")
        );
        match selected {
            Ok(provider) => println!(
                "Model: {} · auth {:?}",
                view::plain(&provider.model),
                provider.auth
            ),
            Err(error) => println!("Provider setup: {}", view::plain(&error.to_string())),
        }
        if !config.legacy_sources.is_empty() {
            println!(
                "Python ngn provider files (read-only): {}",
                config
                    .legacy_sources
                    .iter()
                    .map(|path| view::plain(&path.display().to_string()))
                    .collect::<Vec<_>>()
                    .join(", ")
            );
        }
        for (name, reason) in &config.unavailable {
            println!(
                "Unavailable connection {}: {}",
                view::plain(name),
                view::plain(reason)
            );
        }
        if let Some(runtime) = &config.bundled_runtime {
            println!(
                "Bundled CPython: {}\nBundled runtime: {}",
                runtime.python_version(),
                runtime.python().display()
            );
        }
        return Ok(0);
    }
    if cli.command.is_none() {
        ensure!(
            std::io::stdin().is_terminal() && std::io::stdout().is_terminal(),
            "The TUI needs a terminal; use ngn run --json PROMPT for scripts"
        );
    }
    let wire_log = wire_log_enabled(wire_log_override)?;
    let history = ngn_core::upgrade::PythonHistory::discover(&config.workspace);
    if let Ok(Some(history)) = &history {
        history.ensure_separate(&config)?;
    }
    let host = Host::open_with_wire_log(config, wire_log)?;
    let imported = match history {
        Ok(Some(history)) => history.append(&host).await,
        Ok(None) => Ok(0),
        Err(error) => Err(error),
    };
    match imported {
        Ok(0) => (),
        Ok(count) => {
            eprintln!("ngn: copied {count} read-only Python archives into this workspace.")
        }
        Err(error) => eprintln!(
            "ngn: Python history was not copied: {}. Startup is continuing. \
             Close Python ngn and retry, or use `ngn upgrade --from BACKUP --data-dir FRESH_DIR` with a standalone backup.",
            view::plain(&format!("{error:#}"))
        ),
    }
    if cli.command.is_some() && host.runtime_log_path().is_none() {
        eprintln!("ngn: runtime log unavailable; continuing without it");
    }
    let result = async {
        match cli.command {
            Some(Commands::Run { json, prompt }) => {
                let mut prompt = prompt.join(" ");
                if prompt == "-" || (prompt.is_empty() && !std::io::stdin().is_terminal()) {
                    prompt.clear();
                    std::io::stdin().take(32_001).read_to_string(&mut prompt)?;
                }
                ensure!(
                    !prompt.trim().is_empty(),
                    "A prompt is required. Try: ngn run --json 'hello'"
                );
                let id = session(&host, cli.resume.as_deref(), cli.continue_session).await?;
                headless(host.clone(), &id, &prompt, json).await
            }
            Some(Commands::Serve {
                host: address,
                port,
            }) => {
                web::serve(host.clone(), SocketAddr::new(address, port)).await?;
                Ok(0)
            }
            Some(Commands::Sessions) => {
                for session in host.store.sessions().await? {
                    println!("{}  {}", session.id, view::plain(&session.title));
                }
                Ok(0)
            }
            Some(
                Commands::Doctor
                | Commands::Auth { .. }
                | Commands::Install(_)
                | Commands::Update(_)
                | Commands::Upgrade { .. },
            ) => unreachable!(),
            None => {
                let id = session(&host, cli.resume.as_deref(), cli.continue_session).await?;
                tui::run(host.clone(), id, !cli.no_animations.is_empty()).await?;
                Ok(0)
            }
        }
    }
    .await;
    host.shutdown().await;
    result
}

fn parse_cli(args: &[OsString]) -> Result<Cli, clap::Error> {
    let mut command = Cli::command();
    let mut matches = command.try_get_matches_from_mut(args)?;
    if let Some(run) = matches.subcommand_matches("run")
        && let Some(mut prompt) = run.indices_of("prompt")
        && let Some(first) = prompt.next()
        && let Some(mut flags) = run.indices_of("no_animations")
        && let Some(split) = flags.find(|&index| index > first)
    {
        // Python's run parser consumes one contiguous variadic prompt. If it
        // has already started, the motion flag ends it. Use Clap's positions
        // rather than scanning tokens for words (options can take values).
        let has_tail = prompt.any(|index| index > split);
        // Clap drops an empty trailing delimiter; Python treats it as extra
        // input after a flag has already ended the prompt.
        let bare_delimiter_at_end = args.last().is_some_and(|arg| arg == "--");
        if has_tail || bare_delimiter_at_end {
            return Err(command.error(
                ErrorKind::UnknownArgument,
                "unexpected arguments after --no-animations; place it before or after the entire prompt",
            ));
        }
    }
    Cli::from_arg_matches_mut(&mut matches)
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args_os().collect();
    let cli = parse_cli(&args).unwrap_or_else(|error| error.exit());
    // Clap propagates global arguments between parser levels, but does not
    // preserve the relative order of different flag IDs across a subcommand.
    let wire_log_override = if cli.wire_log || cli.no_wire_log {
        args.iter()
            .skip(1)
            .take_while(|arg| *arg != "--")
            .filter_map(|arg| match arg.to_str() {
                Some("--wire-log") => Some(true),
                Some("--no-wire-log") => Some(false),
                _ => None,
            })
            .last()
    } else {
        None
    };
    let code = match execute(cli, wire_log_override).await {
        Ok(code) => code,
        Err(error) => {
            eprintln!("ngn: {}", view::plain(&error.to_string()));
            2
        }
    };
    std::process::ExitCode::from(code as u8)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Cli, clap::Error> {
        parse_cli(
            &std::iter::once("ngn")
                .chain(args.iter().copied())
                .map(OsString::from)
                .collect::<Vec<_>>(),
        )
    }

    #[test]
    fn no_animations_matches_python_prefixes_and_placement() {
        // Observed via Python ngn's _parser().parse_args at ba0a7bc:
        // --n, --no and --no- are ambiguous; every longer prefix is accepted.
        const FULL: &str = "--no-animations";
        assert!(parse(&[]).unwrap().no_animations.is_empty());
        for flag in ["--n", "--no", "--no-", "--no-animationz"] {
            assert!(parse(&[flag]).is_err(), "{flag}");
            assert!(parse(&["run", "hello", flag]).is_err(), "{flag}");
        }
        for end in "--no-a".len()..=FULL.len() {
            let flag = &FULL[..end];
            assert_eq!(parse(&[flag, flag]).unwrap().no_animations.len(), 2);
            for args in [
                [flag, "run", "hello", "world"],
                ["run", flag, "hello", "world"],
                ["run", "hello", "world", flag],
            ] {
                let cli = parse(&args).unwrap();
                assert!(!cli.no_animations.is_empty(), "{args:?}");
                assert!(
                    matches!(cli.command, Some(Commands::Run { prompt, .. }) if prompt == ["hello", "world"]),
                    "{args:?}"
                );
            }
            for args in [
                &["run", "hello", flag, "world"][..],
                &["run", "hello", flag, "world", FULL],
                &[FULL, "run", "hello", flag, FULL, "world"],
                &["run", "hello", flag, "--", "world"],
                &["run", "hello", flag, "--"],
            ] {
                assert!(
                    matches!(parse(args), Err(error) if error.kind() == ErrorKind::UnknownArgument),
                    "{args:?}"
                );
            }
            assert!(parse(&[&format!("{flag}=true")]).is_err());
        }
    }

    #[test]
    fn no_animations_respects_literal_delimiter_and_option_values() {
        for (args, disabled, expected) in [
            (
                &["run", "--", "--no-animations"][..],
                false,
                &["--no-animations"][..],
            ),
            (
                &["run", "hello", "--", "--no-anim", "world"],
                false,
                &["hello", "--no-anim", "world"],
            ),
            (
                &["run", "--no-animations", "--", "hello", "--no-anim"],
                true,
                &["hello", "--no-anim"],
            ),
            (&["run", "--no-animations", "hello", "--"], true, &["hello"]),
            (
                &[
                    "run",
                    "--config",
                    "run",
                    "--model",
                    "hello",
                    "--no-anim",
                    "world",
                ],
                true,
                &["world"],
            ),
            (
                &[
                    "--config",
                    "run",
                    "run",
                    "--model=hello",
                    "--no-anim",
                    "world",
                ],
                true,
                &["world"],
            ),
            (
                &["run", "hello", "--no-anim", "--model", "world"],
                true,
                &["hello"],
            ),
            (&["run", "hello", "--no-anim", "-mworld"], true, &["hello"]),
            (
                &["run", "hello", "--no-anim", "--config=world"],
                true,
                &["hello"],
            ),
            (
                &[
                    "run",
                    "--model=--no-anim",
                    "--config=--no-animation",
                    "hello",
                ],
                false,
                &["hello"],
            ),
            (&["run", "-m--no-anim", "hello"], false, &["hello"]),
        ] {
            let cli = parse(args).unwrap();
            assert_eq!(!cli.no_animations.is_empty(), disabled, "{args:?}");
            assert!(
                matches!(cli.command, Some(Commands::Run { prompt, .. }) if prompt == expected),
                "{args:?}"
            );
        }
        let cli = parse(&[
            "run",
            "--model=--no-anim",
            "--config=--no-animation",
            "hello",
        ])
        .unwrap();
        assert_eq!(cli.model.as_deref(), Some("--no-anim"));
        assert_eq!(
            cli.config.as_deref(),
            Some(std::path::Path::new("--no-animation"))
        );
    }

    #[test]
    fn no_animations_does_not_change_other_options() {
        assert!(parse(&["--mod", "fixture"]).is_err());
        let cli = parse(&["run", "hello", "--model", "fixture", "world"]).unwrap();
        assert_eq!(cli.model.as_deref(), Some("fixture"));
        assert!(
            matches!(cli.command, Some(Commands::Run { prompt, .. }) if prompt == ["hello", "world"])
        );
    }
}
