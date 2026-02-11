"""
Batch processing client for OpenAI and Anthropic APIs.

Provides a unified interface for:
- Creating batch jobs
- Checking batch status
- Retrieving batch results
- Cancelling batches

Both providers offer 50% cost discount for batch processing.
"""

import io
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..http import HTTPClient
from ..provider import ProviderType
from ..types import GenerationConfig
from ..types import ToolDefinition
from .types import BatchConfig
from .types import BatchJob
from .types import BatchRequest
from .types import BatchRequestCounts
from .types import BatchResult
from .types import BatchStatus

logger = logging.getLogger(__name__)


class BatchClient:
    """
    Unified batch processing client for OpenAI and Anthropic.

    Example:
        async with BatchClient(
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            api_key="sk-...",
            model="gpt-4o-mini",
        ) as client:
            # Create batch requests
            requests = [
                BatchRequest(
                    custom_id="req-1",
                    messages=[Message(role="user", content="Hello!")],
                ),
                BatchRequest(
                    custom_id="req-2",
                    messages=[Message(role="user", content="How are you?")],
                ),
            ]

            # Submit batch
            job = await client.create_batch(requests)
            print(f"Batch created: {job.id}")

            # Poll for completion
            while job.status in (BatchStatus.VALIDATING, BatchStatus.IN_PROGRESS):
                await asyncio.sleep(60)
                job = await client.get_batch(job.id)

            # Get results
            async for result in client.get_results(job):
                print(f"{result.custom_id}: {result.content}")
    """

    def __init__(
        self,
        provider_type: ProviderType,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 120.0,
    ):
        """
        Initialize the batch client.

        Args:
            provider_type: OPENAI_COMPATIBLE or ANTHROPIC
            api_key: API key for authentication
            model: Default model for batch requests
            base_url: Optional custom base URL
            timeout: Request timeout in seconds
        """
        if provider_type == ProviderType.GEMINI_NATIVE:
            raise ValueError("Batch processing is not supported for Gemini native API. Use Vertex AI instead.")

        self.provider_type = provider_type
        self.api_key = api_key
        self.model = model
        self._http = HTTPClient(timeout=timeout)

        # Set default base URLs
        if base_url:
            self.base_url = base_url.rstrip("/")
        elif provider_type == ProviderType.OPENAI_COMPATIBLE:
            self.base_url = "https://api.openai.com/v1"
        else:  # ANTHROPIC
            self.base_url = "https://api.anthropic.com/v1"

    def _get_headers(self) -> dict[str, str]:
        """Get headers for API requests."""
        if self.provider_type == ProviderType.OPENAI_COMPATIBLE:
            return {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        else:  # ANTHROPIC
            return {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }

    # =========================================================================
    # OpenAI Batch Implementation
    # =========================================================================

    async def _create_batch_openai(
        self,
        requests: list[BatchRequest],
        config: BatchConfig | None = None,
        tools: list[ToolDefinition] | None = None,
        generation_config: GenerationConfig | None = None,
    ) -> BatchJob:
        """Create a batch job using OpenAI's file-based API."""
        from ..adapters import openai as openai_adapter

        config = config or BatchConfig()

        # Build JSONL content
        lines = []
        for req in requests:
            # Format messages
            formatted_messages = openai_adapter.format_messages(req.messages)

            body: dict[str, Any] = {
                "model": req.model or self.model,
                "messages": formatted_messages,
            }

            # Add tools
            req_tools = req.tools or tools
            if req_tools:
                body["tools"] = openai_adapter.format_tools(req_tools)

            # Add generation config
            req_config = req.config or generation_config
            if req_config:
                if req_config.temperature is not None:
                    body["temperature"] = req_config.temperature
                if req_config.max_tokens is not None:
                    body["max_completion_tokens"] = req_config.max_tokens
                if req_config.top_p is not None:
                    body["top_p"] = req_config.top_p
                if req_config.stop:
                    body["stop"] = req_config.stop

            line = {
                "custom_id": req.custom_id,
                "method": "POST",
                "url": config.endpoint,
                "body": body,
            }
            lines.append(json.dumps(line))

        jsonl_content = "\n".join(lines)

        # Step 1: Upload the file
        upload_url = f"{self.base_url}/files"
        headers = self._get_headers()

        # Use multipart form data for file upload
        import aiohttp

        form = aiohttp.FormData()
        form.add_field("purpose", "batch")
        form.add_field(
            "file",
            io.BytesIO(jsonl_content.encode("utf-8")),
            filename="batch_input.jsonl",
            content_type="application/jsonl",
        )

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                upload_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=form,
            ) as resp,
        ):
            if resp.status >= 400:
                error_text = await resp.text()
                raise RuntimeError(f"Failed to upload batch file: {resp.status} {error_text}")
            file_response = await resp.json()

        input_file_id = file_response["id"]
        logger.info(f"Uploaded batch input file: {input_file_id}")

        # Step 2: Create the batch
        batch_url = f"{self.base_url}/batches"
        batch_body = {
            "input_file_id": input_file_id,
            "endpoint": config.endpoint,
            "completion_window": config.completion_window,
        }
        if config.metadata:
            batch_body["metadata"] = config.metadata

        response = await self._http.post_json(batch_url, batch_body, headers)

        return self._parse_openai_batch(response)

    def _parse_openai_batch(self, response: dict[str, Any]) -> BatchJob:
        """Parse OpenAI batch response into BatchJob."""
        status_map = {
            "validating": BatchStatus.VALIDATING,
            "failed": BatchStatus.FAILED,
            "in_progress": BatchStatus.IN_PROGRESS,
            "finalizing": BatchStatus.FINALIZING,
            "completed": BatchStatus.COMPLETED,
            "expired": BatchStatus.EXPIRED,
            "cancelling": BatchStatus.CANCELLING,
            "cancelled": BatchStatus.CANCELLED,
        }

        counts = response.get("request_counts", {})

        return BatchJob(
            id=response["id"],
            status=status_map.get(response.get("status", ""), BatchStatus.IN_PROGRESS),
            provider="openai",
            request_counts=BatchRequestCounts(
                total=counts.get("total", 0),
                succeeded=counts.get("completed", 0),
                failed=counts.get("failed", 0),
            ),
            created_at=response.get("created_at"),
            expires_at=response.get("expires_at"),
            completed_at=response.get("completed_at"),
            input_file_id=response.get("input_file_id"),
            output_file_id=response.get("output_file_id"),
            error_file_id=response.get("error_file_id"),
            endpoint=response.get("endpoint"),
            metadata=response.get("metadata", {}),
            raw_response=response,
        )

    async def _get_batch_openai(self, batch_id: str) -> BatchJob:
        """Get OpenAI batch status."""
        url = f"{self.base_url}/batches/{batch_id}"
        response = await self._http.get_json(url, self._get_headers())
        return self._parse_openai_batch(response)

    async def _get_results_openai(self, job: BatchJob) -> AsyncIterator[BatchResult]:
        """Stream results from OpenAI batch."""
        if not job.output_file_id:
            logger.warning("No output file ID available")
            return

        url = f"{self.base_url}/files/{job.output_file_id}/content"
        headers = self._get_headers()

        # Download file content
        import aiohttp

        async with (
            aiohttp.ClientSession() as session,
            session.get(url, headers=headers) as resp,
        ):
            if resp.status >= 400:
                error_text = await resp.text()
                raise RuntimeError(f"Failed to download results: {resp.status} {error_text}")
            content = await resp.text()

        # Parse JSONL
        for line in content.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                yield self._parse_openai_result(data)
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse result line: {line[:100]}")

    def _parse_openai_result(self, data: dict[str, Any]) -> BatchResult:
        """Parse OpenAI batch result."""
        custom_id = data.get("custom_id", "")
        error = data.get("error")
        response = data.get("response", {})

        if error:
            return BatchResult(
                custom_id=custom_id,
                result_type="errored",
                error_type=error.get("code", "unknown"),
                error_message=error.get("message", "Unknown error"),
                raw_response=data,
            )

        # Check response status
        status_code = response.get("status_code", 200)
        if status_code >= 400:
            return BatchResult(
                custom_id=custom_id,
                result_type="errored",
                error_type=f"http_{status_code}",
                error_message=str(response.get("body", {})),
                raw_response=data,
            )

        # Parse successful response
        body = response.get("body", {})
        choices = body.get("choices", [])

        content = None
        tool_calls = None

        if choices:
            message = choices[0].get("message", {})
            content = message.get("content")
            if message.get("tool_calls"):
                tool_calls = message["tool_calls"]

        return BatchResult(
            custom_id=custom_id,
            result_type="succeeded",
            content=content,
            tool_calls=tool_calls,
            usage=body.get("usage"),
            raw_response=data,
        )

    async def _cancel_batch_openai(self, batch_id: str) -> BatchJob:
        """Cancel OpenAI batch."""
        url = f"{self.base_url}/batches/{batch_id}/cancel"
        response = await self._http.post_json(url, {}, self._get_headers())
        return self._parse_openai_batch(response)

    async def _list_batches_openai(
        self, limit: int = 20, after: str | None = None
    ) -> tuple[list[BatchJob], str | None]:
        """List OpenAI batches."""
        url = f"{self.base_url}/batches?limit={limit}"
        if after:
            url += f"&after={after}"

        response = await self._http.get_json(url, self._get_headers())

        jobs = [self._parse_openai_batch(b) for b in response.get("data", [])]
        next_cursor = response.get("last_id") if response.get("has_more") else None

        return jobs, next_cursor

    # =========================================================================
    # Anthropic Batch Implementation
    # =========================================================================

    async def _create_batch_anthropic(
        self,
        requests: list[BatchRequest],
        config: BatchConfig | None = None,
        tools: list[ToolDefinition] | None = None,
        generation_config: GenerationConfig | None = None,
    ) -> BatchJob:
        """Create a batch job using Anthropic's Messages Batches API."""
        from ..adapters import anthropic as anthropic_adapter

        batch_requests = []
        for req in requests:
            # Format messages (extracts system prompt)
            system_prompt, formatted_messages = anthropic_adapter.format_messages(req.messages)

            # Use explicit system prompt if provided
            if req.system_prompt:
                system_prompt = req.system_prompt

            params: dict[str, Any] = {
                "model": req.model or self.model,
                "messages": formatted_messages,
                "max_tokens": 4096,  # Required by Anthropic
            }

            if system_prompt:
                params["system"] = system_prompt

            # Add tools
            req_tools = req.tools or tools
            if req_tools:
                params["tools"] = anthropic_adapter.format_tools(req_tools)

            # Add generation config
            req_config = req.config or generation_config
            if req_config:
                if req_config.max_tokens:
                    params["max_tokens"] = req_config.max_tokens
                if req_config.temperature is not None:
                    params["temperature"] = req_config.temperature
                if req_config.top_p is not None:
                    params["top_p"] = req_config.top_p
                if req_config.stop:
                    params["stop_sequences"] = req_config.stop

            batch_requests.append({"custom_id": req.custom_id, "params": params})

        # Create batch
        url = f"{self.base_url}/messages/batches"
        body = {"requests": batch_requests}

        response = await self._http.post_json(url, body, self._get_headers())

        return self._parse_anthropic_batch(response)

    def _parse_anthropic_batch(self, response: dict[str, Any]) -> BatchJob:
        """Parse Anthropic batch response into BatchJob."""
        status_map = {
            "in_progress": BatchStatus.IN_PROGRESS,
            "ended": BatchStatus.COMPLETED,
            "canceling": BatchStatus.CANCELLING,
        }

        counts = response.get("request_counts", {})

        # Determine final status
        processing_status = response.get("processing_status", "in_progress")
        status = status_map.get(processing_status, BatchStatus.IN_PROGRESS)

        # If ended, check if all succeeded or mixed
        if processing_status == "ended":
            if counts.get("expired", 0) > 0 and counts.get("succeeded", 0) == 0:
                status = BatchStatus.EXPIRED
            elif counts.get("canceled", 0) == counts.get("total", 0):
                status = BatchStatus.CANCELLED

        return BatchJob(
            id=response["id"],
            status=status,
            provider="anthropic",
            request_counts=BatchRequestCounts(
                total=counts.get("processing", 0)
                + counts.get("succeeded", 0)
                + counts.get("errored", 0)
                + counts.get("canceled", 0)
                + counts.get("expired", 0),
                processing=counts.get("processing", 0),
                succeeded=counts.get("succeeded", 0),
                failed=counts.get("errored", 0),
                cancelled=counts.get("canceled", 0),
                expired=counts.get("expired", 0),
            ),
            created_at=response.get("created_at"),
            expires_at=response.get("expires_at"),
            completed_at=response.get("ended_at"),
            cancelled_at=response.get("cancel_initiated_at"),
            results_url=response.get("results_url"),
            raw_response=response,
        )

    async def _get_batch_anthropic(self, batch_id: str) -> BatchJob:
        """Get Anthropic batch status."""
        url = f"{self.base_url}/messages/batches/{batch_id}"
        response = await self._http.get_json(url, self._get_headers())
        return self._parse_anthropic_batch(response)

    async def _get_results_anthropic(self, job: BatchJob) -> AsyncIterator[BatchResult]:
        """Stream results from Anthropic batch."""
        if not job.results_url:
            logger.warning("No results URL available")
            return

        headers = self._get_headers()

        # Stream results from URL
        import aiohttp

        async with aiohttp.ClientSession() as session:  # noqa: SIM117
            async with session.get(job.results_url, headers=headers) as resp:
                if resp.status >= 400:
                    error_text = await resp.text()
                    raise RuntimeError(f"Failed to download results: {resp.status} {error_text}")

                # Stream line by line
                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str:
                        continue
                    try:
                        data = json.loads(line_str)
                        yield self._parse_anthropic_result(data)
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse result line: {line_str[:100]}")

    def _parse_anthropic_result(self, data: dict[str, Any]) -> BatchResult:
        """Parse Anthropic batch result."""
        custom_id = data.get("custom_id", "")
        result = data.get("result", {})
        result_type = result.get("type", "errored")

        if result_type == "succeeded":
            message = result.get("message", {})
            content_blocks = message.get("content", [])

            # Extract text content
            text_parts = []
            tool_calls = []
            for block in content_blocks:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(block)

            return BatchResult(
                custom_id=custom_id,
                result_type="succeeded",
                content="\n".join(text_parts) if text_parts else None,
                tool_calls=tool_calls if tool_calls else None,
                usage=message.get("usage"),
                raw_response=data,
            )

        elif result_type == "errored":
            error = result.get("error", {})
            return BatchResult(
                custom_id=custom_id,
                result_type="errored",
                error_type=error.get("type", "unknown"),
                error_message=error.get("message", "Unknown error"),
                raw_response=data,
            )

        elif result_type == "canceled":
            return BatchResult(
                custom_id=custom_id,
                result_type="cancelled",
                raw_response=data,
            )

        else:  # expired
            return BatchResult(
                custom_id=custom_id,
                result_type="expired",
                raw_response=data,
            )

    async def _cancel_batch_anthropic(self, batch_id: str) -> BatchJob:
        """Cancel Anthropic batch."""
        url = f"{self.base_url}/messages/batches/{batch_id}/cancel"
        response = await self._http.post_json(url, {}, self._get_headers())
        return self._parse_anthropic_batch(response)

    async def _list_batches_anthropic(
        self, limit: int = 20, after: str | None = None
    ) -> tuple[list[BatchJob], str | None]:
        """List Anthropic batches."""
        url = f"{self.base_url}/messages/batches?limit={limit}"
        if after:
            url += f"&after_id={after}"

        response = await self._http.get_json(url, self._get_headers())

        jobs = [self._parse_anthropic_batch(b) for b in response.get("data", [])]
        next_cursor = response.get("last_id") if response.get("has_more") else None

        return jobs, next_cursor

    # =========================================================================
    # Public Interface
    # =========================================================================

    async def create_batch(
        self,
        requests: list[BatchRequest],
        config: BatchConfig | None = None,
        tools: list[ToolDefinition] | None = None,
        generation_config: GenerationConfig | None = None,
    ) -> BatchJob:
        """
        Create a batch processing job.

        Args:
            requests: List of BatchRequest objects
            config: Optional batch configuration
            tools: Optional default tools for all requests
            generation_config: Optional default generation config

        Returns:
            BatchJob with ID and initial status
        """
        if self.provider_type == ProviderType.OPENAI_COMPATIBLE:
            return await self._create_batch_openai(requests, config, tools, generation_config)
        else:
            return await self._create_batch_anthropic(requests, config, tools, generation_config)

    async def get_batch(self, batch_id: str) -> BatchJob:
        """
        Get the current status of a batch job.

        Args:
            batch_id: The batch ID

        Returns:
            Updated BatchJob
        """
        if self.provider_type == ProviderType.OPENAI_COMPATIBLE:
            return await self._get_batch_openai(batch_id)
        else:
            return await self._get_batch_anthropic(batch_id)

    async def get_results(self, job: BatchJob) -> AsyncIterator[BatchResult]:
        """
        Stream results from a completed batch job.

        Args:
            job: The BatchJob (must be completed)

        Yields:
            BatchResult for each request
        """
        if job.provider == "openai":
            async for result in self._get_results_openai(job):
                yield result
        else:
            async for result in self._get_results_anthropic(job):
                yield result

    async def cancel_batch(self, batch_id: str) -> BatchJob:
        """
        Cancel a batch job.

        Args:
            batch_id: The batch ID

        Returns:
            Updated BatchJob (status will be CANCELLING)
        """
        if self.provider_type == ProviderType.OPENAI_COMPATIBLE:
            return await self._cancel_batch_openai(batch_id)
        else:
            return await self._cancel_batch_anthropic(batch_id)

    async def list_batches(self, limit: int = 20, after: str | None = None) -> tuple[list[BatchJob], str | None]:
        """
        List batch jobs.

        Args:
            limit: Maximum number of batches to return
            after: Cursor for pagination

        Returns:
            Tuple of (list of BatchJobs, next cursor or None)
        """
        if self.provider_type == ProviderType.OPENAI_COMPATIBLE:
            return await self._list_batches_openai(limit, after)
        else:
            return await self._list_batches_anthropic(limit, after)

    async def wait_for_completion(
        self,
        batch_id: str,
        poll_interval: float = 60.0,
        max_wait: float | None = None,
    ) -> BatchJob:
        """
        Wait for a batch to complete.

        Args:
            batch_id: The batch ID
            poll_interval: Seconds between status checks
            max_wait: Maximum seconds to wait (None for no limit)

        Returns:
            Completed BatchJob

        Raises:
            TimeoutError: If max_wait is exceeded
        """
        import asyncio
        import time

        start = time.time()
        active_statuses = {
            BatchStatus.VALIDATING,
            BatchStatus.IN_PROGRESS,
            BatchStatus.FINALIZING,
        }

        while True:
            job = await self.get_batch(batch_id)

            if job.status not in active_statuses:
                return job

            if max_wait and (time.time() - start) > max_wait:
                raise TimeoutError(f"Batch {batch_id} did not complete within {max_wait} seconds")

            await asyncio.sleep(poll_interval)

    async def close(self) -> None:
        """Close the client and release resources."""
        await self._http.close()

    async def __aenter__(self) -> "BatchClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
