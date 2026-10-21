"""Discover public remote connection metadata without sending credentials."""

from __future__ import annotations

import re
import ssl
from dataclasses import dataclass
from dataclasses import replace

import httpx
from a2a.client import A2ACardResolver
from google.protobuf.json_format import MessageToDict

from .client import RemoteAgentError
from .remote_config import PUBLIC_CLIENT_EXTENSION
from .remote_config import RemoteAgentConfig
from .remote_config import remote_endpoint


@dataclass(frozen=True)
class RemoteDiscovery:
    connection: RemoteAgentConfig
    name: str
    description: str
    authentication: str
    client_id_advertised: bool = False


async def discover_remote(
    connection: RemoteAgentConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> RemoteDiscovery:
    """Use the selected host's Card and OIDC document; never infer a client secret.

    A client registration is not part of standard A2A security schemes. Native
    hosts can advertise a *public* PKCE client using our optional extension.
    Ambiguous/combined schemes require an explicit local choice, not a guess.
    A discovered RPC endpoint must stay on the selected service origin.
    """
    context = ssl.create_default_context()
    if connection.ca_file:
        context.load_verify_locations(cafile=connection.ca_file)
    try:
        async with httpx.AsyncClient(
            verify=context,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=10,
            headers={"A2A-Version": "1.0"},
        ) as http:
            card = await A2ACardResolver(http, connection.url).get_agent_card()
            selected = httpx.URL(connection.url)
            interfaces = [
                interface
                for interface in card.supported_interfaces
                if interface.protocol_binding == "JSONRPC"
                and re.fullmatch(r"1\.0(?:\.\d+)?", interface.protocol_version)
                and not interface.tenant
                and (
                    lambda endpoint: (
                        (endpoint.scheme, endpoint.host, endpoint.port)
                        == (selected.scheme, selected.host, selected.port)
                    )
                )(httpx.URL(interface.url))
                and (not connection.rpc_url or httpx.URL(interface.url) == httpx.URL(connection.rpc_url))
            ]
            if len(interfaces) != 1:
                raise RemoteAgentError("Discovery must offer one A2A 1.0 JSON-RPC endpoint on the selected origin")
            discovered = replace(connection, rpc_url=interfaces[0].url)
            if connection.issuer or connection.token_env:
                return RemoteDiscovery(discovered, card.name[:200], card.description[:2000], "configured")
            if not card.security_requirements or any(
                not requirement.schemes for requirement in card.security_requirements
            ):
                return RemoteDiscovery(discovered, card.name[:200], card.description[:2000], "none")
            if len(card.security_requirements) != 1 or len(card.security_requirements[0].schemes) != 1:
                raise RemoteAgentError(
                    "The remote host advertises multiple authentication options. Choose its authentication explicitly."
                )
            name, required = next(iter(card.security_requirements[0].schemes.items()))
            scheme = card.security_schemes.get(name)
            if scheme is None or scheme.WhichOneof("scheme") not in {
                "open_id_connect_security_scheme",
                "oauth2_security_scheme",
            }:
                return RemoteDiscovery(discovered, card.name[:200], card.description[:2000], "manual")
            metadata_url = (
                scheme.open_id_connect_security_scheme.open_id_connect_url
                if scheme.WhichOneof("scheme") == "open_id_connect_security_scheme"
                else scheme.oauth2_security_scheme.oauth2_metadata_url
            )
            metadata_url = remote_endpoint(metadata_url, loopback_http=connection.loopback_http)
            response = await http.get(metadata_url)
            response.raise_for_status()
            if len(response.content) > 131072:
                raise ValueError("Metadata exceeds the supported size")
            metadata = response.json()
            issuer = metadata.get("issuer")
            if not isinstance(issuer, str):
                raise ValueError("Sign-in metadata does not define an issuer")
            remote_endpoint(issuer, loopback_http=connection.loopback_http)
            # A public client's advertised scopes may differ from API permission
            # claims when the server maps roles to permissions itself.
            scopes = tuple(
                dict.fromkeys(
                    ("openid", *required.list)
                    if scheme.WhichOneof("scheme") == "open_id_connect_security_scheme"
                    else required.list
                )
            )
            client_id, resource, advertised = "", "", False
            for extension in card.capabilities.extensions:
                if extension.uri == PUBLIC_CLIENT_EXTENSION:
                    fields = MessageToDict(extension.params)
                    client_id = fields.get("client_id", "")
                    public_scopes = fields.get("scopes", list(scopes))
                    resource = fields.get("resource", "")
                    if (
                        not isinstance(client_id, str)
                        or not isinstance(resource, str)
                        or not isinstance(public_scopes, list)
                        or any(not isinstance(scope, str) for scope in public_scopes)
                    ):
                        raise ValueError("Invalid public OAuth client advertisement")
                    scopes, advertised = tuple(public_scopes), bool(client_id)
            discovered = replace(
                discovered,
                issuer=issuer,
                metadata_url=metadata_url,
                scopes=scopes,
                client_id=client_id,
                resource=resource,
            )
            return RemoteDiscovery(discovered, card.name[:200], card.description[:2000], "oauth", advertised)
    except RemoteAgentError:
        raise
    except Exception:
        raise RemoteAgentError(
            "Remote discovery failed. Check the server URL, CA bundle and advertised authentication metadata."
        ) from None
