//! HTTP and WebSocket transport for the generated engine contract.
//!
//! This crate knows neither the TUI nor any agent runtime. Applications should
//! connect once, validate the engine major version, and keep their own event
//! cursor so reconnects can request replay.

mod methods;
pub mod models;
mod stream;

use std::{
    fmt,
    sync::{Arc, RwLock},
    time::Duration,
};

use models::ErrorResponse;
pub use models::{EVENTS_PATH, EVENTS_SUBPROTOCOL, PROTOCOL_REVISION, PROTOCOL_VERSION};
use reqwest::{Method, Url};
use serde::{Serialize, de::DeserializeOwned};
pub use stream::Events;

const MAX_RESPONSE: usize = 16 * 1024 * 1024;

#[derive(Debug)]
pub enum Error {
    Configuration(&'static str),
    Http(reqwest::StatusCode, String),
    Transport(reqwest::Error),
    Json(serde_json::Error),
    WebSocket(Box<tokio_tungstenite::tungstenite::Error>),
    Incompatible { client: u64, engine: u64 },
    TooLarge,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Configuration(message) => f.write_str(message),
            Self::Http(status, detail) => write!(f, "Engine returned {status}: {detail}"),
            Self::Transport(error) => write!(f, "Engine request failed: {error}"),
            Self::Json(error) => write!(f, "Invalid engine JSON: {error}"),
            Self::WebSocket(error) => write!(f, "Engine event connection failed: {error}"),
            Self::Incompatible { client, engine } => {
                write!(
                    f,
                    "Incompatible engine protocol: client v{client}, engine v{engine}"
                )
            }
            Self::TooLarge => f.write_str("Engine response exceeds the client size limit"),
        }
    }
}

impl std::error::Error for Error {}

#[derive(Clone)]
pub struct Client {
    endpoint: Arc<RwLock<Endpoint>>,
    http: reqwest::Client,
}

#[derive(Clone)]
struct Endpoint {
    base: Url,
    token: String,
}

impl Client {
    pub async fn connect(base: &str, token: &str) -> Result<Self, Error> {
        let client = Self::configured(base, token)?;
        let version = client.info().await?.protocol_version;
        if version != PROTOCOL_VERSION {
            return Err(Error::Incompatible {
                client: PROTOCOL_VERSION,
                engine: version,
            });
        }
        Ok(client)
    }

    fn configured(base: &str, token: &str) -> Result<Self, Error> {
        let base = Url::parse(base).map_err(|_| Error::Configuration("Invalid engine URL"))?;
        let encrypted_or_loopback = base.scheme() == "https"
            || (base.scheme() == "http" && base.host_str() == Some("127.0.0.1"));
        if !encrypted_or_loopback
            || base.host_str().is_none()
            || !base.username().is_empty()
            || base.password().is_some()
            || base.path() != "/"
            || base.query().is_some()
            || base.fragment().is_some()
        {
            return Err(Error::Configuration(
                "Use an engine origin (HTTP on 127.0.0.1 or HTTPS) without credentials or a path",
            ));
        }
        if token.is_empty()
            || token.len() > 512
            || !token
                .bytes()
                .all(|value| value.is_ascii_alphanumeric() || b"-_".contains(&value))
        {
            return Err(Error::Configuration("Invalid engine token"));
        }
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(Error::Transport)?;
        Ok(Self {
            endpoint: Arc::new(RwLock::new(Endpoint {
                base,
                token: token.to_owned(),
            })),
            http,
        })
    }

    fn endpoint(&self) -> Endpoint {
        self.endpoint
            .read()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }

    /// Redirect subsequent requests to a replacement that already passed
    /// `connect`. Existing requests and sockets are not retried or replayed.
    pub fn use_verified_endpoint(&self, replacement: &Self) {
        let endpoint = replacement.endpoint();
        *self
            .endpoint
            .write()
            .unwrap_or_else(|error| error.into_inner()) = endpoint;
    }

    async fn send<T: DeserializeOwned, B: Serialize>(
        &self,
        method: Method,
        path: &[&str],
        query: &[(&str, String)],
        body: Option<&B>,
    ) -> Result<T, Error> {
        let endpoint = self.endpoint();
        let url = endpoint.url(path)?;
        let mut request = self.http.request(method, url).bearer_auth(&endpoint.token);
        if !query.is_empty() {
            request = request.query(query);
        }
        if let Some(body) = body {
            request = request.json(body);
        }
        let mut response = request.send().await.map_err(Error::Transport)?;
        let status = response.status();
        if response
            .content_length()
            .is_some_and(|size| size > MAX_RESPONSE as u64)
        {
            return Err(Error::TooLarge);
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(Error::Transport)? {
            if chunk.len() > MAX_RESPONSE - bytes.len() {
                return Err(Error::TooLarge);
            }
            bytes.extend_from_slice(&chunk);
        }
        if !status.is_success() {
            let detail = serde_json::from_slice::<ErrorResponse>(&bytes)
                .map(|value| {
                    value
                        .detail
                        .chars()
                        .filter(|char| !char.is_control())
                        .take(512)
                        .collect()
                })
                .unwrap_or_else(|_| "request rejected".into());
            return Err(Error::Http(status, detail));
        }
        serde_json::from_slice(&bytes).map_err(Error::Json)
    }
}

impl Endpoint {
    fn url(&self, segments: &[&str]) -> Result<Url, Error> {
        if segments
            .iter()
            .any(|segment| ["", ".", ".."].contains(segment))
        {
            return Err(Error::Configuration("Empty or relative API path segment"));
        }
        let mut url = self.base.clone();
        url.path_segments_mut()
            .map_err(|_| Error::Configuration("Invalid engine origin"))?
            .clear()
            .extend(segments.iter().copied());
        Ok(url)
    }
}
