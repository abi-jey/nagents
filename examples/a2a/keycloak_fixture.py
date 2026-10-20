"""Own one disposable loopback Keycloak fixture for optional A2A tests."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler
from urllib.request import build_opener

IMAGE = "quay.io/keycloak/keycloak:26.8.1@sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244"
REALM = "ngn-a2a-test"
SCOPES = ("ngn:access", "ngn:invoke", "ngn:read", "ngn:cancel", "ngn:approve")
REDIRECT = "http://127.0.0.1:8768/oauth/callback"
LABEL = "org.nagents.a2a.test-fixture"
GENERATED = ("container.json", "realm.json", "credentials.env", "credentials.sh", "a2a.json")


def private_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Use a private owned fixture directory (mode 0700)")


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(text)


def docker(*arguments: str, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(
        ["docker", *arguments], env=env, stderr=subprocess.DEVNULL, timeout=180, text=True
    ).strip()


def client(identifier: str, *, public: bool, audience: str) -> dict[str, object]:
    return {
        "clientId": identifier,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": public,
        "standardFlowEnabled": public,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": not public,
        "redirectUris": [REDIRECT, "http://127.0.0.1:8780/callback", "http://127.0.0.1:8781/oauth/callback"]
        if public
        else [],
        "attributes": {"pkce.code.challenge.method": "S256"} if public else {},
        "optionalClientScopes": list(SCOPES),
        "protocolMappers": [
            {
                "name": "authenticated-subject",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-sub-mapper",
                "config": {"access.token.claim": "true", "id.token.claim": "true", "userinfo.token.claim": "true"},
            },
            {
                "name": "intended-api-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.custom.audience": audience,
                    "id.token.claim": "false",
                    "access.token.claim": "true",
                },
            },
        ],
    }


def prepare(directory: Path) -> dict[str, str]:
    values = {
        "NGN_IDP_SERVICE_CLIENT_ID": "nagents-service",
        "NGN_IDP_SERVICE_CLIENT_SECRET": secrets.token_urlsafe(32),
        "NGN_IDP_BROWSER_CLIENT_ID": "nagents-browser",
        "NGN_IDP_BROWSER_REDIRECT_URI": REDIRECT,
        "NGN_IDP_EXPECTED_AUDIENCE": "nagents-a2a",
        "NGN_IDP_TEST_SCOPES": " ".join(SCOPES),
        "NGN_IDP_ALICE_USERNAME": "alice",
        "NGN_IDP_ALICE_PASSWORD": secrets.token_urlsafe(24),
        "NGN_IDP_BOB_USERNAME": "bob",
        "NGN_IDP_BOB_PASSWORD": secrets.token_urlsafe(24),
        "NGN_IDP_DOWNSTREAM_CLIENT_ID": "nagents-downstream",
        "NGN_IDP_DOWNSTREAM_SCOPE": "demo:read",
        "NGN_IDP_DOWNSTREAM_AUDIENCE": "demo-service",
    }
    service = client("nagents-service", public=False, audience="nagents-a2a")
    service["secret"] = values["NGN_IDP_SERVICE_CLIENT_SECRET"]
    downstream = client("nagents-downstream", public=True, audience="demo-service")
    downstream.update(consentRequired=True, optionalClientScopes=["demo:read"])
    realm = {
        "realm": REALM,
        "enabled": True,
        "registrationAllowed": False,
        "clients": [service, client("nagents-browser", public=True, audience="nagents-a2a"), downstream],
        "clientScopes": [
            {
                "name": scope,
                "protocol": "openid-connect",
                "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "true"},
            }
            for scope in (*SCOPES, "demo:read")
        ],
        "users": [
            {
                "username": user,
                "enabled": True,
                "emailVerified": True,
                "email": f"{user}@example.invalid",
                "firstName": user.title(),
                "lastName": "Fixture",
                "credentials": [
                    {"type": "password", "value": values[f"NGN_IDP_{user.upper()}_PASSWORD"], "temporary": False}
                ],
            }
            for user in ("alice", "bob")
        ],
    }
    write_private(directory / "realm.json", json.dumps(realm))
    return values


def stop_owned(container_id: str, owner: str) -> None:
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("Invalid fixture container identity")
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise ValueError("Invalid fixture owner identity")
    labels = json.loads(docker("inspect", "--format", "{{json .Config.Labels}}", container_id))
    if labels.get(LABEL) != owner:
        raise ValueError("Container ownership does not match this fixture")
    docker("rm", "-f", container_id)


def remove_generated(directory: Path) -> None:
    # Remove only the fixed files created by this helper, never arbitrary data.
    for name in GENERATED:
        (directory / name).unlink(missing_ok=True)


def stop(directory: Path) -> None:
    state = json.loads((directory / "container.json").read_text())
    stop_owned(state["container_id"], state["owner"])
    remove_generated(directory)


def start(directory: Path) -> None:
    if any((directory / name).exists() or (directory / name).is_symlink() for name in GENERATED):
        raise ValueError("Fixture files already exist; stop the existing fixture or use a fresh directory")
    values = prepare(directory)
    owner = secrets.token_hex(16)
    environment = dict(os.environ)
    environment.update(
        KC_BOOTSTRAP_ADMIN_USERNAME="fixture-admin", KC_BOOTSTRAP_ADMIN_PASSWORD=secrets.token_urlsafe(32)
    )
    # Root is confined to the disposable container; it can read the private
    # import file regardless of the developer's host uid. No host sockets/data
    # other than this one read-only fixture file are mounted.
    try:
        container_id = docker(
            "run",
            "-d",
            "--name",
            f"ngn-a2a-test-{owner[:12]}",
            "--label",
            f"{LABEL}={owner}",
            "--user",
            "0:0",
            "--memory",
            "1536m",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "-p",
            "127.0.0.1::8080",
            "--env",
            "KC_BOOTSTRAP_ADMIN_USERNAME",
            "--env",
            "KC_BOOTSTRAP_ADMIN_PASSWORD",
            "--mount",
            f"type=bind,src={directory / 'realm.json'},dst=/opt/keycloak/data/import/realm.json,readonly",
            IMAGE,
            "start-dev",
            "--import-realm",
            "--hostname-strict=false",
            env=environment,
        )
    except BaseException:
        # Docker may create a stopped container before reporting a startup
        # error. Only remove a container carrying this invocation's own label.
        owned = docker("ps", "-aq", "--filter", f"label={LABEL}={owner}")
        for container_id in owned.splitlines():
            if re.fullmatch(r"[0-9a-f]{12,64}", container_id):
                docker("rm", "-f", container_id)
        (directory / "realm.json").unlink(missing_ok=True)
        raise
    try:
        write_private(directory / "container.json", json.dumps({"container_id": container_id, "owner": owner}))
        ports = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", container_id))
        port = int(ports["8080/tcp"][0]["HostPort"])
        issuer = f"http://127.0.0.1:{port}/realms/{REALM}"
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 180
        while True:
            try:
                with opener.open(f"{issuer}/.well-known/openid-configuration", timeout=2) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError, ConnectionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Disposable Keycloak did not become ready") from None
                time.sleep(1)
        values.update(
            NGN_IDP_ISSUER=issuer,
            NGN_IDP_AUTHORIZATION_URL=f"{issuer}/protocol/openid-connect/auth",
            NGN_IDP_TOKEN_URL=f"{issuer}/protocol/openid-connect/token",
        )
        write_private(directory / "credentials.env", "".join(f"{key}={value}\n" for key, value in values.items()))
        write_private(
            directory / "credentials.sh",
            "".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()),
        )
        write_private(
            directory / "a2a.json",
            json.dumps(
                {
                    "public_url": "http://127.0.0.1:8781",
                    "agent": "machine",
                    "data_dir": str(directory / "server-state"),
                    "loopback_http": True,
                    "issuer": {
                        "issuer": issuer,
                        "audience": "nagents-a2a",
                        "token_kind_claim": "typ",
                        "token_kind_value": "Bearer",
                    },
                    "browser_client_id": "nagents-browser",
                }
            ),
        )
    except BaseException:
        stop_owned(container_id, owner)
        remove_generated(directory)
        raise
    print(json.dumps({"ready": True, "issuer": issuer, "private_fixture": str(directory)}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("up", "down"))
    parser.add_argument("--directory", type=Path, default=Path(tempfile.gettempdir()) / "ngn-a2a-idp")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("This disposable fixture requires POSIX private-file ownership controls")
    directory = args.directory.resolve()
    private_directory(directory)
    if args.operation == "up":
        start(directory)
    else:
        stop(directory)
        print("Disposable fixture stopped; its private generated files were removed.")


if __name__ == "__main__":
    main()
