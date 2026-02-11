"""
Batch processing manager with persistence and callbacks.

Provides a high-level interface for batch processing that:
- Persists batch jobs across agent restarts
- Supports callback handlers for result processing
- Automatically resumes monitoring pending batches
- Handles the full lifecycle: create → monitor → process results

Example:
    manager = BatchManager(
        api_key=os.environ["OPENAI_API_KEY"],
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        model="gpt-4o-mini",
        db_path=Path("~/.homelab/batches.db"),
    )

    # Register callback handlers
    @manager.register_callback("summarize")
    async def handle_summaries(results: list[BatchResult], context: dict):
        for result in results:
            print(f"Summary {result.custom_id}: {result.content}")

    # Create batch with callback
    job = await manager.create_batch(
        requests=[...],
        callback_name="summarize",
        callback_context={"source": "daily_reports"},
    )

    # On restart, resume all pending batches
    await manager.resume_all()
"""

import asyncio
import logging
from collections.abc import Awaitable
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..provider import ProviderType
from ..types import GenerationConfig
from ..types import ToolDefinition
from .client import BatchClient
from .store import BatchStore
from .types import BatchConfig
from .types import BatchJob
from .types import BatchRequest
from .types import BatchResult
from .types import BatchStatus

logger = logging.getLogger(__name__)

# Type for callback handlers
BatchCallback = Callable[[list[BatchResult], dict[str, Any]], Awaitable[None]]


