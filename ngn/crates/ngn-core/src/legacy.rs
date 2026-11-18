//! Read-only compatibility with the Python ngn connection files.
//!
//! When neither `--config` nor `rust.json` exists, `ngn` selects the same
//! provider connection and model the Python ngn did:
//! `providers.json` (global) overlaid by `workspaces/<id>/providers.json`,
//! and the model binding from `assistant.md` (workspace, then global), falling
//! back to `config.json`'s `model`. Nothing is written, and credential values
//! are never read here: only environment-variable names and login locations.

use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail, ensure};
use ngn_protocol::{ProviderAuth, ProviderConfig};
use serde::Deserialize;
use sha2::{Digest, Sha256};

/// Same bound as the Python registry.
const MAX_BYTES: u64 = 64 * 1024;
/// Python's `DEFAULT_HARNESS_MODEL`, used when nothing selects a model.
pub const DEFAULT_MODEL: &str = "gpt-6-astra";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Registry {
    version: u32,
    #[allow(dead_code)]
    revision: String,
    active: String,
    providers: BTreeMap<String, Profile>,
}

/// Only the routing fields; Python-only tuning fields are accepted and ignored.
#[derive(Deserialize)]
struct Profile {
    kind: String,
    #[serde(default = "default_auth")]
    auth: String,
    #[serde(default)]
    base_url: String,
    #[serde(default = "default_api")]
    api: String,
    #[serde(default)]
    api_key_env: String,
}

fn default_auth() -> String {
    "api-key".into()
}
fn default_api() -> String {
    "auto".into()
}

/// What the legacy files select, already mapped to Rust provider settings.
#[derive(Debug, Default, Clone)]
pub struct Selection {
    pub provider: String,
    pub providers: BTreeMap<String, ProviderConfig>,
    /// Connections that exist but cannot run in the Rust host, with the reason.
    pub unavailable: BTreeMap<String, String>,
    /// Files that contributed, for diagnostics.
    pub sources: Vec<PathBuf>,
}

/// Where local logins live; injectable so tests never inspect the real home.
pub struct Logins {
    /// The Rust host's own ChatGPT device-login store.
    pub chatgpt: PathBuf,
    /// `$CODEX_HOME`, else `~/.codex`.
    pub codex: Option<PathBuf>,
}

impl Logins {
    pub fn discover(chatgpt: PathBuf) -> Self {
        let codex = std::env::var_os("CODEX_HOME")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".codex")));
        Self { chatgpt, codex }
    }

    fn codex_exists(&self) -> bool {
        self.codex.as_ref().is_some_and(|home| {
            home.join("auth.json").exists() || home.join("config.toml").exists()
        })
    }
}

/// Python's workspace overlay identifier: the resolved path's SHA-256, 32 hex digits.
fn workspace_id(workspace: &Path) -> String {
    let digest = format!(
        "{:x}",
        Sha256::digest(workspace.to_string_lossy().as_bytes())
    );
    digest[..32].to_owned()
}

fn read_bounded(path: &Path) -> Result<Option<Vec<u8>>> {
    let metadata = match path.symlink_metadata() {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(error).with_context(|| format!("Cannot inspect {}", path.display()));
        }
    };
    ensure!(
        metadata.is_file() && metadata.len() <= MAX_BYTES,
        "{} must be a regular file of at most 64 KiB",
        path.display()
    );
    Ok(Some(std::fs::read(path).with_context(|| {
        format!("Cannot read {}", path.display())
    })?))
}

fn registry(path: &Path) -> Result<Option<Registry>> {
    let Some(bytes) = read_bounded(path)? else {
        return Ok(None);
    };
    let registry: Registry = serde_json::from_slice(&bytes)
        .with_context(|| format!("{} is not a valid ngn provider registry", path.display()))?;
    ensure!(
        registry.version == 2,
        "{} uses unsupported provider registry version {}",
        path.display(),
        registry.version
    );
    Ok(Some(registry))
}

fn unquote(value: &str) -> String {
    let value = value.trim();
    for quote in ['"', '\''] {
        if let Some(inner) = value
            .strip_prefix(quote)
            .and_then(|rest| rest.strip_suffix(quote))
        {
            return inner.to_owned();
        }
    }
    value.to_owned()
}

/// The `model: {provider, id}` binding from an assistant document's front matter.
fn binding(path: &Path) -> Result<(String, String)> {
    let Some(bytes) = read_bounded(path)? else {
        return Ok(Default::default());
    };
    let text = String::from_utf8_lossy(&bytes);
    let Some(rest) = text.strip_prefix("---\n") else {
        return Ok(Default::default());
    };
    let front = rest.split("\n---").next().unwrap_or_default();
    let mut lines = front.lines().peekable();
    while let Some(line) = lines.next() {
        let Some(value) = line.strip_prefix("model:") else {
            continue;
        };
        if !value.trim().is_empty() {
            return Ok((String::new(), unquote(value)));
        }
        let (mut provider, mut id) = (String::new(), String::new());
        while let Some(next) = lines.peek() {
            if !next.starts_with([' ', '\t']) {
                break;
            }
            let entry = lines.next().unwrap_or_default().trim();
            if let Some(value) = entry.strip_prefix("provider:") {
                provider = unquote(value);
            } else if let Some(value) = entry.strip_prefix("id:") {
                id = unquote(value);
            }
        }
        return Ok((provider, id));
    }
    Ok(Default::default())
}

