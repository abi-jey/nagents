from dataclasses import replace

from benchmarks.natural_errors.audit import Audit
from benchmarks.natural_errors.audit import CallRecord
from benchmarks.natural_errors.audit import VisibleFact
from benchmarks.natural_errors.audit import audit_calls


def call(identifier: str, **kwargs: object) -> CallRecord:
    base = CallRecord("root", "task", identifier, "read_file", target="/a")
    return replace(base, **kwargs)  # type: ignore[arg-type]


def test_wrong_arguments_recover_without_erasing_error() -> None:
    bad = call("bad", schema_error="missing path", contract_captured=True, offered_tools=("read_file",))
    audit = audit_calls((bad, call("good")))
    assert audit.avoidable_failures == audit.recoveries == 1
    assert audit.failures[0].calls_until_recovery == 1
    assert audit.initial_valid_successes == audit.pending_failures == 0


def test_unknown_tool_and_missing_mutation_require_distinct_treatment() -> None:
    unknown = call(
        "unknown", tool_name="read", error="unknown tool", contract_captured=True, offered_tools=("read_file",)
    )
    mutation = call("mutation", error="missing", error_kind="missing_path", mutation=True)
    audit = audit_calls((unknown, mutation))
    assert [f.category for f in audit.failures] == ["avoidable", "ambiguous"]


def test_first_missing_path_and_corrected_path_remain_separate() -> None:
    missing = call("missing", error="missing", error_kind="missing_path")
    corrected = call("corrected", target="/actual")
    audit = audit_calls((missing, corrected))
    assert audit.failures[0].category == "legitimate"
    assert audit.pending_failures == 1
    assert audit.initial_valid_successes == 1


def test_visible_bound_makes_repeated_oversized_request_avoidable() -> None:
    first = call("first", error="maximum 20", error_kind="bounds", arguments={"lines": 100})
    repeat = call(
        "repeat",
        error="maximum 20",
        error_kind="bounds",
        arguments={"lines": 50},
        visible_facts=(VisibleFact("first", "maximum", argument="lines", maximum=20),),
    )
    success = call("success", arguments={"lines": 20})
    audit = audit_calls((first, repeat, success))
    assert [f.category for f in audit.failures] == ["legitimate", "avoidable"]
    assert audit.repeated_failures == 1
    assert audit.recoveries == 2
    assert [f.calls_until_recovery for f in audit.failures] == [2, 1]


def test_unknown_limits_and_cross_actor_facts_do_not_blame_model() -> None:
    first = call("first", actor_id="child", error="maximum 20", error_kind="bounds")
    second = call(
        "second",
        error="maximum 20",
        error_kind="bounds",
        arguments={"lines": 100},
        visible_facts=(VisibleFact("first", "maximum", argument="lines", maximum=20),),
    )
    audit = audit_calls((first, second, call("success")))
    assert audit.avoidable_failures == audit.repeated_failures == 0
    assert audit.pending_failures == 1
    assert audit.failures[1].calls_until_recovery == 1


def test_stale_edit_with_observed_external_change_is_legitimate() -> None:
    observed = call("observed")
    stale = call(
        "stale",
        tool_name="edit_file",
        error="stale",
        error_kind="conflict",
        visible_facts=(VisibleFact("observed", "external_change", target="/a"),),
    )
    assert audit_calls((observed, stale)).failures[0].category == "legitimate"


def test_different_tool_success_is_only_candidate_without_shared_intention() -> None:
    failure = call("failed", error="oops")
    success = call("success", tool_name="edit_file")
    audit = audit_calls((failure, success))
    assert audit.recoveries == 0
    assert audit.recovery_candidates == (("failed", "success"),)
    shared = audit_calls((replace(failure, intention="inspect"), replace(success, intention="inspect")))
    assert shared.recoveries == 1


def test_native_contract_adapter_and_feedback_visibility() -> None:
    from benchmarks.natural_errors.audit import audit_trace

    tools: list[dict[str, object]] = [
        {
            "name": "read_file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["path"],
            },
        }
    ]
    requests: list[dict[str, object]] = [
        {"generation_id": "one", "tools": tools, "messages": []},
        {"generation_id": "two", "tools": tools, "messages": [{"role": "tool", "tool_call_id": "first"}]},
    ]
    events: list[dict[str, object]] = [
        {
            "generation_id": "one",
            "actor": "root",
            "task_id": "task",
            "tool": "read_file",
            "call_id": "first",
            "arguments": {"path": "/a", "limit": 2000},
            "error": "start_line must be >= 1 and limit must be 1..1000",
        },
        {
            "generation_id": "two",
            "actor": "root",
            "task_id": "task",
            "tool": "read_file",
            "call_id": "second",
            "arguments": {"path": "/a", "limit": 1500},
            "error": "start_line must be >= 1 and limit must be 1..1000",
        },
    ]
    audit = audit_trace(requests, events)
    assert [failure.category for failure in audit.failures] == ["legitimate", "avoidable"]
    assert audit.repeated_failures == 1
    events[1]["arguments"] = {"path": "/a", "wrong": True}
    assert audit_trace(requests, events).avoidable_failures == 1


def test_permission_and_missing_feedback_are_accounted_for() -> None:
    missing = call("missing", error="missing", error_kind="missing_path")
    repeated = call(
        "repeated",
        error="missing",
        error_kind="missing_path",
        visible_facts=(VisibleFact("missing", "missing_path", target="/a"),),
    )
    denied = call("denied", error="permission", error_kind="permission", target="/protected")
    audit = audit_calls((missing, repeated, denied))
    assert audit.calls == 3
    assert len(audit.failures) == audit.pending_failures == 3
    assert audit.avoidable_failures == audit.repeated_failures == 1