class BatchManager:
    """
    High-level batch processing manager with persistence.

    Combines BatchClient (API operations) with BatchStore (persistence)
    to provide a complete batch processing solution that survives
    agent restarts.

    Features:
    - Automatic persistence of batch jobs
    - Named callback handlers for result processing
    - Resume pending batches on startup
    - Background monitoring of active batches
    """

    def __init__(
        self,
        api_key: str,
        provider_type: ProviderType,
        model: str,
        db_path: Path,
        *,
        base_url: str | None = None,
        poll_interval: float = 60.0,
    ):
        """
        Initialize the batch manager.

        Args:
            api_key: API key for the provider
            provider_type: OPENAI_COMPATIBLE or ANTHROPIC
            model: Default model for batch requests
            db_path: Path to SQLite database for persistence
            base_url: Optional custom base URL
            poll_interval: Seconds between status checks (default 60)
        """
        self.api_key = api_key
        self.provider_type = provider_type
        self.model = model
        self.base_url = base_url
        self.poll_interval = poll_interval

        # Initialize components
        self._store = BatchStore(db_path)
        self._client = BatchClient(
            provider_type=provider_type,
            api_key=api_key,
            model=model,
            base_url=base_url,
        )

        # Callback registry
        self._callbacks: dict[str, BatchCallback] = {}

        # Background task tracking
        self._monitor_tasks: dict[str, asyncio.Task[None]] = {}
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the store and resume pending batches."""
        if self._initialized:
            return

        await self._store.initialize()
        self._initialized = True
        logger.info("BatchManager initialized")

    def register_callback(
        self,
        name: str,
    ) -> Callable[[BatchCallback], BatchCallback]:
        """
        Decorator to register a callback handler.

        Args:
            name: Unique name for this callback

        Example:
            @manager.register_callback("process_summaries")
            async def handler(results: list[BatchResult], context: dict):
                for result in results:
                    save_summary(result.custom_id, result.content)
        """

        def decorator(func: BatchCallback) -> BatchCallback:
            self._callbacks[name] = func
            logger.debug(f"Registered batch callback: {name}")
            return func

        return decorator

    def add_callback(self, name: str, callback: BatchCallback) -> None:
        """
        Add a callback handler directly.

        Args:
            name: Unique name for this callback
            callback: Async function to handle results
        """
        self._callbacks[name] = callback
        logger.debug(f"Added batch callback: {name}")

    async def create_batch(
        self,
        requests: list[BatchRequest],
        *,
        config: BatchConfig | None = None,
        tools: list[ToolDefinition] | None = None,
        generation_config: GenerationConfig | None = None,
        callback_name: str | None = None,
        callback_context: dict[str, Any] | None = None,
        store_requests: bool = True,
        auto_monitor: bool = True,
    ) -> BatchJob:
        """
        Create a batch job with persistence.

        Args:
            requests: List of BatchRequest objects
            config: Optional batch configuration
            tools: Optional default tools for all requests
            generation_config: Optional default generation config
            callback_name: Name of registered callback for results
            callback_context: Context dict passed to callback
            store_requests: Whether to store requests for retry (default True)
            auto_monitor: Whether to start background monitoring (default True)

        Returns:
            The created BatchJob
        """
        await self.initialize()

        # Validate callback if provided
        if callback_name and callback_name not in self._callbacks:
            raise ValueError(f"Unknown callback: {callback_name}. Available: {list(self._callbacks.keys())}")

        # Create batch via API
        job = await self._client.create_batch(requests, config, tools, generation_config)

        # Persist to database
        await self._store.save_batch(
            job,
            requests=requests if store_requests else None,
            model=self.model,
            base_url=self.base_url or "",
            callback_name=callback_name,
            callback_context=callback_context,
        )

        logger.info(f"Created batch {job.id} with {len(requests)} requests")

        # Start background monitoring
        if auto_monitor:
            self._start_monitor(job.id)

        return job

    async def get_batch(self, batch_id: str) -> BatchJob | None:
        """
        Get a batch job by ID.

        Fetches from API and updates local store.

        Args:
            batch_id: The batch ID

        Returns:
            Updated BatchJob or None if not found
        """
        await self.initialize()

        try:
            job = await self._client.get_batch(batch_id)
            await self._store.update_status(job)
            return job
        except Exception as e:
            logger.warning(f"Failed to get batch {batch_id} from API: {e}")
            # Fall back to stored data
            return await self._store.get_batch(batch_id)

    async def get_results(self, batch_id: str) -> list[BatchResult]:
        """
        Get results for a completed batch.

        Args:
            batch_id: The batch ID

        Returns:
            List of BatchResult objects
        """
        await self.initialize()

        job = await self._client.get_batch(batch_id)
        if job.status not in (BatchStatus.COMPLETED, BatchStatus.ENDED):
            raise ValueError(f"Batch {batch_id} is not completed (status={job.status})")

        results = []
        async for result in self._client.get_results(job):
            results.append(result)
            # Save individual result
            await self._store.save_request_result(
                batch_id,
                result.custom_id,
                result.result_type,
                content=result.content,
                error=result.error_message,
            )

        return results

    async def cancel_batch(self, batch_id: str) -> BatchJob:
        """
        Cancel a batch job.

        Args:
            batch_id: The batch ID

        Returns:
            Updated BatchJob
        """
        await self.initialize()

        job = await self._client.cancel_batch(batch_id)
        await self._store.update_status(job)

        # Stop monitoring if active
        if batch_id in self._monitor_tasks:
            self._monitor_tasks[batch_id].cancel()
            del self._monitor_tasks[batch_id]

        logger.info(f"Cancelled batch {batch_id}")
        return job

    async def resume_all(self) -> list[str]:
        """
        Resume monitoring all pending batches.

        Call this on startup to resume any batches that were
        submitted before the agent was shut down.

        Returns:
            List of batch IDs that were resumed
        """
        await self.initialize()

        resumed = []

        # Get active batches (still processing)
        active = await self._store.get_active_batches()
        for job in active:
            if job.id not in self._monitor_tasks:
                self._start_monitor(job.id)
                resumed.append(job.id)
                logger.info(f"Resumed monitoring batch {job.id}")

        # Process completed but unhandled batches
        completed = await self._store.get_completed_unprocessed()
        for job in completed:
            await self._process_completed_batch(job.id)
            resumed.append(job.id)
            logger.info(f"Processing completed batch {job.id}")

        return resumed

    async def list_pending(self) -> list[BatchJob]:
        """
        List all pending (unprocessed) batches.

        Returns:
            List of pending BatchJobs
        """
        await self.initialize()
        return await self._store.get_pending_batches()

    async def list_batches(
        self,
        limit: int = 50,
        status: BatchStatus | None = None,
        processed: bool | None = None,
    ) -> list[BatchJob]:
        """
        List batch jobs with optional filtering.

        Args:
            limit: Maximum number of results
            status: Optional filter by status
            processed: Optional filter by processed state

        Returns:
            List of BatchJobs
        """
        await self.initialize()
        return await self._store.list_batches(limit=limit, status=status, processed=processed)

    async def cleanup(self, days: int = 30) -> int:
        """
        Delete old processed batches.

        Args:
            days: Delete batches older than this many days

        Returns:
            Number of batches deleted
        """
        await self.initialize()
        return await self._store.cleanup_old_batches(days)

    async def close(self) -> None:
        """Close the manager and release resources."""
        # Cancel all monitor tasks
        for task in self._monitor_tasks.values():
            task.cancel()
        self._monitor_tasks.clear()

        await self._client.close()

    async def __aenter__(self) -> "BatchManager":
        await self.initialize()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _start_monitor(self, batch_id: str) -> None:
        """Start background monitoring for a batch."""
        if batch_id in self._monitor_tasks:
            return

        task = asyncio.create_task(self._monitor_batch(batch_id))
        self._monitor_tasks[batch_id] = task

    async def _monitor_batch(self, batch_id: str) -> None:
        """Background task to monitor batch until completion."""
        active_statuses = {
            BatchStatus.VALIDATING,
            BatchStatus.IN_PROGRESS,
            BatchStatus.FINALIZING,
        }

        try:
            while True:
                try:
                    job = await self._client.get_batch(batch_id)
                    await self._store.update_status(job)

                    if job.status not in active_statuses:
                        logger.info(f"Batch {batch_id} completed with status {job.status}")
                        await self._process_completed_batch(batch_id)
                        break

                    logger.debug(
                        f"Batch {batch_id}: {job.status.value} "
                        f"({job.request_counts.succeeded}/{job.request_counts.total})"
                    )

                except Exception as e:
                    logger.error(f"Error monitoring batch {batch_id}: {e}")

                await asyncio.sleep(self.poll_interval)

        except asyncio.CancelledError:
            logger.debug(f"Monitor task cancelled for batch {batch_id}")
            raise

        finally:
            # Clean up task reference
            self._monitor_tasks.pop(batch_id, None)

    async def _process_completed_batch(self, batch_id: str) -> None:
        """Process results for a completed batch."""
        try:
            # Get callback info
            callback_name, callback_context = await self._store.get_callback_info(batch_id)

            if callback_name and callback_name in self._callbacks:
                # Fetch results
                results = await self.get_results(batch_id)

                # Call the handler
                callback = self._callbacks[callback_name]
                await callback(results, callback_context or {})

                logger.info(f"Processed batch {batch_id} with callback {callback_name}")
            else:
                logger.debug(f"No callback for batch {batch_id}")

            # Mark as processed
            await self._store.mark_processed(batch_id)

        except Exception as e:
            logger.error(f"Failed to process batch {batch_id}: {e}")
            # Don't mark as processed so it can be retried
            raise
