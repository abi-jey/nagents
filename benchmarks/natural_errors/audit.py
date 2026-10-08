"""Conservative, actor-local accounting for real model tool traces.

The recorder supplies schema validation against the contract actually offered on
that turn. Runtime facts must be backed by earlier model-visible feedback; hidden
fixture outcomes must never be turned into knowledge here.
"""

import re
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Literal

if TYPE_CHECKING:
    from nagents.types import JsonSchema
    from nagents.types import JsonValue

Category = Literal["avoidable", "legitimate", "ambiguous"]
ErrorKind = Literal["schema", "unknown_tool", "missing_path", "permission", "bounds", "conflict", "other"]


@dataclass(frozen=True)
class VisibleFact:
    """A fact visible before this call, scoped to this actor and task.

    source_call_id is mandatory and must identify an earlier trace call. Runtime
    bounds use argument and maximum. Path facts use target. External conflicts
    use kind='external_change' only for model-visible read/change evidence;
    hidden fixture events are never evidence of agent knowledge. A read_snapshot
    records a successful visible read; path identity requires an exact target
    match. Potential aliases remain uncertain without visible identity evidence.
    """

    source_call_id: str
    kind: Literal["maximum", "minimum", "missing_path", "external_change", "read_snapshot", "feedback"]
    target: str = ""
    argument: str = ""
    maximum: int = 0
    minimum: int = 0


@dataclass(frozen=True)
class CallRecord:
    actor_id: str
    task_id: str
    call_id: str
    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)
    offered_tools: tuple[str, ...] = ()
    contract_captured: bool = False
    schema_error: str = ""
    error: str = ""
    error_kind: ErrorKind = "other"
    target: str = ""
    mutation: bool = False
    # Only populate when the same intention is established from visible trace.
    intention: str = ""
    visible_facts: tuple[VisibleFact, ...] = ()
    # Bounds extracted only from this turn's offered parameter contract.
    offered_bounds: tuple[VisibleFact, ...] = ()


@dataclass(frozen=True)
class Failure:
    call_id: str
    actor_id: str
    task_id: str
    category: Category
    reason: str
    repeated_after_feedback: bool
    recovered_by: str = ""
    calls_until_recovery: int = 0


@dataclass(frozen=True)
class Audit:
    calls: int
    initial_valid_successes: int
    failures: tuple[Failure, ...]
    recovery_candidates: tuple[tuple[str, str], ...]

    @property
    def avoidable_failures(self) -> int:
        return sum(f.category == "avoidable" for f in self.failures)

    @property
    def repeated_failures(self) -> int:
        return sum(f.repeated_after_feedback for f in self.failures)

    @property
    def recoveries(self) -> int:
        return sum(bool(f.recovered_by) for f in self.failures)

    @property
    def pending_failures(self) -> int:
        return sum(not f.recovered_by for f in self.failures)


def _facts(call: CallRecord, prior: dict[str, CallRecord]) -> tuple[VisibleFact, ...]:
    return tuple(
        fact
        for fact in call.visible_facts
        if fact.source_call_id in prior
        and prior[fact.source_call_id].actor_id == call.actor_id
        and prior[fact.source_call_id].task_id == call.task_id
        and (fact.kind not in {"maximum", "minimum"} or prior[fact.source_call_id].tool_name == call.tool_name)
    )


