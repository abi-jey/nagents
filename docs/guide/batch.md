# Batch processing

The **Python library** supports delayed provider batch jobs through `BatchClient`
and `BatchManager`. This is not native Rust FIFO/subagent execution. Batch routes
are implemented for OpenAI/Azure and Anthropic; text-generation support alone
does not imply batch support. Model access, pricing, quotas and deadlines are
provider-specific. ChatGPT subscription credentials are not batch API keys.

## Basic usage and complete example

`BatchManager` takes credentials, provider type, model and a database path—not a
`Provider` or prebuilt store. It persists jobs internally. Requests use
`custom_id` and `list[Message]`, not `id` and raw message dictionaries.

This example makes live provider requests **only if you run it**. Set
`OPENAI_API_KEY` and `OPENAI_MODEL` for an account/model supporting the batch
endpoint. It uses explicit polling with automatic monitoring disabled so there
is only one result-processing owner:

```python title="complete_batch.py"
import asyncio
import os
from pathlib import Path

from nagents import BatchManager, BatchRequest, BatchStatus, Message, ProviderType


async def process_documents(documents: list[str]) -> dict[str, str]:
    async with BatchManager(
        api_key=os.environ["OPENAI_API_KEY"],
        provider_type=ProviderType.OPENAI,
        model=os.environ["OPENAI_MODEL"],
        db_path=Path("batch_jobs.db"),
    ) as manager:
        requests = [
            BatchRequest(
                custom_id=f"doc-{index}",
                messages=[Message(role="user", content=f"Summarize: {document}")],
            )
            for index, document in enumerate(documents)
        ]
        job = await manager.create_batch(requests, auto_monitor=False)
        print(f"Batch ID: {job.id}")  # Keep this ID to inspect/resume later.
        terminal = {
            BatchStatus.COMPLETED,
            BatchStatus.ENDED,
            BatchStatus.FAILED,
            BatchStatus.EXPIRED,
            BatchStatus.CANCELLED,
        }
        while job.status not in terminal:
            await asyncio.sleep(60)
            refreshed = await manager.get_batch(job.id)
            if refreshed is None:
                raise RuntimeError("Batch status unavailable")
            job = refreshed

        if job.status not in {BatchStatus.COMPLETED, BatchStatus.ENDED}:
            raise RuntimeError(f"Batch ended with status {job.status.value}")

        summaries: dict[str, str] = {}
        for result in await manager.get_results(job.id):
            if result.result_type == "succeeded":
                summaries[result.custom_id] = result.content or ""
            else:
                summaries[result.custom_id] = f"{result.result_type}: {result.error_message or 'No text result'}"
        return summaries


async def main() -> None:
    summaries = await process_documents(["AI is transforming industries."])
    for document_id, summary in summaries.items():
        print(document_id, summary)


if __name__ == "__main__":
    asyncio.run(main())
```

`get_batch(id)` updates local state from the API, but may return cached stored
state after an API error. A returned status is not proof of fresh connectivity.
`get_results(id)` fetches results only for `COMPLETED` or Anthropic `ENDED` jobs.
Inspect each `result_type`; an ended/completed job can contain failed requests.

## Status and result fields

| Status | Meaning |
| --- | --- |
| `VALIDATING` | Input validation |
| `IN_PROGRESS` | Provider processing |
| `FINALIZING` | Provider preparing results |
| `COMPLETED` | Completed OpenAI-style job |
| `ENDED` | Anthropic processing ended; inspect individual results |
| `FAILED`, `EXPIRED` | Job failed or deadline expired |
| `CANCELLING`, `CANCELLED` | Cancellation requested / completed |

These are `BatchStatus` enum members; the old `pending`/`processing` examples
were not this API's values. `BatchResult` identifies its request with `custom_id`
and exposes `result_type`, optional `content`, `tool_calls`, `usage`,
`error_type` and `error_message`. It has no `response` or generic `error` field.

## Persistence, callbacks and restart

`BatchManager` creates its own `BatchStore(db_path)` and `BatchClient`. Keep the
database private: stored requests, callback context and results can contain
sensitive data. Register named callbacks again after a process restart, **before**
calling `resume_all()`:

```python
from nagents import BatchResult


@manager.register_callback("summaries")
async def on_summaries(results: list[BatchResult], context: dict[str, object]) -> None:
    for result in results:
        print(result.custom_id, result.result_type, result.content)


# Resume monitoring pending jobs and handling completed-but-unprocessed jobs.
resumed_ids = await manager.resume_all()
# New jobs can use the registered callback and default automatic monitoring.
job = await manager.create_batch(
    requests,
    callback_name="summaries",
    callback_context={"source": "daily_reports"},
)
```

The application must keep the manager/process alive while monitoring; closing
the manager stops local monitors and releases its HTTP resources, not the remote
provider job. `initialize()` alone does not call `resume_all()`. Design callbacks
to tolerate retries: failed callback processing is not marked complete, so it
can be attempted again. Persistence is not an exactly-once external-action promise.

For lower-level persistence, instantiate `BatchStore(Path("batches.db"))` and
`await store.initialize()`, then use its documented methods yourself. Passing
`store=` to `BatchManager` is not supported. If you only need API operations,
use `BatchClient` with the same explicit credentials/provider/model; its
`get_results(job)` is an **async iterator**, unlike the manager's list-returning
`await get_results(job.id)`.

## Cancelling and failures

```python
job = await manager.cancel_batch(job.id)
print(job.status.value)
```

Cancellation is provider-managed and may remain `CANCELLING`; do not assert
immediate `CANCELLED`. It does not roll back completed requests. Inspect each
request outcome before deciding whether to resubmit anything; never blindly
retry a job whose external outcome is uncertain.

See the generated [Batch API](../api/batch.md) for exact signatures and config
fields, including `BatchConfig`, per-request model/config overrides, and the
supported provider-specific batch routes.
