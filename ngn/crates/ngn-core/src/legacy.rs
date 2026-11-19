//! Read-only compatibility with the Python ngn connection files.
//!
//! Without `--config`, `ngn` reads Python ngn's provider and model settings:
//! `providers.json` (global) overlaid by `workspaces/<id>/providers.json`,
//! and the model binding from `assistant.md` (workspace, then global), falling
//! back to `config.json`'s `model`. It also reads the personal `animations`
//! preference. Nothing is written, and credential values are never read here:
//! only environment-variable names and login locations.

use std::{
    collections::BTreeMap,
    env,
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
    /// An inherited permission ceiling; never authorizes native mutations.
    pub read_only: bool,
    /// A saved personal TUI preference, when set to a JSON boolean.
    pub animations: Option<bool>,
}

/// Implicit Python settings and login locations, injectable in tests.
#[derive(Default)]
pub(crate) struct Environment {
    python_chatgpt: Option<PathBuf>,
    /// `$CODEX_HOME`, else `~/.codex`.
    codex: Option<PathBuf>,
    read_only: bool,
    provider_id: Option<String>,
}

pub(crate) fn python_data_directory() -> Option<PathBuf> {
    env::var_os("XDG_DATA_HOME")
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
        .or_else(|| env::home_dir().map(|home| home.join(".local/share")))
        .map(|base| base.join("ngn"))
}

impl Environment {
    pub(crate) fn discover() -> Result<Self> {
        let home = env::home_dir();
        let codex = env::var_os("CODEX_HOME")
            .filter(|path| !path.is_empty())
            .map(PathBuf::from)
            .map(|path| match (&home, path.strip_prefix("~")) {
                (Some(home), Ok(rest)) => home.join(rest),
                _ => path,
            })
            .or_else(|| home.map(|home| home.join(".codex")));
        let read_only = match env::var("NGN_READ_ONLY") {
            Ok(value) => match value.to_ascii_lowercase().as_str() {
                "true" | "1" => true,
                "false" | "0" => false,
                _ => bail!("NGN_READ_ONLY must be true/false or 1/0"),
            },
            Err(env::VarError::NotPresent) => false,
            Err(env::VarError::NotUnicode(_)) => bail!("NGN_READ_ONLY must be true/false or 1/0"),
        };
        let provider_id = match env::var("NGN_PROVIDER_ID") {
            Ok(value) => Some(value),
            Err(env::VarError::NotPresent) => None,
            Err(env::VarError::NotUnicode(_)) => bail!("NGN_PROVIDER_ID must be a provider name"),
        };
        let python_data = python_data_directory();
        ensure!(
            python_data.as_ref().is_none_or(|path| path.is_absolute()),
            "XDG_DATA_HOME must be an absolute path to discover Python ngn accounts"
        );
        Ok(Self {
            python_chatgpt: python_data.map(|path| path.join("auth/openai.json")),
            codex,
            read_only,
            provider_id,
        })
    }

    fn python_chatgpt_may_exist(&self) -> bool {
        self.python_chatgpt
            .as_deref()
            .is_some_and(login_file_may_exist)
    }

    fn codex_exists(&self) -> Result<bool> {
        let Some(home) = &self.codex else {
            return Ok(false);
        };
        ensure!(
            home.is_absolute(),
            "Set CODEX_HOME to an absolute path before using Codex discovery in Rust"
        );
        Ok(login_file_may_exist(&home.join("auth.json"))
            || login_file_may_exist(&home.join("config.toml")))
    }
}

fn login_file_may_exist(path: &Path) -> bool {
    // Do not open credential files. Anything except a confirmed absence must
    // not cause auto routing to switch to another account.
    !matches!(path.symlink_metadata(), Err(error) if error.kind() == std::io::ErrorKind::NotFound)
}

