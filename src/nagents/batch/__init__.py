"""
Batch processing module for the v2 LLM integration.

Provides batch processing support for OpenAI and Anthropic APIs.
Both providers offer 50% cost discount for batch processing.

Two usage patterns:

1. BatchClient - Direct API client (no persistence):
    ```python
    async with BatchClient(...) as client:
        job = await client.create_batch(requests)
        job = await client.wait_for_completion(job.id)
        async for result in client.get_results(job):
            print(result.content)
    ```

2. BatchManager - With persistence and callbacks (survives restarts):
    ```python
    manager = BatchManager(
        api_key="sk-...",
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        model="gpt-4o-mini",
        db_path=Path("~/.homelab/batches.db"),
    )

    @manager.register_callback("summarize")
    async def handle_summaries(results, context):
        for r in results:
            save_to_db(r.custom_id, r.content)

    # Create batch - automatically monitored in background
    job = await manager.create_batch(
        requests,
        callback_name="summarize",
        callback_context={"source": "reports"},
    )

    # On restart, resume all pending batches
    await manager.resume_all()
    ```
"""

from .client import BatchClient
from .manager import BatchManager
from .store import BatchStore
from .types import BatchConfig
from .types import BatchJob
from .types import BatchRequest
from .types import BatchRequestCounts
from .types import BatchResult
from .types import BatchStatus

__all__ = [
    "BatchClient",
    "BatchConfig",
    "BatchJob",
    "BatchManager",
    "BatchRequest",
    "BatchRequestCounts",
    "BatchResult",
    "BatchStatus",
    "BatchStore",
]
