"""Export the engine HTTP and event contract without starting its runtime."""

import argparse
import json

from contract_files import CONTRACT
from contract_files import write_or_check
from fastapi import FastAPI

from nagents.engine.api import EVENTS_PATH
from nagents.engine.api import install_api
from nagents.engine.api import schema_backend
from nagents.protocol import EVENTS_SUBPROTOCOL
from nagents.protocol.events import EventRecord


def export() -> dict[str, dict[str, object]]:
    app = FastAPI(title="nagents engine", openapi_url=None, docs_url=None, redoc_url=None)
    install_api(app, schema_backend)
    document: dict[str, object] = app.openapi()
    # OpenAPI describes HTTP but not upgraded messages. Link the event schema
    # explicitly instead of pretending the upgrade returns a JSON document.
    document["x-ngn-events"] = {
        "path": EVENTS_PATH,
        "subprotocol": EVENTS_SUBPROTOCOL,
        "authentication": "EngineToken",
        "cursor": "after",
        "session_filter": "session_id",
        "schema": "events.schema.json",
    }
    events = EventRecord.model_json_schema()
    events["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return {"openapi.json": document, "events.schema.json": events}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail rather than writing stale files.")
    arguments = parser.parse_args()
    results = [
        write_or_check(
            CONTRACT / filename, json.dumps(document, indent=2, sort_keys=True) + "\n", check=arguments.check
        )
        for filename, document in export().items()
    ]
    if not all(results):
        print("Run python scripts/export_contract.py and python scripts/generate_rust_api.py.")
        return 1
    print("Engine v1 OpenAPI and event JSON Schema are current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