def _classify(call: CallRecord, facts: tuple[VisibleFact, ...], prior: dict[str, CallRecord]) -> tuple[Category, str]:
    if call.schema_error:
        if call.contract_captured:
            return "avoidable", "Arguments violate the offered contract"
        return "ambiguous", "Validation failed but offered contract was not captured"
    if call.contract_captured and call.tool_name not in call.offered_tools:
        return "avoidable", "Tool was absent from the offered catalog"
    if call.error_kind == "permission":
        if any(
            f.kind == "feedback"
            and prior[f.source_call_id].error_kind == "permission"
            and prior[f.source_call_id].tool_name == call.tool_name
            and prior[f.source_call_id].arguments == call.arguments
            and prior[f.source_call_id].error == call.error
            for f in facts
        ):
            return "avoidable", "Identical denied action repeated after visible denial"
        return "legitimate", "Permission denial"
    if call.error_kind == "missing_path":
        if any(f.kind == "missing_path" and f.target == call.target for f in facts):
            return "avoidable", "Path was already reported missing"
        if call.mutation:
            return "ambiguous", "Mutation destination requires intent review"
        return "legitimate", "First missing-path observation"
    if call.error_kind == "bounds":
        for fact in (*facts, *call.offered_bounds):
            value = call.arguments.get(fact.argument)
            if (
                fact.kind == "maximum"
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value > fact.maximum
            ):
                return "avoidable", "Argument exceeds an explicitly visible runtime bound"
            if (
                fact.kind == "minimum"
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value < fact.minimum
            ):
                return "avoidable", "Argument falls below an explicitly visible runtime bound"
        return "legitimate", "Runtime bound was not previously established"
    if call.error_kind == "conflict":
        visible_ids = {fact.source_call_id for fact in facts}
        visible_calls = [record for identifier, record in prior.items() if identifier in visible_ids]
        last_conflict = -1
        last_read = -1
        last_other_read = -1
        for index, record in enumerate(visible_calls):
            if record.target != call.target or not call.target:
                if record.tool_name == "read_file" and not record.error and not record.schema_error:
                    last_other_read = index
                continue
            if record.error_kind == "conflict" and record.tool_name == call.tool_name and record.error:
                last_conflict = index
            if record.tool_name == "read_file" and not record.error and not record.schema_error:
                last_read = index
        if last_conflict >= 0 and last_read <= last_conflict:
            if last_other_read > last_conflict:
                return "ambiguous", "Reread target differs; path equivalence requires manual judgment"
            return "avoidable", "Conflict repeated after visible feedback without a successful reread"
    if call.error_kind == "conflict" and "File changed during approval" in call.error:
        return "legitimate", "Runtime explicitly reports concurrent change during approval"
    if call.error_kind == "conflict" and any(
        f.kind in {"external_change", "read_snapshot"} and f.target == call.target for f in facts
    ):
        return "legitimate", "Observed external change explains stale edit"
    return "ambiguous", "Runtime failure requires manual judgment"


def _same_intention(first: CallRecord, later: CallRecord) -> bool:
    if first.intention and later.intention:
        return first.intention == later.intention and first.target == later.target
    return bool(first.target) and first.target == later.target and first.tool_name == later.tool_name


def audit_calls(calls: tuple[CallRecord, ...]) -> Audit:
    """Account for every error, preserving failed attempts after recovery.

    Windows count calls by the same actor/task only. A successful different tool
    is only a recovery when an explicit shared intention and target establish it.
    Otherwise a same-target success is exposed as a manual-review candidate.
    """
    prior: dict[str, CallRecord] = {}
    failures: list[Failure] = []
    positions: dict[str, int] = {}
    local_counts: dict[tuple[str, str], int] = {}
    candidates: list[tuple[str, str]] = []
    initial_successes = 0
    for call in calls:
        if call.call_id in prior:
            raise ValueError(f"Duplicate call id: {call.call_id}")
        actor = (call.actor_id, call.task_id)
        local_counts[actor] = local_counts.get(actor, 0) + 1
        positions[call.call_id] = local_counts[actor]
        facts = _facts(call, prior)
        failed = bool(call.error or call.schema_error)
        if failed:
            category, reason = _classify(call, facts, prior)
            repeated = (
                any(
                    (prior[f.source_call_id].error or prior[f.source_call_id].schema_error)
                    and prior[f.source_call_id].error_kind == call.error_kind
                    and (
                        _same_intention(prior[f.source_call_id], call)
                        or (
                            prior[f.source_call_id].tool_name == call.tool_name
                            and prior[f.source_call_id].arguments == call.arguments
                        )
                    )
                    for f in facts
                )
                and category == "avoidable"
            )
            failures.append(Failure(call.call_id, *actor, category, reason, repeated))
        else:
            matching = False
            for index, failure in enumerate(failures):
                if (failure.actor_id, failure.task_id) != actor or failure.recovered_by:
                    continue
                first = prior[failure.call_id]
                if _same_intention(first, call):
                    failures[index] = Failure(
                        failure.call_id,
                        *actor,
                        failure.category,
                        failure.reason,
                        failure.repeated_after_feedback,
                        call.call_id,
                        local_counts[actor] - positions[failure.call_id],
                    )
                    matching = True
                elif first.target and first.target == call.target:
                    candidates.append((first.call_id, call.call_id))
            if not matching:
                initial_successes += 1
        prior[call.call_id] = call
    return Audit(len(calls), initial_successes, tuple(failures), tuple(candidates))


