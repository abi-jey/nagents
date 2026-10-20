"""Host-owned A2A configuration. Requests cannot supply endpoints or profiles."""

from __future__ import annotations

import ipaddress
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_BROWSER_SCOPES = ("ngn:access", "ngn:approve")


def mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def string(value: object, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be a nonempty bounded string")
    return value


def names(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise ValueError(f"{name} must contain 1 to 64 names")
    items = tuple(string(item, name, maximum=128) for item in value)
    if len(set(items)) != len(items) or any(not re.fullmatch(r"[A-Za-z0-9_.:/-]+", item) for item in items):
        raise ValueError(f"{name} contains invalid or repeated names")
    return items


def endpoint(value: str, *, loopback_http: bool = False) -> str:
    url = urlsplit(value)
    local = url.hostname == "localhost"
    with suppress(ValueError):
        local = local or ipaddress.ip_address(url.hostname or "").is_loopback
    if (
        not url.hostname
        or url.username is not None
        or url.password is not None
        or url.fragment
        or url.query
        or (url.scheme != "https" and not (loopback_http and local and url.scheme == "http"))
    ):
        raise ValueError("A2A endpoints require HTTPS; explicit development HTTP is limited to loopback")
    return value


@dataclass(frozen=True)
class IssuerConfig:
    issuer: str
    audience: str
    algorithms: tuple[str, ...] = ("RS256",)
    validation: str = "jwt"
    token_kind_claim: str = ""
    token_kind_value: str = ""
    scope_claim: str = "scope"
    tenant_claim: str = ""
    role_claim: str = ""
    role_scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    introspection_endpoint: str = ""
    client_id: str = ""
    client_secret_env: str = ""

    def __post_init__(self) -> None:
        endpoint(self.issuer, loopback_http=True)
        string(self.audience, "audience", maximum=256)
        if not self.algorithms or not set(self.algorithms) <= {
            "RS256",
            "RS384",
            "RS512",
            "ES256",
            "ES384",
            "ES512",
            "EdDSA",
        }:
            raise ValueError("Only explicitly allowed asymmetric JWT algorithms are supported")
        if self.validation not in {"jwt", "introspection"}:
            raise ValueError("validation must be jwt or introspection")
        if bool(self.token_kind_claim) != bool(self.token_kind_value):
            raise ValueError("token_kind_claim and token_kind_value must be configured together")
        for claim in (self.token_kind_claim, self.scope_claim, self.tenant_claim, self.role_claim):
            if claim and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", claim):
                raise ValueError("Invalid token claim name")
        if self.client_secret_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.client_secret_env):
            raise ValueError("client_secret_env must name an environment variable")

    @classmethod
    def parse(cls, value: object, *, loopback_http: bool = False) -> IssuerConfig:
        data = mapping(value, "issuer")
        if set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown A2A issuer configuration field")
        issuer = endpoint(string(data.pop("issuer", ""), "issuer"), loopback_http=loopback_http)
        audience = string(data.pop("audience", ""), "audience", maximum=256)
        algorithms = names(data.pop("algorithms", ["RS256"]), "algorithms")
        if not set(algorithms) <= {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"}:
            raise ValueError("Only explicitly allowed asymmetric JWT algorithms are supported")
        roles = mapping(data.pop("role_scopes", {}), "role_scopes")
        if len(roles) > 64:
            raise ValueError("Too many role mappings")
        role_scopes = {
            string(role, "role", maximum=128): names(scopes, "role scopes") for role, scopes in roles.items()
        }
        fields = {key: string(item, key) if item != "" else "" for key, item in data.items()}
        result = cls(
            issuer,
            audience,
            algorithms,
            validation=fields.get("validation", "jwt"),
            token_kind_claim=fields.get("token_kind_claim", ""),
            token_kind_value=fields.get("token_kind_value", ""),
            scope_claim=fields.get("scope_claim", "scope"),
            tenant_claim=fields.get("tenant_claim", ""),
            role_claim=fields.get("role_claim", ""),
            role_scopes=role_scopes,
            introspection_endpoint=fields.get("introspection_endpoint", ""),
            client_id=fields.get("client_id", ""),
            client_secret_env=fields.get("client_secret_env", ""),
        )
        if result.validation not in {"jwt", "introspection"}:
            raise ValueError("validation must be jwt or introspection")
        if bool(result.token_kind_claim) != bool(result.token_kind_value):
            raise ValueError("token_kind_claim and token_kind_value must be configured together")
        if result.validation == "introspection":
            endpoint(result.introspection_endpoint, loopback_http=loopback_http)
            if not result.client_id or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result.client_secret_env):
                raise ValueError("Introspection requires a client ID and a client_secret_env variable name")
        return result


@dataclass(frozen=True)
class ServiceConfig:
    issuer: str
    client_id: str
    scopes: tuple[str, ...]
    api_origin: str
    client_secret_env: str = ""
    resource: str = ""

    def validate(self, *, loopback_http: bool) -> None:
        endpoint(self.issuer, loopback_http=loopback_http)
        endpoint(self.api_origin, loopback_http=loopback_http)
        if urlsplit(self.api_origin).path not in {"", "/"}:
            raise ValueError("Service api_origin must be its fixed API origin")
        string(self.client_id, "service client_id", maximum=256)
        names(list(self.scopes), "service scopes")
        if self.resource:
            endpoint(self.resource, loopback_http=loopback_http)
        if self.client_secret_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.client_secret_env):
            raise ValueError("Service client_secret_env must name an environment variable")

    @classmethod
    def parse(cls, value: object, *, loopback_http: bool) -> ServiceConfig:
        data = mapping(value, "service")
        if set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown downstream service configuration field")
        result = cls(
            string(data.get("issuer", ""), "service issuer"),
            string(data.get("client_id", ""), "service client_id", maximum=256),
            names(data.get("scopes", []), "service scopes"),
            string(data.get("api_origin", ""), "service api_origin"),
            string(data["client_secret_env"], "client_secret_env") if "client_secret_env" in data else "",
            string(data["resource"], "resource") if "resource" in data else "",
        )
        result.validate(loopback_http=loopback_http)
        return result


