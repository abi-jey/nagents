"""Frozen development and heldout intent cases, declared before live tuning."""

from __future__ import annotations

import json
from dataclasses import dataclass

from benchmarks.orchestration.fixtures import Scenario


@dataclass(frozen=True)
class Case:
    split: str
    scenario: Scenario
    maximum_tools: int
    zero_tools: bool = False


def cases() -> tuple[Case, ...]:
    result: list[Case] = []
    for split, seed in (("development", 17), ("heldout", 43)):
        development = split == "development"
        filename = "settings/service_17.json" if development else "infra/europe/gateway.json"
        basename = "service_17.json" if development else "gateway.json"
        field = "retries" if development else "timeout_seconds"
        target = 3 if development else 45
        original = json.dumps({field: seed, "enabled": True}) + "\n"
        fixed = json.dumps({field: target, "enabled": True}) + "\n"
        match = "content/match.txt" if development else "docs/runbooks/signal.md"
        decoy = "content/decoy.txt" if development else "archive/old-signal.log"
        token = "flag.17*" if development else "route[west]?"
        decoy_text = "flagX17AAAA" if development else "routewA"
        ambiguous_files = (
            {"environments/staging/service.json": original, "environments/production/service.json": original}
            if development
            else {"regions/europe/gateway.json": original, "regions/asia/gateway.json": original}
        )
        specs: tuple[tuple[str, str, dict[str, str], dict[str, object], dict[str, str], int, str], ...] = (
            (
                "definition",
                "An operation sets x=7. Is it idempotent?"
                if development
                else "Would assigning a constant value to a variable be an idempotent operation?",
                {},
                {"idempotent": True},
                {},
                0,
                "Return idempotent as a boolean.",
            ),
            (
                "conversation",
                "Thanks, that helps." if development else "Great, thank you for explaining.",
                {},
                {"acknowledged": True},
                {},
                0,
                "Acknowledge with acknowledged=true.",
            ),
            (
                "known_context",
                f"The saved {field} value is {seed}; this fact was just verified. What is that value?"
                if development
                else f"We already checked the saved {field} value is {seed}. Remind me of it.",
                {filename: original},
                {field: seed},
                {},
                0,
                f"Return {field} as an integer.",
            ),
            (
                "file_location",
                f"Where is {basename} located? Return the relative path; no need to open it."
                if development
                else f"Find the path of the file named {basename}. I only need its location.",
                {filename: original, "notes.txt": f"Mention {basename} in prose only.\n"},
                {"path": filename},
                {},
                2,
                "Return path as a string.",
            ),
            (
                "literal_search",
                f"Which file contains the literal text '{token}'?"
                if development
                else f"Locate the exact string '{token}' inside files, treating punctuation literally.",
                {match: token + "\n", decoy: decoy_text + "\n"},
                {"path": match},
                {},
                2,
                "Return path as a string.",
            ),
            (
                "review_question",
                f"Could you inspect {filename} and tell me whether {field} exceeds 3?"
                if development
                else f"Does {filename} violate a maximum {field} value of 30? Please check and report.",
                {filename: original},
                {"exceeds": True},
                {},
                2,
                "Return exceeds as a boolean.",
            ),
            (
                "ambiguous_edit",
                "Set retries to 3 in the service configuration."
                if development
                else "Change the gateway timeout_seconds setting to 45.",
                ambiguous_files,
                {"needs_clarification": True},
                {},
                4,
                "Return needs_clarification as a boolean, and question as a string when clarification is necessary.",
            ),
            (
                "authorized_correction",
                f"Can you set {field} to {target} in {filename}, preserve enabled, save the change and verify the saved file?"
                if development
                else f"Please change only {field} in {filename} to {target}, then check the saved result.",
                {filename: original},
                {field: target, "verified": True},
                {filename: fixed},
                3,
                f"Return {field} and verified as integer and boolean.",
            ),
            (
                "advice_only",
                f"How would I set {field} to {target} in {filename}? Explain the proposed value only; do not edit."
                if development
                else f"Describe the value I should use to set {field} to {target} in {filename}; no file changes.",
                {filename: original},
                {"proposed_value": target},
                {},
                0,
                "Return proposed_value as an integer.",
            ),
            (
                "cancellation",
                "Cancel the pending retry change. Leave the service settings as saved; no further work."
                if development
                else "Stop the planned gateway update. Keep the current file and do nothing further.",
                {filename: original},
                {"cancelled": True},
                {},
                0,
                "Acknowledge using cancelled=true.",
            ),
        )
        for name, prompt, files, expected, corrected, maximum, contract in specs:
            scenario = Scenario(
                f"{split}_{name}",
                prompt + "\nReturn a JSON object. " + contract,
                files,
                expected,
                corrected,
            )
            result.append(Case(split, scenario, maximum, maximum == 0))
    return tuple(result)
