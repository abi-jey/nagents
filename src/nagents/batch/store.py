"""
SQLite-based batch job persistence.

Enables tracking batch jobs across agent restarts. When the agent starts,
it can resume monitoring pending batches and process their results.

Schema Design:
- v2_batch_jobs: Core batch job metadata and status
- v2_batch_requests: Original requests (for retry/debugging)
- v2_batch_callbacks: Callback info for result processing
"""

import json
import logging
from dataclasses import asdict
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Literal

import aiosqlite

from .types import BatchJob
from .types import BatchRequest
from .types import BatchRequestCounts
from .types import BatchStatus

logger = logging.getLogger(__name__)


class BatchStore:
    """
    SQLite-based persistence for batch jobs.

    Tracks batch jobs across agent restarts, enabling:
    - Resume pending batch monitoring after restart
    - Store original requests for retry on failure
    - Track callback handlers for result processing
    - Query batch history and status

    Example:
        store = BatchStore(Path("batches.db"))
        await store.initialize()

        # Save a new batch
        await store.save_batch(batch_job, requests, callback_name="process_summaries")

        # On restart, get pending batches
        pending = await store.get_pending_batches()
        for job in pending:
            # Resume monitoring...
            pass

        # Mark as processed
        await store.mark_processed(job.id)
    """

    def __init__(self, db_path: Path):
        """
        Initialize the batch store.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        self._initialized = False

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        if self._initialized:
            return

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                -- Core batch job tracking
                CREATE TABLE IF NOT EXISTS v2_batch_jobs (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,  -- 'openai' or 'anthropic'
                    status TEXT NOT NULL,    -- BatchStatus value
                    model TEXT NOT NULL,

                    -- Request counts
                    total_requests INTEGER DEFAULT 0,
                    succeeded INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    cancelled INTEGER DEFAULT 0,
                    expired INTEGER DEFAULT 0,

                    -- Timestamps (ISO format)
                    created_at TEXT,
                    expires_at TEXT,
                    completed_at TEXT,

                    -- Provider-specific IDs
                    input_file_id TEXT,      -- OpenAI
                    output_file_id TEXT,     -- OpenAI
                    error_file_id TEXT,      -- OpenAI
                    results_url TEXT,        -- Anthropic

                    -- Processing state
                    processed INTEGER DEFAULT 0,  -- 0 = pending, 1 = processed
                    processed_at TEXT,

                    -- Callback for result handling
                    callback_name TEXT,      -- Handler function name
                    callback_context TEXT,   -- JSON context for handler

                    -- API connection info (for resuming)
                    base_url TEXT,

                    -- Metadata
                    metadata TEXT,           -- JSON user metadata

                    -- Tracking
                    db_created_at TEXT DEFAULT (datetime('now')),
                    db_updated_at TEXT DEFAULT (datetime('now'))
                );

                -- Store original requests for retry capability
                CREATE TABLE IF NOT EXISTS v2_batch_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    custom_id TEXT NOT NULL,
                    messages TEXT NOT NULL,      -- JSON serialized messages
                    model TEXT,
                    tools TEXT,                  -- JSON serialized tools
                    config TEXT,                 -- JSON serialized config
                    system_prompt TEXT,

                    -- Result tracking
                    result_type TEXT,            -- succeeded, errored, cancelled, expired
                    result_content TEXT,
                    result_error TEXT,

                    FOREIGN KEY (batch_id) REFERENCES v2_batch_jobs(id),
                    UNIQUE (batch_id, custom_id)
                );

                -- Indexes for common queries
                CREATE INDEX IF NOT EXISTS idx_v2_batch_jobs_status
                ON v2_batch_jobs(status);

                CREATE INDEX IF NOT EXISTS idx_v2_batch_jobs_processed
                ON v2_batch_jobs(processed);

                CREATE INDEX IF NOT EXISTS idx_v2_batch_jobs_provider
                ON v2_batch_jobs(provider);

                CREATE INDEX IF NOT EXISTS idx_v2_batch_requests_batch
                ON v2_batch_requests(batch_id);

                CREATE INDEX IF NOT EXISTS idx_v2_batch_requests_custom_id
                ON v2_batch_requests(batch_id, custom_id);
            """
            )
            await db.commit()

        self._initialized = True
        logger.debug(f"Batch store initialized at {self.db_path}")

    async def save_batch(
        self,
        job: BatchJob,
        requests: list[BatchRequest] | None = None,
        *,
        model: str = "",
        base_url: str = "",
        callback_name: str | None = None,
        callback_context: dict[str, Any] | None = None,
    ) -> None:
        """
        Save or update a batch job.

        Args:
            job: The BatchJob to save
            requests: Optional list of original requests (for retry)
            model: Model used for the batch
            base_url: Base URL for API (for resuming)
            callback_name: Name of callback handler for results
            callback_context: Context dict passed to callback handler
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            # Upsert batch job
            await db.execute(
                """INSERT INTO v2_batch_jobs (
                    id, provider, status, model,
                    total_requests, succeeded, failed, cancelled, expired,
                    created_at, expires_at, completed_at,
                    input_file_id, output_file_id, error_file_id, results_url,
                    base_url, callback_name, callback_context, metadata,
                    db_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    total_requests = excluded.total_requests,
                    succeeded = excluded.succeeded,
                    failed = excluded.failed,
                    cancelled = excluded.cancelled,
                    expired = excluded.expired,
                    completed_at = excluded.completed_at,
                    output_file_id = excluded.output_file_id,
                    error_file_id = excluded.error_file_id,
                    results_url = excluded.results_url,
                    db_updated_at = datetime('now')
                """,
                (
                    job.id,
                    job.provider,
                    job.status.value,
                    model,
                    job.request_counts.total,
                    job.request_counts.succeeded,
                    job.request_counts.failed,
                    job.request_counts.cancelled,
                    job.request_counts.expired,
                    job.created_at,
                    job.expires_at,
                    job.completed_at,
                    job.input_file_id,
                    job.output_file_id,
                    job.error_file_id,
                    job.results_url,
                    base_url,
                    callback_name,
                    json.dumps(callback_context) if callback_context else None,
                    json.dumps(job.metadata) if job.metadata else None,
                ),
            )

            # Save requests if provided
            if requests:
                for req in requests:
                    messages_json = json.dumps([asdict(m) for m in req.messages])
                    tools_json = json.dumps([asdict(t) for t in req.tools]) if req.tools else None
                    config_json = json.dumps(asdict(req.config)) if req.config else None

                    await db.execute(
                        """INSERT OR IGNORE INTO v2_batch_requests (
                            batch_id, custom_id, messages, model, tools, config, system_prompt
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            job.id,
                            req.custom_id,
                            messages_json,
                            req.model,
                            tools_json,
                            config_json,
                            req.system_prompt,
                        ),
                    )

            await db.commit()
            logger.debug(f"Saved batch job: {job.id} (status={job.status.value})")

    async def update_status(self, job: BatchJob) -> None:
        """
        Update batch job status and counts.

        Args:
            job: BatchJob with updated status
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE v2_batch_jobs SET
                    status = ?,
                    total_requests = ?,
                    succeeded = ?,
                    failed = ?,
                    cancelled = ?,
                    expired = ?,
                    completed_at = ?,
                    output_file_id = ?,
                    error_file_id = ?,
                    results_url = ?,
                    db_updated_at = datetime('now')
                WHERE id = ?""",
                (
                    job.status.value,
                    job.request_counts.total,
                    job.request_counts.succeeded,
                    job.request_counts.failed,
                    job.request_counts.cancelled,
                    job.request_counts.expired,
                    job.completed_at,
                    job.output_file_id,
                    job.error_file_id,
                    job.results_url,
                    job.id,
                ),
            )
            await db.commit()

    async def get_batch(self, batch_id: str) -> BatchJob | None:
        """
        Get a batch job by ID.

        Args:
            batch_id: The batch ID

        Returns:
            BatchJob or None if not found
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM v2_batch_jobs WHERE id = ?",
                (batch_id,),
            )
            row = await cursor.fetchone()
            return self._row_to_batch_job(row) if row else None

    async def get_pending_batches(
        self,
        provider: Literal["openai", "anthropic"] | None = None,
    ) -> list[BatchJob]:
        """
        Get all pending (unprocessed) batch jobs.

        These are jobs that were submitted but not yet processed
        (either still running or completed but results not handled).

        Args:
            provider: Optional filter by provider

        Returns:
            List of pending BatchJobs
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if provider:
                cursor = await db.execute(
                    """SELECT * FROM v2_batch_jobs
                    WHERE processed = 0 AND provider = ?
                    ORDER BY db_created_at ASC""",
                    (provider,),
                )
            else:
                cursor = await db.execute(
                    """SELECT * FROM v2_batch_jobs
                    WHERE processed = 0
                    ORDER BY db_created_at ASC"""
                )

            rows = await cursor.fetchall()
            return [self._row_to_batch_job(row) for row in rows]

    async def get_active_batches(self) -> list[BatchJob]:
        """
        Get batches that are still in progress.

        Returns:
            List of active BatchJobs
        """
        await self.initialize()

        active_statuses = [
            BatchStatus.VALIDATING.value,
            BatchStatus.IN_PROGRESS.value,
            BatchStatus.FINALIZING.value,
            BatchStatus.CANCELLING.value,
        ]

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join(["?"] * len(active_statuses))
            cursor = await db.execute(
                f"""SELECT * FROM v2_batch_jobs
                WHERE status IN ({placeholders})
                ORDER BY db_created_at ASC""",
                active_statuses,
            )

            rows = await cursor.fetchall()
            return [self._row_to_batch_job(row) for row in rows]

    async def get_completed_unprocessed(self) -> list[BatchJob]:
        """
        Get completed batches that haven't been processed yet.

        These are batches where the API has finished but we haven't
        retrieved and handled the results yet.

        Returns:
            List of completed but unprocessed BatchJobs
        """
        await self.initialize()

        completed_statuses = [
            BatchStatus.COMPLETED.value,
            BatchStatus.ENDED.value,
            BatchStatus.FAILED.value,
            BatchStatus.EXPIRED.value,
            BatchStatus.CANCELLED.value,
        ]

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join(["?"] * len(completed_statuses))
            cursor = await db.execute(
                f"""SELECT * FROM v2_batch_jobs
                WHERE status IN ({placeholders}) AND processed = 0
                ORDER BY db_created_at ASC""",
                completed_statuses,
            )

            rows = await cursor.fetchall()
            return [self._row_to_batch_job(row) for row in rows]

    async def mark_processed(
        self,
        batch_id: str,
        *,
        error: str | None = None,
    ) -> None:
        """
        Mark a batch as processed (results have been handled).

        Args:
            batch_id: The batch ID
            error: Optional error message if processing failed
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            now = datetime.now(UTC).isoformat()
            await db.execute(
                """UPDATE v2_batch_jobs SET
                    processed = 1,
                    processed_at = ?,
                    db_updated_at = datetime('now')
                WHERE id = ?""",
                (now, batch_id),
            )
            await db.commit()
            logger.debug(f"Marked batch as processed: {batch_id}")

    async def save_request_result(
        self,
        batch_id: str,
        custom_id: str,
        result_type: str,
        content: str | None = None,
        error: str | None = None,
    ) -> None:
        """
        Save result for an individual request.

        Args:
            batch_id: The batch ID
            custom_id: The request's custom_id
            result_type: succeeded, errored, cancelled, expired
            content: Response content (for succeeded)
            error: Error message (for errored)
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE v2_batch_requests SET
                    result_type = ?,
                    result_content = ?,
                    result_error = ?
                WHERE batch_id = ? AND custom_id = ?""",
                (result_type, content, error, batch_id, custom_id),
            )
            await db.commit()

    async def get_callback_info(self, batch_id: str) -> tuple[str | None, dict[str, Any] | None]:
        """
        Get callback info for a batch.

        Args:
            batch_id: The batch ID

        Returns:
            Tuple of (callback_name, callback_context)
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT callback_name, callback_context FROM v2_batch_jobs WHERE id = ?",
                (batch_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None, None

            callback_name = row[0]
            callback_context = json.loads(row[1]) if row[1] else None
            return callback_name, callback_context

    async def get_connection_info(self, batch_id: str) -> tuple[str | None, str | None, str | None]:
        """
        Get connection info needed to resume batch monitoring.

        Args:
            batch_id: The batch ID

        Returns:
            Tuple of (provider, model, base_url)
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT provider, model, base_url FROM v2_batch_jobs WHERE id = ?",
                (batch_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None, None, None
            return row[0], row[1], row[2]

    async def get_requests(self, batch_id: str) -> list[BatchRequest]:
        """
        Get original requests for a batch (for retry).

        Args:
            batch_id: The batch ID

        Returns:
            List of BatchRequest objects
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM v2_batch_requests WHERE batch_id = ?",
                (batch_id,),
            )

            rows = await cursor.fetchall()
            requests = []
            for row in rows:
                from ..types import Message

                messages_data = json.loads(row["messages"])
                messages = [Message(**m) for m in messages_data]

                tools = None
                if row["tools"]:
                    from ..types import ToolDefinition

                    tools_data = json.loads(row["tools"])
                    tools = []
                    for t in tools_data:
                        tools.append(
                            ToolDefinition(
                                name=t["name"],
                                description=t["description"],
                                parameters=t.get("parameters", {}),
                            )
                        )

                config = None
                if row["config"]:
                    from ..types import GenerationConfig

                    config = GenerationConfig(**json.loads(row["config"]))

                requests.append(
                    BatchRequest(
                        custom_id=row["custom_id"],
                        messages=messages,
                        model=row["model"],
                        tools=tools,
                        config=config,
                        system_prompt=row["system_prompt"],
                    )
                )

            return requests

    async def list_batches(
        self,
        limit: int = 50,
        offset: int = 0,
        status: BatchStatus | None = None,
        processed: bool | None = None,
    ) -> list[BatchJob]:
        """
        List batch jobs with optional filtering.

        Args:
            limit: Maximum number of results
            offset: Offset for pagination
            status: Optional filter by status
            processed: Optional filter by processed state

        Returns:
            List of BatchJobs
        """
        await self.initialize()

        conditions = []
        params: list[Any] = []

        if status is not None:
            conditions.append("status = ?")
            params.append(status.value)

        if processed is not None:
            conditions.append("processed = ?")
            params.append(1 if processed else 0)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT * FROM v2_batch_jobs
                {where_clause}
                ORDER BY db_created_at DESC
                LIMIT ? OFFSET ?""",
                params,
            )

            rows = await cursor.fetchall()
            return [self._row_to_batch_job(row) for row in rows]

    async def delete_batch(self, batch_id: str) -> bool:
        """
        Delete a batch job and its requests from the database.

        Args:
            batch_id: The batch ID

        Returns:
            True if deleted, False if not found
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            # Delete requests first (foreign key)
            await db.execute(
                "DELETE FROM v2_batch_requests WHERE batch_id = ?",
                (batch_id,),
            )
            cursor = await db.execute(
                "DELETE FROM v2_batch_jobs WHERE id = ?",
                (batch_id,),
            )
            await db.commit()

            deleted: bool = cursor.rowcount is not None and cursor.rowcount > 0
            if deleted:
                logger.debug(f"Deleted batch job: {batch_id}")
            return deleted

    async def cleanup_old_batches(self, days: int = 30) -> int:
        """
        Delete processed batches older than specified days.

        Args:
            days: Delete batches older than this many days

        Returns:
            Number of batches deleted
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            # Get batch IDs to delete
            cursor = await db.execute(
                """SELECT id FROM v2_batch_jobs
                WHERE processed = 1
                AND db_created_at < datetime('now', ?)""",
                (f"-{days} days",),
            )
            rows = await cursor.fetchall()
            batch_ids = [row[0] for row in rows]

            if not batch_ids:
                return 0

            # Delete requests
            placeholders = ",".join(["?"] * len(batch_ids))
            await db.execute(
                f"DELETE FROM v2_batch_requests WHERE batch_id IN ({placeholders})",
                batch_ids,
            )

            # Delete batches
            await db.execute(
                f"DELETE FROM v2_batch_jobs WHERE id IN ({placeholders})",
                batch_ids,
            )

            await db.commit()
            logger.info(f"Cleaned up {len(batch_ids)} old batch jobs")
            return len(batch_ids)

    def _row_to_batch_job(self, row: aiosqlite.Row) -> BatchJob:
        """Convert database row to BatchJob."""
        import contextlib

        status = BatchStatus(row["status"])

        metadata = {}
        if row["metadata"]:
            with contextlib.suppress(json.JSONDecodeError):
                metadata = json.loads(row["metadata"])

        return BatchJob(
            id=row["id"],
            status=status,
            provider=row["provider"],
            request_counts=BatchRequestCounts(
                total=row["total_requests"],
                succeeded=row["succeeded"],
                failed=row["failed"],
                cancelled=row["cancelled"],
                expired=row["expired"],
            ),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            completed_at=row["completed_at"],
            input_file_id=row["input_file_id"],
            output_file_id=row["output_file_id"],
            error_file_id=row["error_file_id"],
            results_url=row["results_url"],
            metadata=metadata,
        )