@dataclass(frozen=True)
class A2AConfig:
    public_url: str
    agent: str
    issuer: IssuerConfig
    data_dir: Path
    name: str = "ngn"
    description: str = "An authenticated ngn agent."
    loopback_http: bool = False
    max_request_bytes: int = 65536
    max_output_bytes: int = 262144
    max_active_per_principal: int = 3
    max_active_tasks: int = 32
    max_retained_tasks: int = 4096
    max_tasks_per_principal: int = 256
    requests_per_minute: int = 120
    max_events_per_task: int = 256
    approval_timeout: int = 300
    browser_client_id: str = ""
    browser_client_secret_env: str = ""
    browser_scopes: tuple[str, ...] = DEFAULT_BROWSER_SCOPES
    services: dict[str, ServiceConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        endpoint(self.public_url, loopback_http=self.loopback_http)
        if urlsplit(self.public_url).path not in {"", "/"}:
            raise ValueError("public_url must be the origin of the separate A2A service")
        endpoint(self.issuer.issuer, loopback_http=self.loopback_http)
        if self.issuer.validation == "introspection":
            endpoint(self.issuer.introspection_endpoint, loopback_http=self.loopback_http)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.agent):
            raise ValueError("Invalid exported agent profile")
        for key in ("name", "description"):
            string(getattr(self, key), key)
        if not isinstance(self.data_dir, Path) or not self.data_dir.is_absolute():
            raise ValueError("data_dir must be an absolute Path")
        if type(self.loopback_http) is not bool:
            raise ValueError("loopback_http must be a boolean")
        names(list(self.browser_scopes), "browser_scopes")
        if len(self.services) > 16:
            raise ValueError("At most sixteen downstream services may be configured")
        for name, service in self.services.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or not isinstance(service, ServiceConfig):
                raise ValueError("Invalid downstream service definition")
            service.validate(loopback_http=self.loopback_http)
        if self.browser_client_secret_env and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.browser_client_secret_env
        ):
            raise ValueError("browser_client_secret_env must name an environment variable")
        for key in (
            "max_request_bytes",
            "max_output_bytes",
            "max_active_per_principal",
            "max_active_tasks",
            "max_retained_tasks",
            "max_tasks_per_principal",
            "requests_per_minute",
            "max_events_per_task",
            "approval_timeout",
        ):
            item = getattr(self, key)
            maximum = 1048576 if key in {"max_request_bytes", "max_output_bytes"} else 4096
            if type(item) is not int or not 1 <= item <= maximum:
                raise ValueError(f"{key} must be an integer from 1 to {maximum}")

    @classmethod
    def from_file(cls, path: Path) -> A2AConfig:
        if path.stat().st_size > 65536:
            raise ValueError("A2A configuration exceeds 64 KiB")
        data = mapping(json.loads(path.read_text(encoding="utf-8")), "A2A configuration")
        if set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown A2A configuration field")
        development = data.pop("loopback_http", False)
        if type(development) is not bool:
            raise ValueError("loopback_http must be a boolean")
        public = endpoint(string(data.pop("public_url", ""), "public_url"), loopback_http=development).rstrip("/")
        if urlsplit(public).path not in {"", "/"}:
            raise ValueError("public_url must be the origin of the separate A2A service")
        agent = string(data.pop("agent", ""), "agent", maximum=128)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", agent):
            raise ValueError("Invalid exported agent profile")
        issuer = IssuerConfig.parse(data.pop("issuer", {}), loopback_http=development)
        directory = Path(string(data.pop("data_dir", ""), "data_dir")).expanduser()
        if not directory.is_absolute():
            directory = path.resolve().parent / directory
        strings = {
            key: string(data.pop(key), key)
            for key in ("name", "description", "browser_client_id", "browser_client_secret_env")
            if key in data
        }
        scopes = names(data.pop("browser_scopes", list(DEFAULT_BROWSER_SCOPES)), "browser_scopes")
        services = {
            name: ServiceConfig.parse(value, loopback_http=development)
            for name, value in mapping(data.pop("services", {}), "services").items()
        }
        limits: dict[str, int] = {}
        for key, item in data.items():
            maximum = 1048576 if key in {"max_request_bytes", "max_output_bytes"} else 4096
            if type(item) is not int or not 1 <= item <= maximum:
                raise ValueError(f"{key} must be an integer from 1 to {maximum}")
            limits[key] = item
        return cls(
            public,
            agent,
            issuer,
            directory,
            name=strings.get("name", "ngn"),
            description=strings.get("description", "An authenticated ngn agent."),
            loopback_http=development,
            max_request_bytes=limits.get("max_request_bytes", 65536),
            max_output_bytes=limits.get("max_output_bytes", 262144),
            max_active_per_principal=limits.get("max_active_per_principal", 3),
            max_active_tasks=limits.get("max_active_tasks", 32),
            max_retained_tasks=limits.get("max_retained_tasks", 4096),
            max_tasks_per_principal=limits.get("max_tasks_per_principal", 256),
            requests_per_minute=limits.get("requests_per_minute", 120),
            max_events_per_task=limits.get("max_events_per_task", 256),
            approval_timeout=limits.get("approval_timeout", 300),
            browser_client_id=strings.get("browser_client_id", ""),
            browser_client_secret_env=strings.get("browser_client_secret_env", ""),
            browser_scopes=scopes,
            services=services,
        )
