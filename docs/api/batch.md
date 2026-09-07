# Batch API

`BatchClient` performs provider API operations. `BatchStore` persists job state
in SQLite. `BatchManager` combines them with named callbacks and monitoring.
These are delayed jobs, not ordinary streaming generations. See
[Batch Processing](../guide/batch.md) for usage.

Batch support is limited to the OpenAI/Azure and Anthropic routes implemented
by the batch client; do not infer support from a provider's text-generation
support. Endpoint availability, quotas, and pricing are provider-specific.
Register callbacks again before resuming stored jobs, and close the manager
when finished. The reference follows the current source checkout.

::: nagents.BatchManager
    options:
      members:
        - __init__
        - initialize
        - register_callback
        - add_callback
        - create_batch
        - resume_all
        - close

::: nagents.BatchClient
    options:
      members:
        - __init__
        - create_batch
        - get_batch
        - get_results
        - cancel_batch
        - close

::: nagents.BatchStore
    options:
      members:
        - __init__
        - initialize

::: nagents.BatchRequest

::: nagents.BatchConfig

::: nagents.BatchJob

::: nagents.BatchRequestCounts

::: nagents.BatchResult

::: nagents.BatchStatus
