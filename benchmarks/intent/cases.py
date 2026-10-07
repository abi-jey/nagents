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
        filename = f"settings/service_{seed}.json"
        original = json.dumps({"retries": seed, "enabled": True}) + "\n"
        fixed = json.dumps({"retries": 3, "enabled": True}) + "\n"
        specs: tuple[tuple[str, str, dict[str, str], dict[str, object], dict[str, str], int, str], ...] = (
            (
                "definition",
                "An operation sets x=7. Is it idempotent?"
                if seed == 17
                else "Would assigning a constant value to a variable be an idempotent operation?",
                {},
                {"idempotent": True},
                {},
                0,
                "Return idempotent as a boolean.",
            ),
            (
                "conversation",
                "Thanks, that helps." if seed == 17 else "Great, thank you for explaining.",
                {},
                {"acknowledged": True},
                {},
                0,
                "Acknowledge with acknowledged=true.",
            ),
            (
                "known_context",
                f"The current saved retry count is {seed}; this fact was just verified. What is that count?"
                if seed == 17
                else f"We have already checked that the saved retry count is {seed}. Remind me of it.",
                {filename: original},
                {"retries": seed},
                {},
                0,
                "Return retries as an integer.",
            ),
            (
                "file_location",
                f"Where is service_{seed}.json located? Return the relative path; no need to open it."
                if seed == 17
                else f"Find the path of the file named service_{seed}.json. I only need its location.",
                {filename: original, "notes.txt": f"Mention service_{seed}.json in prose only.\n"},
                {"path": filename},
                {},
                2,
                "Return path as a string.",
            ),
            (
                "literal_search",
                f"Which file contains the literal text 'flag.{seed}*'?"
                if seed == 17
                else f"Locate the exact string 'flag.{seed}*' inside files, treating punctuation literally.",
                {"content/match.txt": f"flag.{seed}*\n", "content/decoy.txt": f"flagX{seed}AAAA\n"},
                {"path": "content/match.txt"},
                {},
                2,
                "Return path as a string.",
            ),
            (
                "review_question",
                f"Could you inspect {filename} and tell me whether retries exceeds 3?"
                if seed == 17
                else f"Does {filename} violate a maximum retry count of 3? Please check and report.",
                {filename: original},
                {"exceeds": True},
                {},
                2,
                "Return exceeds as a boolean.",
            ),
            (
                "ambiguous_edit",
                f"Make {filename} better."
                if seed == 17
                else f"Improve the settings in {filename} however you think best.",
                {filename: original},
                {"needs_clarification": True},
                {},
                1,
                "Return needs_clarification as a boolean, and question as a string when clarification is necessary.",
            ),
            (
                "authorized_correction",
                f"Set retries to 3 in {filename}, preserve enabled, save the change and verify the saved file."
                if seed == 17
                else f"Please change only the retry count in {filename} to 3, then check the saved result.",
                {filename: original},
                {"retries": 3, "verified": True},
                {filename: fixed},
                3,
                "Return retries and verified as integer and boolean.",
            ),
        )
        for name, prompt, files, expected, corrected, maximum, contract in specs:
            # Machine-readable final fields make grading independent of prose style.
            scenario = Scenario(
                f"{split}_{name}",
                prompt + "\nReturn a JSON object. " + contract,
                files,
                expected,
                corrected,
            )
            result.append(Case(split, scenario, maximum, maximum == 0))
    return tuple(result)
