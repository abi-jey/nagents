# Configuration

## Provider Configuration

### OpenAI

```python
from nagents import Provider, ProviderType

provider = Provider(
    provider_type=ProviderType.OPENAI_COMPATIBLE,
    api_key="sk-...",
    model="gpt-4o-mini",
    base_url="https://api.openai.com/v1",  # Optional, defaults to OpenAI
)
```

### Anthropic Claude

```python
provider = Provider(
    provider_type=ProviderType.ANTHROPIC,
    api_key="sk-ant-...",
    model="claude-3-5-sonnet-20241022",
)
```

### Google Gemini

```python
provider = Provider(
    provider_type=ProviderType.GEMINI_NATIVE,
    api_key="...",
    model="gemini-2.0-flash",
)
```

## Generation Configuration

Control generation parameters:

```python
from nagents import GenerationConfig

config = GenerationConfig(
    temperature=0.7,      # Creativity (0.0-2.0)
    max_tokens=1000,      # Maximum output tokens
    top_p=0.9,            # Nucleus sampling
    stop=["END"],         # Stop sequences
)

async for event in agent.run(
    "Write a story",
    generation_config=config,
):
    ...
```

## Environment Variables

Use environment variables for API keys:

```python
import os
from dotenv import load_dotenv

load_dotenv()

provider = Provider(
    provider_type=ProviderType.OPENAI_COMPATIBLE,
    api_key=os.getenv("OPENAI_API_KEY"),
    model="gpt-4o-mini",
)
```

Example `.env` file:

```bash
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
```