def audit_trace(requests: list[dict[str, object]], tool_events: list[dict[str, object]]) -> Audit:
    """Adapt captured native requests/events without consulting expected outcomes.

    Captured parameter descriptions and tool feedback present in the next
    provider request supply knowledge.
    Interrupted entries have no completed result and are excluded from call
    counts and recovery accounting. The runner retains them in the raw trace.
    Unknown runtime error formats remain ambiguous rather than guessed.
    """
    from typing import cast

    from nagents.harness.tools import _validate

    records: list[CallRecord] = []
    request_map = {str(request.get("generation_id", "")): request for request in requests}
    for event in tool_events:
        if event.get("outcome") == "interrupted":
            continue
        request = request_map.get(str(event.get("generation_id", "")), {})
        raw_tools = request.get("tools", [])
        contracts: dict[str, dict[str, object]] = {}
        if isinstance(raw_tools, list):
            for tool in raw_tools:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    contracts[tool["name"]] = tool
        arguments = event.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        name = str(event.get("tool", ""))
        schema_error = ""
        parameters = contracts.get(name, {}).get("parameters")
        if isinstance(parameters, dict):
            try:
                _validate(cast("JsonValue", arguments), cast("JsonSchema", parameters))
            except ValueError as exc:
                schema_error = str(exc)
        error = str(event.get("error") or "")
        kind: ErrorKind = "schema" if schema_error else "other"
        if "No such file or directory" in error or "FileNotFoundError" in error:
            kind = "missing_path"
        elif any(
            text in error
            for text in (
                "Permission denied",
                "PermissionError",
                "Approval denied for",
                "read-only",
                "disabled for this agent",
                "Delegation is disabled",
                "do not support custom plugin/tool execution",
            )
        ):
            kind = "permission"
        elif "limit must be 1..1000" in error:
            kind = "bounds"
        elif "File changed during approval" in error or "File was not read or has changed since read" in error:
            kind = "conflict"
        target = str(arguments.get("path", ""))
        actor = str(event.get("actor", "root"))
        task = str(event.get("task_id", ""))
        facts: list[VisibleFact] = []
        offered_bounds: list[VisibleFact] = []
        if isinstance(parameters, dict):
            properties = parameters.get("properties", {})
            if isinstance(properties, dict):
                for argument, prop in properties.items():
                    if not isinstance(argument, str) or not isinstance(prop, dict):
                        continue
                    description = str(prop.get("description", ""))
                    bounds = re.search(r"\b(\d+)\.\.(\d+)\b", description)
                    if bounds:
                        offered_bounds.extend(
                            (
                                VisibleFact("", "minimum", argument=argument, minimum=int(bounds[1])),
                                VisibleFact("", "maximum", argument=argument, maximum=int(bounds[2])),
                            )
                        )
                    for bound_name in ("minimum", "maximum"):
                        value = prop.get(bound_name)
                        if isinstance(value, int) and not isinstance(value, bool):
                            offered_bounds.append(
                                VisibleFact(
                                    "",
                                    "minimum" if bound_name == "minimum" else "maximum",
                                    argument=argument,
                                    minimum=value,
                                    maximum=value,
                                )
                            )
        messages = request.get("messages", [])
        visible_ids: set[str] = set()
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "user":
                    # User intervention can change authorization or state.
                    visible_ids.clear()
                elif isinstance(message, dict) and message.get("role") == "tool":
                    visible_ids.add(str(message.get("tool_call_id", "")))
        for previous in records:
            if previous.actor_id != actor or previous.task_id != task or previous.call_id not in visible_ids:
                continue
            if (
                kind == "conflict"
                and not previous.error
                and not previous.schema_error
                and previous.tool_name == "read_file"
            ):
                facts.append(VisibleFact(previous.call_id, "read_snapshot", target=previous.target))
            if previous.error or previous.schema_error:
                facts.append(VisibleFact(previous.call_id, "feedback", target=previous.target))
            if previous.error_kind == "missing_path":
                facts.append(VisibleFact(previous.call_id, "missing_path", target=previous.target))
            if previous.error_kind == "bounds" and "limit must be 1..1000" in previous.error:
                facts.append(VisibleFact(previous.call_id, "maximum", argument="limit", maximum=1000))
                facts.append(VisibleFact(previous.call_id, "minimum", argument="limit", minimum=1))
                facts.append(VisibleFact(previous.call_id, "minimum", argument="start_line", minimum=1))
        records.append(
            CallRecord(
                actor,
                task,
                str(event.get("call_id", "")),
                name,
                arguments,
                tuple(contracts),
                "tools" in request and isinstance(raw_tools, list),
                schema_error,
                error,
                kind,
                target,
                name in {"edit", "write", "edit_file", "write_file", "apply_patch"},
                visible_facts=tuple(facts),
                offered_bounds=tuple(offered_bounds),
            )
        )
    return audit_calls(tuple(records))
