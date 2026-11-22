mod bundle;
mod engine;
mod engine_process;
mod install;
mod model;
mod paths;
#[cfg(test)]
mod test_support;
mod tui;
mod view;
mod web;

use std::{
    ffi::OsString,
    io::{IsTerminal, Read, Write},
    net::{IpAddr, SocketAddr},
    path::PathBuf,
};

use anyhow::{Context, Result, ensure};
use clap::{ArgAction, CommandFactory, FromArgMatches, Parser, Subcommand, error::ErrorKind};
use engine::{Engine, SessionSelection};
use engine_process::{EngineProcess, Launch};
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
    about = "Terminal and browser client for the nagents Python engine"
)]
struct Cli {
    #[arg(short = 'C', long, global = true)]
    workspace: Option<PathBuf>,
    /// Trusted configuration file for the Python engine.
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
    /// Start a private Python engine that shuts down when this client exits.
    #[arg(long, global = true)]
    standalone: bool,
    /// Use the offline demo when starting the Python engine.
    #[arg(long, global = true)]
    engine_demo: bool,
    /// Trust project configuration and code when starting the Python engine.
    #[arg(long, global = true)]
    trust_project: bool,
    #[arg(long, global = true, conflicts_with = "continue_session")]
    resume: Option<String>,
    #[arg(short = 'c', long = "continue", global = true)]
    continue_session: bool,
    /// Disable TUI and update progress animations for this invocation.
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

fn animations_enabled(no_animations: bool, saved: Option<bool>, textual: Option<&str>) -> bool {
    !no_animations
        && saved.unwrap_or(true)
        && !textual.is_some_and(|value| value.eq_ignore_ascii_case("none"))
}

#[derive(Subcommand)]
enum Commands {
    /// Install a verified self-contained bundle, without loading config or state.
    Install(install::Install),
    /// Update only this managed installation, without touching running hosts/state.
    Update(install::Update),
    /// Provider login, status and logout, handled by the Python engine's runtime.
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
    /// List sessions for this workspace.
    Sessions,
    /// Show the Python engine's configuration diagnostics without an LLM call.
    Doctor,
}

#[derive(Subcommand)]
enum AuthCommand {
    /// Sign in (ChatGPT device code by default; pass a method such as `openrouter`).
    Login {
        #[arg(num_args = 0.., allow_hyphen_values = true, trailing_var_arg = true)]
        arguments: Vec<OsString>,
    },
    /// Show local login status without a network request.
    Status,
    /// Remove saved logins.
    Logout,
}

impl AuthCommand {
    /// The `nagents.cli` arguments that perform this operation.
    fn arguments(&self) -> Vec<OsString> {
        match self {
            Self::Login { arguments } if arguments.is_empty() => {
                vec!["login".into(), "chatgpt".into(), "--device-auth".into()]
            }
            Self::Login { arguments } => std::iter::once("login".into())
                .chain(arguments.iter().cloned())
                .collect(),
            Self::Status => vec!["login".into(), "--status".into()],
            Self::Logout => vec!["logout".into()],
        }
    }
}

/// Run a `nagents.cli` command with this invocation's workspace and config, in
/// the same Python runtime as the engine.
async fn python_cli(cli: &Cli, arguments: Vec<OsString>) -> Result<i32> {
    let mut command = engine_process::python_command()?;
    if arguments.first().is_some_and(|first| first == "doctor") {
        println!(
            "Engine Python: {}",
            command.as_std().get_program().to_string_lossy()
        );
    }
    command
        .args(["-I", "-B", "-m", "nagents.cli"])
        .args(arguments);
    if let Some(workspace) = &cli.workspace {
        command.arg("--workspace").arg(workspace);
    }
    if let Some(config) = &cli.config {
        command.arg("--config").arg(config);
    }
    if let Some(provider) = &cli.provider_id {
        command.arg("--provider-id").arg(provider);
    }
    let status = command
        .kill_on_drop(true)
        .status()
        .await
        .context("Could not run Python. Install nagents[engine] or set NGN_ENGINE_PYTHON.")?;
    Ok(status.code().unwrap_or(1))
}

fn run_prompt(words: Vec<String>) -> Result<String> {
    let mut prompt = words.join(" ");
    if prompt == "-" || (prompt.is_empty() && !std::io::stdin().is_terminal()) {
        prompt.clear();
        std::io::stdin().take(32_001).read_to_string(&mut prompt)?;
    }
    ensure!(
        !prompt.trim().is_empty(),
        "A prompt is required. Try: ngn run --json 'hello'"
    );
    Ok(prompt)
}

async fn engine_session(
    client: &ngn_api::Client,
    selection: SessionSelection<'_>,
) -> Result<String> {
    use ngn_api::models::SessionCreate;

    match selection {
        SessionSelection::Resume(id) => return Ok(client.session(id).await?.id),
        SessionSelection::LatestOrNew => {
            if let Some(session) = client
                .sessions()
                .await?
                .into_iter()
                .find(|session| !session.archived)
            {
                return Ok(session.id);
            }
        }
        SessionSelection::New => (),
    }
    Ok(client
        .create_session(&SessionCreate { title: None })
        .await?
        .id)
}

fn print_engine_record(
    record: &ngn_api::models::EventRecord,
    json: bool,
    streamed: &mut bool,
) -> Result<()> {
    use ngn_api::models::EngineEvent;

    if json {
        println!("{}", serde_json::to_string(record)?);
    } else {
        match &record.event {
            EngineEvent::TextDelta(value) => {
                print!("{}", view::plain(&value.text));
                *streamed = true;
            }
            EngineEvent::TextCompleted(value) => {
                if !*streamed {
                    print!("{}", view::plain(&value.text));
                }
                println!();
                *streamed = false;
            }
            EngineEvent::ToolStarted(value) => eprintln!("[{}]", view::plain(&value.call.name)),
            EngineEvent::ToolCompleted(value) if !value.result.error.is_empty() => eprintln!(
                "[{}: {}]",
                view::plain(&value.result.name),
                view::plain(&value.result.error)
            ),
            EngineEvent::Notice(value) => eprintln!("{}", view::plain(&value.text)),
            EngineEvent::RunFinished(value) => {
                if !value.error.is_empty() {
                    eprintln!("{}", view::plain(&value.error));
                }
                eprintln!("Session: {} · {}", record.session_id, value.status.as_str());
            }
            _ => (),
        }
    }
    std::io::stdout().flush()?;
    Ok(())
}

async fn headless_python(
    client: &ngn_api::Client,
    session: &str,
    prompt: String,
    json: bool,
) -> Result<i32> {
    use ngn_api::models::{EngineEvent, RunMode, RunStart, RunStatus};

    let mut events = client.subscribe(0, Some(session)).await?;
    let request_id = Uuid::new_v4().to_string();
    let receipt = client
        .start_run(
            session,
            &RunStart {
                request_id: request_id.clone(),
                prompt,
                mode: Some(RunMode::Idle),
                interactive: Some(false),
            },
        )
        .await
        .with_context(|| format!("Run admission failed; request ID {request_id}"))?;
    ensure!(
        receipt.session_id == session && receipt.request_id == request_id,
        "Run receipt identity mismatch"
    );
    let run = receipt
        .run_id
        .context("Python engine did not identify the submitted run")?;
    let mut streamed = false;
    let mut stop_requested = false;
    loop {
        tokio::select! {
            _ = shutdown_signal(), if !stop_requested => {
                stop_requested = true;
                if let Err(error) = client.cancel_run(&run).await {
                    eprintln!("Cancellation request: {}", view::plain(&error.to_string()));
                }
            }
            next = events.next() => {
                let record = next?.context("Engine event stream closed before completion. Reopen the session to recover its durable history.")?;
                if record.run_id != run || record.session_id != session { continue; }
                print_engine_record(&record, json, &mut streamed)?;
                if let EngineEvent::RunFinished(value) = record.event {
                    return Ok(match value.status {
                        RunStatus::Completed => 0,
                        RunStatus::Cancelled => 130,
                        _ => 1,
                    });
                }
            }
        }
    }
}

async fn execute_engine(cli: Cli, wire_log: Option<bool>) -> Result<i32> {
    if cli.command.is_none() {
        ensure!(
            std::io::stdin().is_terminal() && std::io::stdout().is_terminal(),
            "The TUI needs a terminal; use ngn run --json PROMPT for scripts"
        );
    }
    let options = Launch {
        workspace: cli.workspace.as_deref(),
        data_dir: cli.data_dir.as_deref(),
        config: cli.config.as_deref(),
        provider_id: cli.provider_id.as_deref(),
        model: cli.model.as_deref(),
        wire_log,
        read_only: cli.read_only,
        trust_project: cli.trust_project,
        demo: cli.engine_demo,
        standalone: cli.standalone,
    };
    let engine = EngineProcess::local(options).await?;
    let selection = SessionSelection::initial(cli.resume.as_deref(), cli.continue_session);
    let result = async {
        match cli.command {
            Some(Commands::Serve { host, port }) => {
                web::serve_python(engine.client.clone(), SocketAddr::new(host, port)).await?;
                Ok(0)
            }
            Some(Commands::Sessions) => {
                for session in engine.client.sessions().await? {
                    println!(
                        "{}  {}",
                        view::plain(&session.id),
                        view::plain(&session.title)
                    );
                }
                Ok(0)
            }
            Some(Commands::Run { json, prompt }) => {
                let prompt = run_prompt(prompt)?;
                let session = engine_session(&engine.client, selection).await?;
                headless_python(&engine.client, &session, prompt, json).await
            }
            None => {
                let engine = engine::Remote::new(engine.client.clone())
                    .await
                    .context("Could not load Python engine configuration")?;
                tui::run(&engine, selection, !cli.no_animations.is_empty()).await?;
                engine.shutdown().await;
                Ok(0)
            }
            _ => unreachable!(),
        }
    }
    .await;
    let shutdown = engine.shutdown().await;
    if result.is_ok() {
        shutdown?;
    }
    result
}

async fn execute(cli: Cli, wire_log_override: Option<bool>) -> Result<i32> {
    match &cli.command {
        Some(command @ (Commands::Install(_) | Commands::Update(_))) => {
            let cancel = tokio_util::sync::CancellationToken::new();
            let operation = async {
                match command {
                    Commands::Install(arguments) => install::install(arguments, &cancel).await,
                    Commands::Update(arguments) => {
                        install::update(arguments, &cancel, !cli.no_animations.is_empty()).await
                    }
                    _ => unreachable!(),
                }
            };
            tokio::pin!(operation);
            tokio::select! {
                result = &mut operation => { result?; Ok(0) },
                _ = shutdown_signal() => {
                    cancel.cancel();
                    let _ = operation.await;
                    eprintln!("Installation cancelled. Inspect `current` if cancellation raced with successful activation; running engines and state were not changed.");
                    Ok(130)
                }
            }
        }
        Some(Commands::Auth { operation }) => python_cli(&cli, operation.arguments()).await,
        Some(Commands::Doctor) => python_cli(&cli, vec!["doctor".into()]).await,
        _ => execute_engine(cli, wire_log_override).await,
    }
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
    fn mouse_controls_do_not_take_a_cli_flag() {
        assert!(matches!(
            parse(&["--mouse"]),
            Err(error) if error.kind() == ErrorKind::UnknownArgument
        ));
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