fn config_model(path: &Path) -> Result<String> {
    let Some(bytes) = read_bounded(path)? else {
        return Ok(String::new());
    };
    let value: serde_json::Value = serde_json::from_slice(&bytes)
        .with_context(|| format!("{} is not valid JSON", path.display()))?;
    Ok(match value.get("model") {
        Some(serde_json::Value::String(model)) => model.trim().to_owned(),
        Some(serde_json::Value::Object(binding)) => binding
            .get("id")
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
            .trim()
            .to_owned(),
        _ => String::new(),
    })
}

fn default_key(kind: &str) -> &'static str {
    match kind {
        "anthropic" => "ANTHROPIC_API_KEY",
        "gemini" | "gemini_native" => "GEMINI_API_KEY",
        "openrouter" => "OPENROUTER_API_KEY",
        "litellm" => "LITELLM_API_KEY",
        "azure_openai_compatible_v1" => "AZURE_OPENAI_API_KEY",
        _ => "OPENAI_API_KEY",
    }
}

fn map(profile: &Profile, model: &str, logins: &Logins) -> Result<ProviderConfig> {
    let kind = match profile.kind.as_str() {
        "gemini" => "gemini_native",
        "openai"
        | "openai_compatible"
        | "anthropic"
        | "openrouter"
        | "litellm"
        | "azure_openai_compatible_v1" => profile.kind.as_str(),
        other => bail!("provider kind {other} is not supported by the Rust host yet"),
    };
    let openai = kind == "openai";
    let auth = match (profile.auth.as_str(), openai) {
        ("api-key", _) | ("auto", false) => ProviderAuth::ApiKey,
        ("chatgpt", true) => ProviderAuth::Chatgpt,
        ("codex", true) => ProviderAuth::Codex,
        // Python order: an existing ChatGPT login, then a local Codex login, then the key.
        ("auto", true) if logins.chatgpt.exists() => ProviderAuth::Chatgpt,
        ("auto", true) if logins.codex_exists() => ProviderAuth::Codex,
        ("auto", true) => ProviderAuth::ApiKey,
        (other, _) => bail!(
            "{other} authentication for {} is not supported by the Rust host yet",
            profile.kind
        ),
    };
    Ok(match auth {
        ProviderAuth::ApiKey => ProviderConfig {
            kind: kind.into(),
            model: model.into(),
            api: profile.api.clone(),
            base_url: profile.base_url.clone(),
            api_key_env: if profile.api_key_env.is_empty() {
                default_key(&profile.kind).into()
            } else {
                profile.api_key_env.clone()
            },
            auth,
        },
        ProviderAuth::Chatgpt | ProviderAuth::Codex => ProviderConfig {
            kind: kind.into(),
            model: model.into(),
            api: profile.api.clone(),
            base_url: String::new(),
            api_key_env: String::new(),
            auth,
        },
    })
}

