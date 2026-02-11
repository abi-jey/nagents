# Batch Processing

Process multiple requests efficiently with batch processing.

## Overview

Batch processing allows you to:

- Submit multiple requests at once
- Process them in the background
- Retrieve results later
- Track job status

## Basic Usage

```python
from nagents import BatchManager, BatchRequest

batch_manager = BatchManager(provider=provider)

# Create batch requests
requests = [
    BatchRequest(id="req-1", messages=[{"role": "user", "content": "Hello"}]),
    BatchRequest(id="req-2", messages=[{"role": "user", "content": "Hi there"}]),
    BatchRequest(id="req-3", messages=[{"role": "user", "content": "Greetings"}]),
]

# Submit batch job
job = await batch_manager.submit(requests)
print(f"Job ID: {job.id}")

# Check status
status = await batch_manager.get_status(job.id)
print(f"Status: {status}")

# Get results when complete
results = await batch_manager.get_results(job.id)
for result in results:
    print(f"{result.id}: {result.response}")
```

## Batch Status

Jobs go through these states:

| Status | Description |
|--------|-------------|
| `pending` | Job submitted, waiting to start |
| `processing` | Job is being processed |
| `completed` | All requests finished |
| `failed` | Job failed |
| `cancelled` | Job was cancelled |

## Polling for Results

```python
import asyncio

job = await batch_manager.submit(requests)

while True:
    status = await batch_manager.get_status(job.id)
    if status == BatchStatus.COMPLETED:
        break
    elif status == BatchStatus.FAILED:
        raise Exception("Batch job failed")
    await asyncio.sleep(5)  # Poll every 5 seconds

results = await batch_manager.get_results(job.id)
```

## Batch Store

Persist batch jobs and results:

```python
from nagents import BatchStore

store = BatchStore(Path("batches.db"))
await store.initialize()

batch_manager = BatchManager(
    provider=provider,
    store=store,
)

# Jobs and results are automatically persisted
```

## Cancelling Jobs

```python
await batch_manager.cancel(job.id)
```

## Error Handling

Individual requests can fail without failing the entire batch:

```python
results = await batch_manager.get_results(job.id)
for result in results:
    if result.error:
        print(f"Request {result.id} failed: {result.error}")
    else:
        print(f"Request {result.id}: {result.response}")
```
