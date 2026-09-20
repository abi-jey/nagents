"""Real production management lifecycle with transport/provider and race injection."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.channels.types import ChannelError
from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.extensions import ModelRequest
from nagents.extensions import RunContext
from nagents.web import service
from nagents.web.catalog import ConnectionInput
from nagents.web.catalog import SavedCatalog
from nagents.web.channel_host import ChannelHost
from nagents.web.channel_management import NAMES
from nagents.web.channel_management import ChannelManagement
from tests.support.channels import FakeChannel
from tests.support.channels import site
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.testclient import WebSocketTestSession

    from nagents.channels.types import ChannelValue
    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.service import WebState
    from tests.support.channels import Site
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix
FAKE_SECRET = "fixture-credential-not-real-987654"


class _IntegrationHost(ChannelHost):
    """Observe production lifecycle; inject only eligibility lookup races."""

    def __init__(self, state: WebState) -> None:
        super().__init__(state)
        self.owned_roots: set[str] = set()  # Additional revocation/failure injection for races.
        self.lookup_fails = False
        self.management.eligible = self.eligible
        self.opens: list[tuple[bool, bool]] = []

    async def eligible(self, session_id: str) -> bool:
        if self.lookup_fails:
            raise RuntimeError(FAKE_SECRET)
        return session_id not in self.owned_roots and await super().management_eligible(session_id)


def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    original_init = _IntegrationHost.__init__
    hosts: list[_IntegrationHost] = []

    def initialize(host: _IntegrationHost, state: WebState) -> None:
        original_init(host, state)
        hosts.append(host)

    async def opened(channel: FakeChannel) -> None:
        host = hosts[-1]
        host.opens.append((host.state.active is None, host.state.mutating))

    monkeypatch.setattr(_IntegrationHost, "__init__", initialize)
    monkeypatch.setattr(service, "ChannelHost", _IntegrationHost)
    monkeypatch.setattr(FakeChannel, "open", opened)


def _host(app: Site) -> _IntegrationHost:
    assert isinstance(app.state.channels, _IntegrationHost)
    return app.state.channels


def _body(app: Site, **values: ChannelValue) -> dict[str, ChannelValue]:
    return {
        "revision": app.state.channels.catalog.revision,
        "plugin": "fixture",
        "enabled": True,
        "config": {"label": "public"},
        "secrets": {"token": FAKE_SECRET},
        **values,
    }


async def _wait(event: asyncio.Event) -> None:
    async with asyncio.timeout(HANG_GUARD):
        await event.wait()


def _approval(app: Site, socket: WebSocketTestSession, *, decision: str = "allow") -> dict[str, object]:
    while True:
        frame = socket.receive_json()
        if frame.get("record", {}).get("event") == "approval":
            record = cast("dict[str, object]", frame["record"])
            break
    response = app.client.post(
        "/api/approval",
        headers=app.headers,
        json={
            "run_id": record["run_id"],
            "approval_id": record["approval_id"],
            "call_id": record["id"],
            "decision": decision,
        },
    )
    assert response.status_code == 200, response.text
    return record


def _script(
    app: Site, outcome: str = "completed", /, **configuration: ChannelValue
) -> tuple[asyncio.Event, asyncio.Event]:
    queued = asyncio.Event()
    release = asyncio.Event()

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        last_user = max(index for index, message in enumerate(messages) if message.role == "user")
        results = [message for message in messages[last_user + 1 :] if message.role == "tool"]
        if not results:
            yield ToolCallEvent(
                id="configure",
                name="channel_configure",
                arguments={
                    "connection_id": "fixture",
                    "configuration": _body(app, **configuration),
                },
            )
        else:
            queued.set()
            await release.wait()
            if outcome == "failed":
                yield ErrorEvent(message=FAKE_SECRET, recoverable=False)
            else:
                yield TextDoneEvent(text="Configuration request queued; check status after this turn.")

    app.providers[0].script = script
    return queued, release


def _hold(app: Site) -> None:
    app.submit("hold")

    async def started() -> None:
        async with asyncio.timeout(HANG_GUARD):
            while not app.providers[0].requests:
                await asyncio.sleep(0.001)

    assert app.client.portal is not None
    app.client.portal.call(started)


def test_approved_tool_queues_then_real_turn_finishes_and_worker_opens_at_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        host = _host(app)
        queued, release = _script(app)
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        socket.receive_json()
        app.submit("Configure my connector")
        approval = _approval(app, socket)
        assert approval["tool"] == "channel_configure"
        assert app.client.portal is not None
        app.client.portal.call(_wait, queued)
        assert not app.channels and not host.catalog.path.exists()
        assert host.runtime is None and host.instructions.catalog == []
        status = app.client.portal.call(host.management.channel_configuration, "status")
        assert status["requests"][0]["status"] == "QUEUED"
        assert status["requests"][0]["saved"] is False
        assert FAKE_SECRET not in json.dumps(status)
        result = app.providers[0].requests[-1][-1]
        assert result.role == "tool" and "QUEUED" in str(result.content)
        assert FAKE_SECRET not in str(result.content)
        with pytest.raises(ChannelError, match="Only one"):
            app.client.portal.call(host.management.channel_configure, "second", _body(app))
        with pytest.raises(RuntimeError, match="idle boundary"):
            app.client.portal.call(host.management.flush)
        app.client.portal.call(release.set)
        app.idle()
        assert host.opens == [(True, True)]
        assert host.catalog.connections["fixture"].secrets == {"token": FAKE_SECRET}
        assert host.catalog.status["fixture"] == ("running", "")
        assert host.catalog.path.exists() and not host.management._pending
        assert host.management._results[-1]["status"] == "APPLIED"
        assert host.management._results[-1]["connector_status"] == "running"
        assert host.catalog.connections["fixture"].main_session_id == app.main
        tools = list(host.tools)
    assert all(channel.closed for channel in app.channels)
    assert not host.sources and not host.management._pending
    assert host.management not in app.state.harness.agent.plugins
    assert all(app.state.harness.agent.tool_registry.get(tool.name) is not tool for tool in tools)


@pytest.mark.parametrize("outcome", ["cancelled", "failed", "denied", "unattended", "shutdown"])
def test_cancel_failure_denial_and_shutdown_never_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app:
        host = _host(app)
        queued, release = _script(app, outcome)
        assert app.client.portal is not None
        if outcome == "unattended":
            app.client.portal.call(release.set)
            app.submit("Configure unattended")
            app.idle()
        else:
            with app.socket() as socket:
                socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
                socket.receive_json()
                app.submit("Configure")
                _approval(app, socket, decision="deny" if outcome == "denied" else "allow")
                app.client.portal.call(_wait, queued)
                if outcome == "cancelled":
                    run = app.state.active
                    assert run is not None
                    response = app.client.post("/api/cancel", headers=app.headers, json={"run_id": run.id})
                    assert response.status_code == 200
                elif outcome != "shutdown":
                    app.client.portal.call(release.set)
                if outcome != "shutdown":
                    app.idle()
        assert not host.catalog.path.exists() and not app.channels
    assert not host.management._pending and not host.management.ready
    assert not host.catalog.path.exists() and not app.channels and not host.sources
    if outcome in {"cancelled", "failed", "shutdown"}:
        assert host.management._results[-1]["status"] in {"CANCELLED", "FAILED"}
    else:
        assert not host.management._results
    # A new process-local manager has no replayable request or status history.
    assert not ChannelManagement(host, host.eligible)._pending


def test_revision_is_not_rebased_after_concurrent_ui_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        host = _host(app)
        queued, release = _script(app)
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        socket.receive_json()
        app.submit("Configure")
        _approval(app, socket)
        assert app.client.portal is not None
        app.client.portal.call(_wait, queued)
        # A competing UI-style save at the first idle boundary, before worker flush.
        original_flush = host.management.flush
        raced = False

        async def flush() -> None:
            nonlocal raced
            if host.management.ready and not raced:
                raced = True
                # Equivalent catalog commit to a concurrent UI edit; no source mutation
                # underneath the originating producer. The idle worker owns this hook.
                body = ConnectionInput.model_validate(_body(app, enabled=False, config={"label": "UI edit"}))
                await host.save("other", body)
            await original_flush()

        monkeypatch.setattr(host.management, "flush", flush)
        app.client.portal.call(release.set)
        app.idle()
        assert raced and set(host.catalog.connections) == {"other"}
        assert host.catalog.connections["other"].config == {"label": "UI edit"}
        record = host.management._results[-1]
        assert record["status"] == "FAILED" and record["reason"] == "STALE_REVISION"
        assert record["saved"] is False and not app.channels


def test_discovery_private_metadata_and_active_root_not_browser_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app:
        host = _host(app)
        app.configure(secrets={"token": FAKE_SECRET})
        other = app.client.post("/api/sessions/new", headers=app.headers, json={})
        assert other.status_code == 200
        selected = app.state.selected_session_id
        _hold(app)
        run = app.state.active
        assert run is not None and run.session_id == app.main
        response = app.client.post("/api/sessions/resume", headers=app.headers, json={"session_id": selected})
        assert response.status_code == 200 and selected != app.main
        assert app.client.portal is not None
        result = app.client.portal.call(host.management.channel_configuration)
        plugin = result["plugins"][0]
        assert plugin["secret_fields"] == ["token"]
        assert plugin["schema"]["properties"]["token"]["writeOnly"] is True
        assert "configured_secrets" in json.dumps(result) and FAKE_SECRET not in json.dumps(result)
        assert result["plugin_path"] == str(host.catalog.plugin_path)
        assert "bindings" not in result
        app.client.portal.call(host.management.channel_configure, "new", _body(app))
        assert host.management._pending[run.id].body.main_session_id == app.main
        assert host.state.selected_session_id == selected


@pytest.mark.parametrize("restriction", ["channel", "owned", "background", "lookup-failure", "no-run"])
def test_management_hidden_and_denied_for_untrusted_contexts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restriction: str
) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app:
        host = _host(app)
        if restriction != "no-run":
            _hold(app)
            run = app.state.active
            assert run is not None
            if restriction == "channel":
                run.source = {"channel": "fixture", "conversation_id": "remote"}
            elif restriction == "background":
                run.background = True
            elif restriction == "owned":
                host.owned_roots.add(run.session_id)  # No live binding; ownership still denies follow-ups.
            else:
                host.lookup_fails = True
        assert app.client.portal is not None
        with pytest.raises(ChannelError, match="trusted unassigned"):
            app.client.portal.call(host.management.channel_configuration)
        with pytest.raises(ChannelError, match="trusted unassigned"):
            app.client.portal.call(host.management.channel_configure, "fixture", _body(app))
        context = RunContext(app.state.harness.agent, app.main, "")
        request = ModelRequest([], app.state.harness.agent.tool_registry.get_all(), None)
        filtered = app.client.portal.call(host.management.before_model, context, request)
        assert not set(NAMES) & {tool.name for tool in filtered.tools}
        assert not host.management._pending and not app.channels


@pytest.mark.parametrize("bad", ["extra", "bool", "private-public", "name", "id", "nan", "large", "stale"])
def test_strict_bounded_input_rejected_without_echoing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app:
        _hold(app)
        host = _host(app)
        body = _body(app)
        id = "fixture"
        if bad == "extra":
            body["unexpected"] = FAKE_SECRET
        elif bad == "bool":
            body["enabled"] = "true"
        elif bad == "private-public":
            body["config"] = {"token": FAKE_SECRET}
        elif bad == "name":
            body["config"] = {"name": FAKE_SECRET}
        elif bad == "id":
            id = "../" + FAKE_SECRET
        elif bad == "nan":
            body["config"] = {"label": float("nan")}
        elif bad == "large":
            body["secrets"] = {"token": "x" * (1024 * 1024 + 1)}
        else:
            body["revision"] = "stale"
        assert app.client.portal is not None
        with pytest.raises(ChannelError) as failure:
            app.client.portal.call(host.management.channel_configure, id, body)
        assert FAKE_SECRET not in str(failure.value)
        assert not host.management._pending and not host.catalog.path.exists() and not app.channels


@pytest.mark.parametrize("leak", ["descriptor", "factory", "result", "exception"])
def test_plugin_data_and_failures_are_credential_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, leak: str
) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        host = _host(app)
        queued, release = _script(app)
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        socket.receive_json()
        app.submit("Configure")
        _approval(app, socket)
        assert app.client.portal is not None
        app.client.portal.call(_wait, queued)
        if leak == "descriptor":
            host.catalog.plugins["fixture"].description = FAKE_SECRET
            with pytest.raises(ChannelError) as error:
                app.client.portal.call(host.management.channel_configuration)
            assert FAKE_SECRET not in str(error.value)
        elif leak == "factory":

            def factory(config: dict[str, ChannelValue]) -> FakeChannel:
                raise RuntimeError(str(config["token"]))

            host.catalog.plugins["fixture"].factory = factory
        else:
            original_save = host.save

            async def save(id: str, body: ConnectionInput) -> dict[str, object]:
                if leak == "exception":
                    raise RuntimeError(FAKE_SECRET)
                result = await original_save(id, body)
                return {**result, "unsafe": FAKE_SECRET}

            monkeypatch.setattr(host, "save", save)
        app.client.portal.call(release.set)
        app.idle()
        records = list(host.management._results)
        assert records[-1]["status"] == "FAILED"
        assert records[-1]["saved"] is (leak == "result")
        assert FAKE_SECRET not in json.dumps(records) + caplog.text


def test_eligibility_rechecked_at_apply_and_early_finish_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app:
        _hold(app)
        host = _host(app)
        run = app.state.active
        assert run is not None and app.client.portal is not None
        app.client.portal.call(host.management.channel_configure, "fixture", _body(app))
        app.client.portal.call(host.management.run_finished, run)
        assert not host.management._pending and host.management._results[-1]["status"] == "FAILED"
        with pytest.raises(ChannelError, match="Only one"):
            app.client.portal.call(host.management.channel_configure, "fixture", _body(app))
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        host = _host(app)
        queued, release = _script(app)
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        socket.receive_json()
        app.submit("Configure")
        _approval(app, socket)
        assert app.client.portal is not None
        app.client.portal.call(_wait, queued)
        host.owned_roots.add(app.main)
        app.client.portal.call(release.set)
        app.idle()
        assert host.management._results[-1]["reason"] == "ORIGIN_NOT_ELIGIBLE"
        assert not host.catalog.path.exists() and not app.channels


def test_discover_and_later_status_use_real_harness_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        host = _host(app)
        queued, release = _script(app)
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        socket.receive_json()
        app.submit("Configure")
        _approval(app, socket)
        assert app.client.portal is not None
        app.client.portal.call(_wait, queued)
        app.client.portal.call(release.set)
        app.idle()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            index = max(index for index, message in enumerate(messages) if message.role == "user")
            results = [message for message in messages[index + 1 :] if message.role == "tool"]
            if len(results) < 2:
                operation = "discover" if not results else "status"
                yield ToolCallEvent(id=operation, name="channel_configuration", arguments={"operation": operation})
            else:
                yield TextDoneEvent(text="The connection is now running.")

        app.providers[0].script = script
        app.submit("Discover and check status")
        assert _approval(app, socket)["tool"] == "channel_configuration"
        assert _approval(app, socket)["tool"] == "channel_configuration"
        app.idle()
        results = [message for message in app.providers[0].requests[-1] if message.role == "tool"][-2:]
        assert "'writeOnly': True" in str(results[0].content)
        assert "'revision'" in str(results[0].content)
        assert "'APPLIED'" in str(results[1].content) and "'running'" in str(results[1].content)
        assert FAKE_SECRET not in str([message.content for message in results])
        assert host.opens == [(True, True)]


def test_queued_configuration_is_not_restored_by_host_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        queued, _ = _script(app)
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        socket.receive_json()
        app.submit("Configure")
        _approval(app, socket)
        assert app.client.portal is not None
        app.client.portal.call(_wait, queued)
        assert _host(app).management._pending
    with site(tmp_path, monkeypatch) as app:
        app.idle()
        host = _host(app)
        assert not host.management._pending and not host.management._results
        assert not host.catalog.connections and not host.catalog.path.exists()
        assert not host.sources and not app.channels and not app.providers[0].requests


def test_failed_open_reports_saved_error_not_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch)

    async def broken_open(channel: FakeChannel) -> None:
        raise RuntimeError(FAKE_SECRET)

    monkeypatch.setattr(FakeChannel, "open", broken_open)
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        host = _host(app)
        queued, release = _script(app)
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        socket.receive_json()
        app.submit("Configure")
        _approval(app, socket)
        assert app.client.portal is not None
        app.client.portal.call(_wait, queued)
        app.client.portal.call(release.set)
        app.idle()
        record = host.management._results[-1]
        assert record["status"] == "APPLIED" and record["saved"] is True
        assert record["connector_status"] == "error" and FAKE_SECRET not in json.dumps(record)
        assert app.channels[0].closed and not host.channels and not host.sources


def test_shutdown_joins_inflight_apply_and_closes_connector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch)
    entered = asyncio.Event()
    release_open = asyncio.Event()

    async def open_gate(channel: FakeChannel) -> None:
        entered.set()
        await release_open.wait()

    monkeypatch.setattr(FakeChannel, "open", open_gate)
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        host = _host(app)
        queued, release = _script(app)
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        socket.receive_json()
        app.submit("Configure")
        _approval(app, socket)
        assert app.client.portal is not None
        app.client.portal.call(_wait, queued)
        app.client.portal.call(release.set)
        app.client.portal.call(_wait, entered)

        async def shutdown() -> None:
            # An approved successful run has already committed at idle. Shutdown
            # joins the ordinary host save, then closes all resources it opened.
            task = asyncio.create_task(host.close())
            await asyncio.sleep(0)
            assert host.closed and host.management.closed and not task.done()
            release_open.set()
            async with asyncio.timeout(HANG_GUARD):
                await task

        app.client.portal.call(shutdown)
        assert all(channel.closed for channel in app.channels)
        assert not host.sources and not host.channels and not host.management._pending
        assert all(task.done() for task in host.tasks)


def test_cancellation_during_eligibility_lookup_cannot_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app:
        _hold(app)
        host = _host(app)
        run = app.state.active
        assert run is not None and app.client.portal is not None

        async def race() -> None:
            entered = asyncio.Event()
            release = asyncio.Event()

            async def eligible(session_id: str) -> bool:
                entered.set()
                await release.wait()
                return True

            host.management.eligible = eligible
            task = asyncio.create_task(host.management.channel_configure("fixture", _body(app)))
            await _wait(entered)
            await app.state.stop(run)
            release.set()
            with pytest.raises(ChannelError, match="trusted unassigned"):
                await task

        app.client.portal.call(race)
        assert not host.management._pending and not host.catalog.path.exists() and not app.channels


@pytest.mark.parametrize("origin", ["channel", "web-follow-up"])
def test_real_chat_owned_turn_cannot_configure_even_with_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app:
        host = _host(app)
        app.configure()
        app.emit("Initial chat message")
        app.idle()
        root = app.bindings()["chat-a"]
        if origin == "web-follow-up":
            app.emit("/new Another chat root")
            app.idle()
            assert app.bindings()["chat-a"] != root  # Permanent ownership outlives this binding.
        observed, release = _script(app)
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": root, "after": 0})
            socket.receive_json()
            if origin == "channel":
                app.emit("Configure from channel")
            else:
                app.submit("Configure from browser follow-up", session=root)
            _approval(app, socket)
            assert app.client.portal is not None
            app.client.portal.call(_wait, observed)
            result = app.providers[0].requests[-1][-1]
            assert result.role == "tool" and "trusted unassigned" in str(result.content)
            assert not set(NAMES) & set(app.providers[0].schemas[-1])
            assert not host.management._pending and not host.management._results
            app.client.portal.call(release.set)
            app.idle()
        assert host.catalog.connections["fixture"].secrets == {"token": "test-only-secret"}


@pytest.mark.parametrize(
    ("initial", "configuration", "expected"),
    [(False, {"auto_reply": True}, True), (True, {"auto_reply": False}, False), (True, {}, True)],
    ids=["enable", "disable", "preserve-omitted"],
)
def test_auto_reply_policy_approved_queue_and_idle_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial: bool,
    configuration: dict[str, ChannelValue],
    expected: bool,
) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        host = _host(app)
        app.configure(enabled=False, auto_reply=initial)
        before = host.catalog.path.read_bytes()
        definition = app.state.harness.agent.tool_registry.get("channel_configure")
        assert definition is not None
        schema = definition.parameters["properties"]["configuration"]
        assert schema["properties"]["auto_reply"]["type"] == "boolean"
        assert "auto_reply" not in schema["required"]
        queued, release = _script(app, **configuration)
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        socket.receive_json()
        app.submit("Update the connector's automatic communication policy")
        assert _approval(app, socket)["tool"] == "channel_configure"
        assert app.client.portal is not None
        app.client.portal.call(_wait, queued)
        run = app.state.active
        assert run is not None
        body = host.management._pending[run.id].body
        assert ("auto_reply" in body.model_fields_set) is ("auto_reply" in configuration)
        if configuration:
            assert body.auto_reply is expected
        assert host.management._results[-1]["status"] == "QUEUED"
        assert host.catalog.connections["fixture"].auto_reply is initial
        assert host.catalog.path.read_bytes() == before and not app.channels
        app.client.portal.call(release.set)
        app.idle()
        assert host.management._results[-1]["status"] == "APPLIED"
        assert host.opens == [(True, True)]
        assert host.catalog.connections["fixture"].auto_reply is expected
        saved = SavedCatalog.model_validate_json(host.catalog.path.read_bytes())
        assert saved.connections["fixture"].auto_reply is expected
        public = app.client.get("/api/channels", headers=app.headers).json()
        assert public["connections"][0]["auto_reply"] is expected


@pytest.mark.parametrize("policy", ["true", "false", 1, 0, 1.0, None, {}, [], FAKE_SECRET])
def test_auto_reply_policy_requires_exact_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: ChannelValue
) -> None:
    _wire(monkeypatch)
    with site(tmp_path, monkeypatch) as app:
        _hold(app)
        host = _host(app)
        assert app.client.portal is not None
        with pytest.raises(ChannelError) as error:
            app.client.portal.call(host.management.channel_configure, "fixture", _body(app, auto_reply=policy))
        assert FAKE_SECRET not in str(error.value)
        assert not host.management._pending and not host.catalog.path.exists() and not app.channels


def test_discovery_withholds_plugin_path_containing_protected_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch)
    monkeypatch.setenv("NGN_CHANNEL_PLUGIN_PATH", str(tmp_path / "installed-plugins" / FAKE_SECRET))
    with site(tmp_path, monkeypatch) as app:
        _hold(app)
        host = _host(app)
        assert app.client.portal is not None
        app.client.portal.call(host.catalog.protection.remember, {"token": FAKE_SECRET})
        with pytest.raises(ChannelError, match="credential protection") as error:
            app.client.portal.call(host.management.channel_configuration)
        assert FAKE_SECRET not in str(error.value)
        assert not host.management._pending and not host.catalog.path.exists()
