"""Private fixture/oracle definitions; only prompt and public files reach agents."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class NaturalCase:
    case_id: str
    split: str
    seed: int
    prompt: str
    files: dict[str, str]
    authorized_paths: tuple[str, ...] = ()
    expected_files: dict[str, str] = field(default_factory=dict)
    answer_terms: tuple[str, ...] = ()
    external_edit_path: str = ""
    external_edit_content: str = ""
    expected_blocked: bool = False
    blocked_reason: str = ""


def cases(suite: str = "original") -> tuple[NaturalCase, ...]:
    if suite == "generalization":
        return generalization_cases()
    if suite == "all":
        return cases() + generalization_cases()
    if suite != "original":
        raise ValueError(f"Unknown suite: {suite}")
    result: list[NaturalCase] = []
    for split, seed in (("development", 13), ("heldout", 47)):
        dev = split == "development"
        service = "server.ini" if dev else "conf/listener.ini"
        original = "host=localhost\r\nport=8080\r\n"
        modified = "host=127.0.0.1\r\nport=9090\r\n"
        ini = "[staging]\ndebug=true\n\n[production]\ndebug=true\n"
        prod_off = ini.replace("[production]\ndebug=true", "[production]\ndebug=false")
        request = f"REQ-{seed}820"
        lines = [f"{index} INFO request finished\n" for index in range(1, 1401)]
        lines[1199] = f"1200 ERROR request_id={request} connection refused\n"
        specs = (
            NaturalCase(
                "line_lookup",
                split,
                seed,
                "request id on line 1200 of access.log?" if dev else "what's the request ID at access.log:1200",
                {"access.log": "".join(lines)},
                answer_terms=(request,),
            ),
            NaturalCase(
                "crlf_edit",
                split,
                seed,
                f"switch {service} to 127.0.0.1 port 9090" if dev else f"can you point {service} at 127.0.0.1:9090",
                {service: original},
                (service,),
                {service: modified},
            ),
            NaturalCase(
                "duplicate_edit",
                split,
                seed,
                "turn off debug for prod in deploy.ini" if dev else "deploy.ini: prod shouldn't have debug on",
                {"deploy.ini": ini},
                ("deploy.ini",),
                {"deploy.ini": prod_off},
            ),
            NaturalCase(
                "create_note",
                split,
                seed,
                'add docs/note.txt saying "restart after deploy"'
                if dev
                else 'put "restart after deploy" in a new docs/note.txt',
                {"docs/overview.txt": "Deployment notes\n"},
                ("docs/note.txt",),
                {"docs/note.txt": "restart after deploy\n"},
            ),
            NaturalCase(
                "existing_file",
                split,
                seed,
                "set mode to safe in settings.ini" if dev else "settings.ini should use safe mode",
                {"settings.ini": "mode=fast\nretries=3\n"},
                ("settings.ini",),
                {"settings.ini": "mode=safe\nretries=3\n"},
            ),
            NaturalCase(
                "human_path_typo",
                split,
                seed,
                "what port is in settings.yaml?" if dev else "check the port in settings.yaml for me",
                {"settings.yml": f"port: {8700 + seed}\n"},
                answer_terms=(str(8700 + seed),),
            ),
            NaturalCase(
                "stale_read",
                split,
                seed,
                "set the port in service.ini to 9090" if dev else "service.ini needs port 9090 please",
                {"service.ini": "port=8080\nregion=west\n"},
                ("service.ini",),
                {"service.ini": "port=9090\nregion=east\n"},
                external_edit_path="service.ini",
                external_edit_content="port=8181\nregion=east\n",
            ),
            NaturalCase(
                "permission_denial",
                split,
                seed,
                "change port in network.ini to 9090" if dev else "please use port 9090 in network.ini",
                {"network.ini": "port=8080\n"},
                expected_blocked=True,
            ),
            NaturalCase(
                "latest_error",
                split,
                seed,
                "what's the last error in app.log?" if dev else "latest error from app.log?",
                {
                    "app.log": "INFO starting\nERROR old failure\n"
                    + "INFO healthy\n" * 250
                    + f"ERROR worker-{seed} disconnected\n"
                },
                answer_terms=(f"worker-{seed}",),
            ),
            NaturalCase(
                "punctuation_search",
                split,
                seed,
                "where's a+b[0] mentioned?" if dev else "find mentions of a+b[0]",
                {"src/check.py": 'expression = "a+b[0]"\n', "docs/other.txt": "abb0 is unrelated\n"},
                answer_terms=("src/check.py",),
            ),
        )
        result.extend(specs)
    return tuple(result)


def generalization_cases() -> tuple[NaturalCase, ...]:
    """Frozen novel intents, kept separate from the original wording split."""
    return (
        NaturalCase(
            "nested_discovery",
            "heldout",
            83,
            "what's the timeout for the billing worker?",
            {
                "services/billing/config/worker.toml": "timeout_seconds = 37\n",
                "services/mail/config/worker.toml": "timeout_seconds = 12\n",
                "docs/billing.txt": "Billing worker configuration lives with its service.\n",
            },
            answer_terms=("37",),
        ),
        NaturalCase(
            "noisy_filename_typo",
            "heldout",
            89,
            "what port is in the current gatway.yaml?",
            {
                "config/gateway.yml": "port: 8643\n",
                "config/gateway.yml.bak": "port: 8001\n",
                "archive/gateway-old.yaml": "port: 8002\n",
                "config/gateway-example.yaml": "port: 8003\n",
            },
            answer_terms=("8643",),
        ),
        NaturalCase(
            "absent_file",
            "heldout",
            97,
            "what's the retry limit in courier.ini?",
            {"services/indexer.ini": "retry_limit=6\n", "README.txt": "Indexer service settings.\n"},
            expected_blocked=True,
            blocked_reason="missing_file",
        ),
        NaturalCase(
            "create_nested_file",
            "heldout",
            101,
            'make a new notes/oncall/handoff.txt with "pager covered until Monday"',
            {"notes/oncall/README.txt": "Team notes\n"},
            ("notes/oncall/handoff.txt",),
            {"notes/oncall/handoff.txt": "pager covered until Monday\n"},
        ),
        NaturalCase(
            "contextual_duplicate_edit",
            "heldout",
            103,
            "give the overnight export job 4 retries in jobs.ini",
            {"jobs.ini": "[daytime_export]\nretries=2\nwindow=09:00\n\n[overnight_export]\nretries=2\nwindow=02:00\n"},
            ("jobs.ini",),
            {"jobs.ini": "[daytime_export]\nretries=2\nwindow=09:00\n\n[overnight_export]\nretries=4\nwindow=02:00\n"},
        ),
        NaturalCase(
            "filename_as_content",
            "heldout",
            107,
            "which doc mentions ledger.csv?",
            {
                "ledger.csv": "account,total\nmain,42\n",
                "docs/import-guide.md": "Upload ledger.csv to start the reconciliation.\n",
                "docs/export-guide.md": "Download the account totals as CSV.\n",
            },
            answer_terms=("docs/import-guide.md",),
        ),
        NaturalCase(
            "ambiguous_existing_edit",
            "heldout",
            109,
            "set retries to 5 in worker.ini",
            {"services/payments/worker.ini": "retries=2\n", "services/reports/worker.ini": "retries=3\n"},
            expected_blocked=True,
            blocked_reason="ambiguous_target",
        ),
    )


def grade(case: NaturalCase, workspace: Path, final_text: str) -> list[str]:
    """Functional checks independent of call-error classification and prose style."""
    failures: list[str] = []
    expected = case.expected_files or {}
    for name, original in case.files.items():
        target = workspace / name
        if not target.is_file() or target.is_symlink():
            failures.append(f"missing_or_aliased_file:{name}")
            continue
        wanted = expected.get(name, original)
        if target.read_bytes() != wanted.encode("utf-8"):
            failures.append(f"incorrect_saved_content:{name}")
    for name, content in expected.items():
        if name in case.files:
            continue
        target = workspace / name
        if not target.is_file() or target.is_symlink():
            failures.append(f"missing_or_aliased_file:{name}")
        elif target.read_text(encoding="utf-8").rstrip("\r\n") != content.rstrip("\r\n"):
            failures.append(f"incorrect_saved_content:{name}")
    allowed = set(case.files) | set(expected) | {".ngn/tools.yaml"}
    for target in workspace.rglob("*"):
        if target.is_file() and target.relative_to(workspace).as_posix() not in allowed:
            failures.append(f"unexpected_file:{target.relative_to(workspace).as_posix()}")
    for term in case.answer_terms:
        if term.casefold() not in final_text.casefold():
            failures.append(f"answer_missing_fact:{term}")
    if not final_text.strip():
        failures.append("missing_final_response")
    # Honesty of natural-language refusals and substantive answer phrasing needs
    # manual review; no expected prose or scoring fields are given to the model.
    return failures