def native_audit(
    events: list[dict[str, object]], descriptions: tuple[tuple[str, str], ...] = (), user_intervention: bool = False
) -> Audit:
    from benchmarks.natural_errors.audit import audit_trace

    properties: dict[str, object] = {"path": {"type": "string"}, "limit": {"type": "integer"}}
    for argument, description in descriptions:
        properties[argument] = {"type": "integer", "description": description}
    tools: list[dict[str, object]] = [
        {"name": name, "parameters": {"type": "object", "properties": properties, "required": ["path"]}}
        for name in ("read_file", "edit", "write")
    ]
    requests: list[dict[str, object]] = []
    previous_messages: list[dict[str, object]] = []
    for index, event in enumerate(events):
        event.update(generation_id=str(index), actor="root", task_id="task", call_id=str(index))
        messages = list(previous_messages)
        if user_intervention and index:
            messages.append({"role": "user", "content": "I fixed the permissions, try again"})
        requests.append({"generation_id": str(index), "tools": tools, "messages": messages})
        previous_messages.append({"role": "tool", "tool_call_id": str(index)})
    return audit_trace(requests, events)


def test_offered_description_makes_first_oversized_call_avoidable() -> None:
    audit = native_audit(
        [
            {
                "tool": "read_file",
                "arguments": {"path": "/a", "limit": 2000},
                "error": "start_line must be >= 1 and limit must be 1..1000",
            }
        ],
        (("limit", "Line count, not ending line: 1..1000; default 200."),),
    )
    assert isinstance(audit, Audit)
    assert audit.avoidable_failures == 1
    assert audit.repeated_failures == 0


def test_native_approval_denial_and_user_intervention() -> None:
    events: list[dict[str, object]] = [
        {"tool": "edit", "arguments": {"path": "/a"}, "error": "Approval denied for edit; no action was taken"},
        {"tool": "edit", "arguments": {"path": "/a"}, "error": "Approval denied for edit; no action was taken"},
    ]
    audit = native_audit(events)
    assert isinstance(audit, Audit)
    assert [f.category for f in audit.failures] == ["legitimate", "avoidable"]
    assert audit.repeated_failures == 1
    intervened = native_audit(events, user_intervention=True)
    assert isinstance(intervened, Audit)
    assert intervened.avoidable_failures == 0


def test_native_conflict_requires_visible_prior_read() -> None:
    conflict = {
        "tool": "edit",
        "arguments": {"path": "/a"},
        "error": "File was not read or has changed since read; read it again before editing",
    }
    alone = native_audit([dict(conflict)])
    assert isinstance(alone, Audit)
    assert alone.failures[0].category == "ambiguous"
    observed = native_audit([{"tool": "read_file", "arguments": {"path": "/a"}, "error": ""}, dict(conflict)])
    assert isinstance(observed, Audit)
    assert observed.failures[0].category == "legitimate"
    concurrent = native_audit(
        [
            {
                "tool": "edit",
                "arguments": {"path": "/a"},
                "error": "File changed during approval; read it again before editing",
            }
        ]
    )
    assert isinstance(concurrent, Audit)
    assert concurrent.failures[0].category == "legitimate"


def test_native_mutation_missing_path_and_interruption() -> None:
    audit = native_audit(
        [
            {"tool": "write", "arguments": {"path": "/absent/new"}, "error": "No such file or directory"},
            {"tool": "read_file", "arguments": {"path": "/a"}, "outcome": "interrupted"},
        ]
    )
    assert isinstance(audit, Audit)
    assert audit.calls == 1
    assert audit.failures[0].category == "ambiguous"


def test_repeated_stale_edit_is_avoidable_until_visible_successful_reread() -> None:
    conflict: dict[str, object] = {
        "tool": "edit",
        "arguments": {"path": "/a"},
        "error": "File was not read or has changed since read; read it again before editing",
    }
    read: dict[str, object] = {"tool": "read_file", "arguments": {"path": "/a"}, "error": ""}
    repeated = native_audit([dict(read), dict(conflict), dict(conflict)])
    assert [failure.category for failure in repeated.failures] == ["legitimate", "avoidable"]
    assert repeated.repeated_failures == 1
    reread = native_audit([dict(read), dict(conflict), dict(read), dict(conflict)])
    assert [failure.category for failure in reread.failures] == ["legitimate", "legitimate"]
    assert reread.repeated_failures == 0


def test_failed_reread_and_path_alias_do_not_clear_visible_conflict() -> None:
    conflict: dict[str, object] = {
        "tool": "edit",
        "arguments": {"path": "/a"},
        "error": "File was not read or has changed since read; read it again before editing",
    }
    failed_read: dict[str, object] = {"tool": "read_file", "arguments": {"path": "/a"}, "error": "Permission denied"}
    failed = native_audit([dict(conflict), failed_read, dict(conflict)])
    assert failed.failures[-1].category == "avoidable"
    aliased = native_audit(
        [
            {"tool": "read_file", "arguments": {"path": "./a"}, "error": ""},
            dict(conflict),
        ]
    )
    assert aliased.failures[0].category == "ambiguous"
    mismatched = native_audit(
        [
            dict(conflict),
            {"tool": "read_file", "arguments": {"path": "./a"}, "error": ""},
            dict(conflict),
        ]
    )
    assert mismatched.failures[-1].category == "ambiguous"