/// Python's workspace overlay identifier: the resolved path's SHA-256, 32 hex digits.
pub(crate) fn workspace_id(workspace: &Path) -> String {
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
    let mut document = text.lines();
    if document.next() != Some("---") {
        return Ok(Default::default());
    }
    let mut lines = document.take_while(|line| *line != "---").peekable();
    while let Some(line) = lines.next() {
        let Some(value) = line.strip_prefix("model:") else {
            continue;
        };
        if !value.trim().is_empty() {
            ensure!(
                !value.trim_start().starts_with('{'),
                "The inline model binding in {} is not supported by Rust; select the intended provider with an explicit --config FILE",
                path.display()
            );
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
        ensure!(
            provider.is_empty() || !id.is_empty(),
            "The model binding in {} is missing its model ID",
            path.display()
        );
        return Ok((provider, id));
    }
    Ok(Default::default())
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct UserSettings {
    read_only: bool,
    provider_id: Option<String>,
    model: Option<ConfigModel>,
    providers: serde_json::Value,
}

#[derive(Default, Deserialize)]
struct PersonalPreferences {
    animations: Option<bool>,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum ConfigModel {
    Id(String),
    Binding { provider: String, id: String },
}

fn user_settings(path: &Path) -> Result<(UserSettings, PersonalPreferences)> {
    let Some(bytes) = read_bounded(path)? else {
        return Ok(Default::default());
    };
    let settings: UserSettings = serde_json::from_slice(&bytes)
        .with_context(|| format!("Invalid Python ngn settings in {}", path.display()))?;
    ensure!(
        settings.providers.is_null()
            || settings
                .providers
                .as_object()
                .is_some_and(serde_json::Map::is_empty),
        "Python ngn settings in {} reference a custom provider registry. Pass an explicit Rust --config FILE for the intended account",
        path.display()
    );
    // Read only the cosmetic field in a separate projection so malformed or
    // duplicate animation values do not change provider or permission loading.
    let preferences = serde_json::from_slice(&bytes).unwrap_or_default();
    Ok((settings, preferences))
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

fn map(profile: &Profile, model: &str, environment: &Environment) -> Result<ProviderConfig> {
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
    let native_chatgpt = "use an explicit Rust --config FILE with kind openai and auth chatgpt, then run ngn --config FILE auth login and sign in to the intended account";
    let auth = match (profile.auth.as_str(), openai) {
        ("api-key", _) | ("auto", false) => ProviderAuth::ApiKey,
        ("chatgpt", true) => bail!(
            "this connection requires ChatGPT. Rust does not reuse Python ngn credentials; {native_chatgpt}"
        ),
        ("auto", true) if environment.python_chatgpt_may_exist() => bail!(
            "Python ngn's saved ChatGPT login takes precedence over Codex and API keys. Rust does not reuse it or verify the account of an existing native login; {native_chatgpt}"
        ),
        ("codex", true) if !environment.codex_exists()? => {
            bail!("local Codex configuration was not found. Sign in with Codex or check CODEX_HOME")
        }
        ("codex", true) => ProviderAuth::Codex,
        ("auto", true) if environment.codex_exists()? => ProviderAuth::Codex,
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
                profile
                    .api_key_env
                    .strip_prefix("${")
                    .and_then(|name| name.strip_suffix('}'))
                    .unwrap_or(&profile.api_key_env)
                    .into()
            },
            auth,
        },
        ProviderAuth::Chatgpt | ProviderAuth::Codex => ProviderConfig {
            kind: kind.into(),
            model: model.into(),
            // Python's Codex route takes its wire API from local Codex settings,
            // not from the named connection's API-key settings.
            api: "auto".into(),
            base_url: String::new(),
            api_key_env: String::new(),
            auth,
        },
    })
}

pub(crate) fn select(
    directory: &Path,
    workspace: &Path,
    environment: &Environment,
) -> Result<Selection> {
    let (settings, preferences) = user_settings(&directory.join("config.json"))?;
    let mut selection = Selection {
        read_only: settings.read_only || environment.read_only,
        animations: preferences.animations,
        ..Selection::default()
    };
    let local_directory = directory.join("workspaces").join(workspace_id(workspace));
    let global_path = directory.join("providers.json");
    let local_path = local_directory.join("providers.json");
    let global = registry(&global_path)?;
    let local = registry(&local_path)?;
    if global.is_none() && local.is_none() {
        return Ok(selection);
    }
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
    let (mut provider, mut model) = binding(&local_directory.join("assistant.md"))?;
    let workspace_model = !model.is_empty();
    if !workspace_model {
        (provider, model) = binding(&directory.join("assistant.md"))?;
    }
    if provider.is_empty() {
        provider = active;
    }
    if let Some(provider_id) = &environment.provider_id {
        provider.clone_from(provider_id);
    }
    match settings.model {
        Some(ConfigModel::Binding {
            provider: binding,
            id,
        }) if !workspace_model => {
            provider = binding;
            model = id;
        }
        Some(ConfigModel::Binding { .. }) => (),
        model_setting => {
            if let Some(provider_id) = settings.provider_id {
                provider = provider_id;
            }
            if !workspace_model && let Some(ConfigModel::Id(id)) = model_setting {
                model = id;
            }
        }
    }
    if model.is_empty() {
        model = DEFAULT_MODEL.into();
    }
    selection.provider = provider;
    for (name, profile) in &profiles {
        match map(profile, &model, environment) {
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
    Ok(selection)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn environment(root: &Path) -> Environment {
        Environment {
            python_chatgpt: Some(root.join("python-ngn/auth/openai.json")),
            codex: Some(root.join("codex")),
            ..Environment::default()
        }
    }

    fn write(path: &Path, text: &str) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }

    #[test]
    fn read_only_is_inherited_even_without_a_registry() {
        let root = tempfile::tempdir().unwrap();
        write(&root.path().join("config.json"), r#"{"read_only":true}"#);
        let env = environment(root.path());
        let selected = select(root.path(), root.path(), &env).unwrap();
        assert!(selected.providers.is_empty() && selected.read_only);
        write(&root.path().join("config.json"), r#"{"read_only":false}"#);
        let env = Environment {
            read_only: true,
            ..env
        };
        assert!(select(root.path(), root.path(), &env).unwrap().read_only);
        write(&root.path().join("config.json"), r#"{"read_only":"true"}"#);
        assert!(select(root.path(), root.path(), &env).is_err());
    }

    #[test]
    fn personal_animations_is_optional_and_does_not_require_a_registry() {
        let root = tempfile::tempdir().unwrap();
        let env = environment(root.path());
        let path = root.path().join("config.json");
        assert_eq!(
            select(root.path(), root.path(), &env).unwrap().animations,
            None
        );
        for (value, expected) in [
            ("false", Some(false)),
            ("true", Some(true)),
            ("null", None),
            (r#""false""#, None),
            ("0", None),
        ] {
            write(
                &path,
                &format!(r#"{{"animations":{value},"read_only":true}}"#),
            );
            let selected = select(root.path(), root.path(), &env).unwrap();
            assert_eq!(selected.animations, expected);
            assert!(selected.read_only && selected.providers.is_empty());
        }
        write(
            &path,
            r#"{"animations":true,"animations":false,"read_only":true}"#,
        );
        let selected = select(root.path(), root.path(), &env).unwrap();
        assert_eq!(selected.animations, None);
        assert!(selected.read_only);
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
        write(&root.path().join("codex/config.toml"), "");
        let selection = select(root.path(), &workspace, &environment(root.path())).unwrap();
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
        let selection = select(root.path(), root.path(), &environment(root.path())).unwrap();
        assert_eq!(selection.provider, "key");
        assert_eq!(selection.providers["key"].model, DEFAULT_MODEL);
        assert_eq!(selection.providers["key"].kind, "gemini_native");
        assert_eq!(selection.providers["key"].api_key_env, "MY_KEY");
        write(
            &root.path().join("config.json"),
            r#"{"model":"from-config"}"#,
        );
        let selection = select(root.path(), root.path(), &environment(root.path())).unwrap();
        assert_eq!(selection.providers["key"].model, "from-config");
    }

    #[test]
    fn openai_routing_preserves_python_precedence_without_reusing_python_chatgpt_credentials() {
        let root = tempfile::tempdir().unwrap();
        write(
            &root.path().join("providers.json"),
            r#"{"version":2,"revision":"r","active":"auto","providers":{
                "auto":{"kind":"openai","auth":"auto","api_key_env":"${NAMED_KEY}"},
                "chatgpt":{"kind":"openai","auth":"chatgpt"},
                "codex":{"kind":"openai","auth":"codex"},
                "key":{"kind":"openai","auth":"api-key"}}}"#,
        );
        let env = environment(root.path());
        let discover = || select(root.path(), root.path(), &env).unwrap();
        write(&root.path().join("ngn-rust/auth/openai.json"), "native");
        let selected = discover();
        assert_eq!(selected.providers["auto"].auth, ProviderAuth::ApiKey);
        assert_eq!(selected.providers["auto"].api_key_env, "NAMED_KEY");
        assert!(selected.unavailable["codex"].contains("CODEX_HOME"));
        assert!(selected.unavailable["chatgpt"].contains("auth login"));
        write(&root.path().join("codex/auth.json"), "{}");
        assert_eq!(discover().providers["auto"].auth, ProviderAuth::Codex);
        let legacy_chatgpt = env.python_chatgpt.as_deref().unwrap();
        write(legacy_chatgpt, "private fixture");
        let selected = discover();
        assert!(!selected.providers.contains_key("auto"));
        assert!(selected.unavailable["auto"].contains("takes precedence"));
        assert_eq!(selected.providers["codex"].auth, ProviderAuth::Codex);
        assert_eq!(selected.providers["key"].auth, ProviderAuth::ApiKey);
        assert_eq!(
            std::fs::read_to_string(legacy_chatgpt).unwrap(),
            "private fixture"
        );
    }

    #[test]
    fn malformed_or_unknown_registries_fail_closed() {
        let root = tempfile::tempdir().unwrap();
        write(
            &root.path().join("providers.json"),
            r#"{"version":3,"revision":"r","active":"","providers":{}}"#,
        );
        assert!(select(root.path(), root.path(), &environment(root.path())).is_err());
        write(
            &root.path().join("providers.json"),
            r#"{"version":2,"revision":"r","active":"","providers":{},"extra":1}"#,
        );
        assert!(select(root.path(), root.path(), &environment(root.path())).is_err());
    }
}
