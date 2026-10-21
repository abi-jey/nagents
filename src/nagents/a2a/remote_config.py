"""Data-only outbound A2A bindings, independent of optional protocol dependencies."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from dataclasses import dataclass
from urllib.parse import urlsplit

PUBLIC_CLIENT_EXTENSION = "urn:nagents:a2a:oauth-client:v1"


def remote_endpoint(value: str, *, loopback_http: bool = False) -> str:
    """Require an explicit, credential-free HTTPS destination; never infer one."""
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError("A2A endpoint must be a bounded URL")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (
            parsed.scheme != "https"
            and not (loopback_http and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"})
        )
    ):
        raise ValueError("A2A endpoint must use HTTPS without credentials, query or fragment")
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("A2A endpoint has an invalid port") from None
    return value.rstrip("/")


@dataclass(frozen=True)
class RemoteAgentConfig:
    """An agent connection, not a model provider. Credentials never enter Markdown."""

    url: str = ""
    rpc_url: str = ""
    token_env: str = ""
    ca_file: str = ""
    issuer: str = ""
    client_id: str = ""
    scopes: tuple[str, ...] = ("openid",)
    resource: str = ""
    loopback_http: bool = False
    timeout: int = 120
    metadata_url: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            if any(
                (
                    self.rpc_url,
                    self.token_env,
                    self.ca_file,
                    self.issuer,
                    self.client_id,
                    self.resource,
                    self.metadata_url,
                )
            ):
                raise ValueError("A2A connection requires a URL")
            return
        origin = remote_endpoint(self.url, loopback_http=self.loopback_http)
        if urlsplit(origin).path:
            raise ValueError("A2A URL must be the service origin")
        rpc = remote_endpoint(self.rpc_url or f"{origin}/rpc", loopback_http=self.loopback_http)
        if urlsplit(rpc).netloc != urlsplit(origin).netloc or urlsplit(rpc).scheme != urlsplit(origin).scheme:
            raise ValueError("A2A RPC endpoint must use the configured origin")
        if self.token_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", self.token_env):
            raise ValueError("A2A token_env must name a server environment variable")
        if self.issuer:
            remote_endpoint(self.issuer, loopback_http=self.loopback_http)
            if self.token_env:
                raise ValueError("A2A OAuth cannot also use token_env")
            if self.metadata_url:
                remote_endpoint(self.metadata_url, loopback_http=self.loopback_http)
        elif self.client_id or self.resource or self.metadata_url:
            raise ValueError("A2A OAuth settings require an issuer")
        if (
            type(self.loopback_http) is not bool
            or type(self.timeout) is not int
            or not 1 <= self.timeout <= 3600
            or len(self.scopes) > 32
            or any(not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]{1,128}", scope) for scope in self.scopes)
        ):
            raise ValueError("Invalid A2A timeout, transport or scopes")
        for value in (self.ca_file, self.client_id, self.resource):
            if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid A2A connection setting")

    @classmethod
    def parse(cls, value: object) -> RemoteAgentConfig:
        if not isinstance(value, dict) or value.keys() - cls.__dataclass_fields__.keys():
            raise ValueError("a2a must contain only supported connection fields")

        def text(name: str) -> str:
            result = value.get(name, "")
            if not isinstance(result, str):
                raise ValueError("A2A connection fields must be strings")
            return result

        scopes = value.get("scopes", ["openid"])
        if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
            raise ValueError("A2A scopes must be an array of strings")
        loopback, timeout = value.get("loopback_http", False), value.get("timeout", 120)
        if type(loopback) is not bool or type(timeout) is not int:
            raise ValueError("Invalid A2A transport or timeout")
        return cls(
            url=text("url"),
            rpc_url=text("rpc_url"),
            token_env=text("token_env"),
            ca_file=text("ca_file"),
            issuer=text("issuer"),
            client_id=text("client_id"),
            resource=text("resource"),
            metadata_url=text("metadata_url"),
            scopes=tuple(scopes),
            loopback_http=loopback,
            timeout=timeout,
        )

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def as_dict(self) -> dict[str, object]:
        values = asdict(self)
        values["scopes"] = list(self.scopes)
        return values

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()