/// `None` when no legacy registry exists, so built-in defaults stay untouched.
pub fn select(directory: &Path, workspace: &Path, logins: &Logins) -> Result<Option<Selection>> {
    let local_directory = directory.join("workspaces").join(workspace_id(workspace));
    let global_path = directory.join("providers.json");
    let local_path = local_directory.join("providers.json");
    let global = registry(&global_path)?;
    let local = registry(&local_path)?;
    if global.is_none() && local.is_none() {
        return Ok(None);
    }
    let mut selection = Selection::default();
    let mut profiles = BTreeMap::new();
    let mut active = String::new();
    for (path, registry) in [(global_path, global), (local_path, local)] {
        if let Some(registry) = registry {
            selection.sources.push(path);
            if !registry.active.is_empty() {
                active = registry.active;
            }
            profiles.extend(registry.providers);
        }
    }
    let mut chosen = binding(&local_directory.join("assistant.md"))?;
    if chosen.1.is_empty() {
        chosen = binding(&directory.join("assistant.md"))?;
    }
    let (bound_provider, mut model) = chosen;
    if model.is_empty() {
        model = config_model(&directory.join("config.json"))?;
    }
    if model.is_empty() {
        model = DEFAULT_MODEL.into();
    }
    selection.provider = if bound_provider.is_empty() {
        active
    } else {
        bound_provider
    };
    for (name, profile) in &profiles {
        match map(profile, &model, logins) {
            Ok(provider) => {
                selection.providers.insert(name.clone(), provider);
            }
            Err(error) => {
                selection
                    .unavailable
                    .insert(name.clone(), error.to_string());
            }
        }
    }
    Ok(Some(selection))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn logins(root: &Path) -> Logins {
        Logins {
            chatgpt: root.join("absent-chatgpt.json"),
            codex: Some(root.join("codex")),
        }
    }

    fn write(path: &Path, text: &str) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }

    #[test]
    fn no_registry_means_no_selection() {
        let root = tempfile::tempdir().unwrap();
        assert!(
            select(root.path(), root.path(), &logins(root.path()))
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn workspace_binding_and_overlay_win_like_python() {
        let root = tempfile::tempdir().unwrap();
        let workspace = root.path().join("project");
        std::fs::create_dir(&workspace).unwrap();
        write(
            &root.path().join("providers.json"),
            r#"{"version":2,"revision":"r","active":"global-key","providers":{
                "global-key":{"kind":"anthropic","auth":"api-key","api":"auto","api_key_env":"","request_timeout":120.0},
                "sub":{"kind":"openai","auth":"codex","api":"auto","api_key_env":"OPENAI_API_KEY"},
                "azure":{"kind":"foundry","auth":"entra","base_url":"https://example.invalid"}}}"#,
        );
        write(
            &root.path().join("config.json"),
            r#"{"model":"config-model"}"#,
        );
        write(
            &root.path().join("assistant.md"),
            "---\nname: assistant\nmodel:\n  provider: global-key\n  id: global-model\n---\nbody\n",
        );
        let local = root
            .path()
            .join("workspaces")
            .join(workspace_id(&workspace));
        write(
            &local.join("assistant.md"),
            "---\nname: assistant\nmodel:\n  provider: sub\n  id: \"gpt-6.1-sol\"\n---\n",
        );
        let selection = select(root.path(), &workspace, &logins(root.path()))
            .unwrap()
            .unwrap();
        assert_eq!(selection.provider, "sub");
        let sub = &selection.providers["sub"];
        assert_eq!(sub.auth, ProviderAuth::Codex);
        assert_eq!(sub.model, "gpt-6.1-sol");
        assert!(sub.api_key_env.is_empty());
        let key = &selection.providers["global-key"];
        assert_eq!(key.auth, ProviderAuth::ApiKey);
        assert_eq!(key.api_key_env, "ANTHROPIC_API_KEY");
        assert!(selection.unavailable["azure"].contains("not supported"));
    }

    #[test]
    fn model_falls_back_to_config_then_default_and_active_selects() {
        let root = tempfile::tempdir().unwrap();
        write(
            &root.path().join("providers.json"),
            r#"{"version":2,"revision":"r","active":"key","providers":{"key":{"kind":"gemini","api_key_env":"MY_KEY"}}}"#,
        );
        let selection = select(root.path(), root.path(), &logins(root.path()))
            .unwrap()
            .unwrap();
        assert_eq!(selection.provider, "key");
        assert_eq!(selection.providers["key"].model, DEFAULT_MODEL);
        assert_eq!(selection.providers["key"].kind, "gemini_native");
        assert_eq!(selection.providers["key"].api_key_env, "MY_KEY");
        write(
            &root.path().join("config.json"),
            r#"{"model":"from-config"}"#,
        );
        let selection = select(root.path(), root.path(), &logins(root.path()))
            .unwrap()
            .unwrap();
        assert_eq!(selection.providers["key"].model, "from-config");
    }

    #[test]
    fn openai_auto_prefers_chatgpt_then_codex_then_key() {
        let root = tempfile::tempdir().unwrap();
        write(
            &root.path().join("providers.json"),
            r#"{"version":2,"revision":"r","active":"o","providers":{"o":{"kind":"openai","auth":"auto"}}}"#,
        );
        let mut logins = logins(root.path());
        let auth = |logins: &Logins| {
            select(root.path(), root.path(), logins)
                .unwrap()
                .unwrap()
                .providers["o"]
                .auth
        };
        assert_eq!(auth(&logins), ProviderAuth::ApiKey);
        write(&root.path().join("codex/auth.json"), "{}");
        assert_eq!(auth(&logins), ProviderAuth::Codex);
        logins.chatgpt = root.path().join("codex/auth.json");
        assert_eq!(auth(&logins), ProviderAuth::Chatgpt);
    }

    #[test]
    fn malformed_or_unknown_registries_fail_closed() {
        let root = tempfile::tempdir().unwrap();
        write(
            &root.path().join("providers.json"),
            r#"{"version":3,"revision":"r","active":"","providers":{}}"#,
        );
        assert!(select(root.path(), root.path(), &logins(root.path())).is_err());
        write(
            &root.path().join("providers.json"),
            r#"{"version":2,"revision":"r","active":"","providers":{},"extra":1}"#,
        );
        assert!(select(root.path(), root.path(), &logins(root.path())).is_err());
    }
}
