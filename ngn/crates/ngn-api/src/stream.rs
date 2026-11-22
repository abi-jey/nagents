use std::time::Duration;

use futures_util::StreamExt;
use tokio::net::TcpStream;
use tokio_tungstenite::{
    MaybeTlsStream, WebSocketStream, connect_async_with_config,
    tungstenite::{
        Message,
        client::IntoClientRequest,
        http::{HeaderValue, header},
        protocol::WebSocketConfig,
    },
};

use crate::{
    Client, EVENTS_PATH, EVENTS_SUBPROTOCOL, Error, PROTOCOL_VERSION, models::EventRecord,
};

const MAX_FRAME: usize = 1024 * 1024;

pub struct Events(WebSocketStream<MaybeTlsStream<TcpStream>>);

impl Client {
    pub async fn subscribe(&self, after: u64, session_id: Option<&str>) -> Result<Events, Error> {
        let endpoint = self.endpoint();
        let mut url = endpoint.url(
            &EVENTS_PATH
                .trim_start_matches('/')
                .split('/')
                .collect::<Vec<_>>(),
        )?;
        let scheme = if url.scheme() == "https" { "wss" } else { "ws" };
        url.set_scheme(scheme)
            .map_err(|()| Error::Configuration("Invalid engine event URL"))?;
        url.query_pairs_mut()
            .append_pair("after", &after.to_string());
        if let Some(session) = session_id {
            url.query_pairs_mut().append_pair("session_id", session);
        }
        let mut request = url.as_str().into_client_request().map_err(socket_error)?;
        let mut authorization = HeaderValue::from_str(&format!("Bearer {}", endpoint.token))
            .map_err(|_| Error::Configuration("Invalid engine token"))?;
        authorization.set_sensitive(true);
        request
            .headers_mut()
            .insert(header::AUTHORIZATION, authorization);
        request.headers_mut().insert(
            header::SEC_WEBSOCKET_PROTOCOL,
            HeaderValue::from_static(EVENTS_SUBPROTOCOL),
        );
        let config = WebSocketConfig::default()
            .max_message_size(Some(MAX_FRAME))
            .max_frame_size(Some(MAX_FRAME));
        let (stream, response) = tokio::time::timeout(
            Duration::from_secs(10),
            connect_async_with_config(request, Some(config), false),
        )
        .await
        .map_err(|_| Error::Configuration("Engine event connection timed out"))?
        .map_err(socket_error)?;
        if response
            .headers()
            .get(header::SEC_WEBSOCKET_PROTOCOL)
            .is_none_or(|value| value != EVENTS_SUBPROTOCOL)
        {
            return Err(Error::Configuration("Engine refused the event subprotocol"));
        }
        Ok(Events(stream))
    }
}

impl Events {
    pub async fn next(&mut self) -> Result<Option<EventRecord>, Error> {
        while let Some(message) = self.0.next().await {
            match message.map_err(socket_error)? {
                Message::Text(text) => {
                    #[derive(serde::Deserialize)]
                    struct WireVersion {
                        schema_version: u64,
                    }

                    let version = serde_json::from_str::<WireVersion>(&text)
                        .map_err(Error::Json)?
                        .schema_version;
                    if version != PROTOCOL_VERSION {
                        return Err(Error::Incompatible {
                            client: PROTOCOL_VERSION,
                            engine: version,
                        });
                    }
                    return serde_json::from_str(&text).map(Some).map_err(Error::Json);
                }
                Message::Close(_) => return Ok(None),
                Message::Binary(_) => {
                    return Err(Error::Configuration("Unexpected binary engine event"));
                }
                _ => (),
            }
        }
        Ok(None)
    }
}

fn socket_error(error: tokio_tungstenite::tungstenite::Error) -> Error {
    Error::WebSocket(Box::new(error))
}
