mod tui;
mod view;
mod web;

use std::{
    io::{IsTerminal, Read, Write},
    net::{IpAddr, SocketAddr},
    path::PathBuf,
    sync::Arc,
};

use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use ngn_core::{ApprovalMode, Config, Host, config::Backend};
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
    /// Explicit, trusted Rust configuration. Legacy Python configs are not imported.
    #[arg(long, global = true)]
    config: Option<PathBuf>,
    #[arg(long, global = true)]
    data_dir: Option<PathBuf>,
    /// Native offline demo; no Python or network required.
    #[arg(long, global = true)]
    demo: bool,
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
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
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

async fn session(host: &Host, resume: Option<&str>, continue_session: bool) -> Result<String> {
    if let Some(resume) = resume {
        return Ok(host.store.session(resume).await?.id);
    }
    if continue_session && let Some(session) = host.store.sessions().await?.into_iter().next() {
        return Ok(session.id);
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

async fn execute(cli: Cli) -> Result<i32> {
    let mut config = Config::load(cli.config.as_deref())?;
    if let Some(workspace) = cli.workspace {
        config.workspace = workspace;
    }
    if let Some(data) = cli.data_dir {
        config.data_dir = data;
    }
    if cli.demo {
        config.backend = Backend::Demo;
    }
    if cli.read_only {
        config.read_only = true;
    }
    if let Some(provider) = cli.provider_id {
        config.provider = provider;
    }
    if let Some(model) = cli.model {
        let provider = config
            .providers
            .get_mut(&config.provider)
            .context("--model requires a selected provider connection")?;
        provider.model = model;
    }
    let config = config.resolve()?;
    if matches!(cli.command, Some(Commands::Doctor)) {
        println!(
            "ngn {}\nAgent Runtime Protocol: {}\nWorkspace: {}\nRust state: {}\nBackend: {:?}\nProvider: {}\nRead only: {}\nRunner: {}\nNo legacy state was imported. No runner or network request was started.",
            env!("CARGO_PKG_VERSION"),
            ngn_protocol::VERSION,
            config.workspace.display(),
            config.state_dir().display(),
            config.backend,
            config.provider,
            config.read_only,
            config.runner.first().map(String::as_str).unwrap_or("unset")
        );
        return Ok(0);
    }
    let host = Host::open(config)?;
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
                    "A prompt is required. Try: ngn --demo run --json 'hello'"
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
            Some(Commands::Doctor) => unreachable!(),
            None => {
                ensure!(
                    std::io::stdin().is_terminal() && std::io::stdout().is_terminal(),
                    "The TUI needs a terminal; use ngn run --json PROMPT for scripts"
                );
                let id = session(&host, cli.resume.as_deref(), cli.continue_session).await?;
                tui::run(host.clone(), id).await?;
                Ok(0)
            }
        }
    }
    .await;
    host.shutdown().await;
    result
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    let code = match execute(Cli::parse()).await {
        Ok(code) => code,
        Err(error) => {
            eprintln!("ngn: {}", view::plain(&error.to_string()));
            2
        }
    };
    std::process::ExitCode::from(code as u8)
}
