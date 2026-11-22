//! `ngn serve`: the embedded browser client and its same-origin gateway to the
//! Python engine. The browser boundary below applies to every route.

use axum::{
    Json,
    body::Body,
    extract::State,
    http::{HeaderMap, HeaderValue, Method, Request, StatusCode, Uri, header::CONTENT_TYPE},
    middleware::Next,
    response::{IntoResponse, Response},
};
use serde_json::json;
use subtle::ConstantTimeEq;

mod engine;
pub(crate) use engine::serve_python;

#[derive(Clone)]
struct BrowserBoundary {
    authority: String,
    token: String,
    events_path: &'static str,
}

struct ApiError(StatusCode, String);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"detail":self.1}))).into_response()
    }
}

fn exactly(headers: &HeaderMap, name: &str, expected: &str) -> bool {
    let values: Vec<_> = headers.get_all(name).iter().collect();
    values.len() == 1 && values[0].as_bytes() == expected.as_bytes()
}

fn token_matches(value: &[u8], expected: &str) -> bool {
    bool::from(value.ct_eq(expected.as_bytes()))
}

async fn boundary(
    State(state): State<BrowserBoundary>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let rejected = |status, detail: &str| ApiError(status, detail.into()).into_response();
    let path = request.uri().path();
    let method = request.method();
    let headers = request.headers();
    let origin = format!("http://{}", state.authority);
    let navigation = path == "/"
        && method == Method::GET
        && exactly(headers, "sec-fetch-mode", "navigate")
        && exactly(headers, "sec-fetch-dest", "document");
    let websocket = path == state.events_path;
    let history_query = state.events_path == ngn_api::EVENTS_PATH
        && path.starts_with("/api/v1/sessions/")
        && path.ends_with("/history");
    let valid = if !exactly(headers, "host", &state.authority) {
        Err((
            StatusCode::FORBIDDEN,
            "Untrusted Host; use the exact loopback address printed by ngn",
        ))
    } else if !navigation
        && (headers
            .get("sec-fetch-site")
            .is_some_and(|value| ![b"same-origin".as_slice(), b"none"].contains(&value.as_bytes()))
            || (headers.contains_key("origin") && !exactly(headers, "origin", &origin)))
    {
        Err((StatusCode::FORBIDDEN, "Same-origin requests only"))
    } else if ![Method::GET, Method::HEAD, Method::POST].contains(method) {
        Err((StatusCode::METHOD_NOT_ALLOWED, "Method not supported"))
    } else if request.uri().query().is_some()
        && !websocket
        && !history_query
        && !path.ends_with("/events")
    {
        Err((
            StatusCode::BAD_REQUEST,
            "Query parameters are not supported on this route",
        ))
    } else if websocket {
        let protocols: Vec<_> = headers
            .get_all("sec-websocket-protocol")
            .iter()
            .filter_map(|value| value.to_str().ok())
            .flat_map(|value| value.split(',').map(str::trim))
            .collect();
        if protocols.len() != 2
            || !protocols.contains(&ngn_api::EVENTS_SUBPROTOCOL)
            || protocols
                .iter()
                .filter(|protocol| {
                    protocol
                        .strip_prefix("ngn.token.")
                        .is_some_and(|token| token_matches(token.as_bytes(), &state.token))
                })
                .count()
                != 1
            || !exactly(headers, "origin", &origin)
        {
            Err((
                StatusCode::FORBIDDEN,
                "Invalid WebSocket origin or protocol token",
            ))
        } else {
            Ok(())
        }
    } else if path.starts_with("/api/")
        && !(path == "/api/bootstrap" && method == Method::GET)
        && (headers.get_all("x-ngn-token").iter().count() != 1
            || !headers
                .get("x-ngn-token")
                .is_some_and(|value| token_matches(value.as_bytes(), &state.token)))
    {
        Err((
            StatusCode::FORBIDDEN,
            "Invalid host token; reconnect to this ngn instance",
        ))
    } else if method == Method::POST
        && (!exactly(headers, "origin", &origin)
            || !headers.get("content-type").is_some_and(|value| {
                [
                    b"application/json".as_slice(),
                    b"application/json; charset=utf-8",
                ]
                .contains(&value.as_bytes())
            }))
    {
        Err((
            StatusCode::FORBIDDEN,
            "Mutations require exact Origin and application/json",
        ))
    } else {
        Ok(())
    };
    let mut response = match valid {
        Ok(()) => next.run(request).await,
        Err((status, detail)) => rejected(status, detail),
    };
    for (name, value) in [
        ("cache-control", "no-store"),
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("cross-origin-resource-policy", "same-origin"),
        (
            "content-security-policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        ),
    ] {
        response
            .headers_mut()
            .insert(name, HeaderValue::from_static(value));
    }
    response
}

include!(concat!(env!("OUT_DIR"), "/web_assets.rs"));

async fn static_file(uri: Uri) -> Response {
    match embedded_asset(uri.path()) {
        Some((content_type, bytes)) => ([(CONTENT_TYPE, content_type)], bytes).into_response(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}
