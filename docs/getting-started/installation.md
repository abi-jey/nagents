# Installation

## Requirements

!!! info "Prerequisites"
    - Python 3.11 or higher
    - An API key from OpenAI, Anthropic, or Google

## Install from PyPI

=== "pip (Recommended)"

    ```bash
    pip install nagents
    ```

=== "uv"

    ```bash
    uv add nagents
    ```

=== "poetry"

    ```bash
    poetry add nagents
    ```

## Install with Extras

=== "Development"

    ```bash
    pip install nagents[dev]
    ```

    This includes:

    - [x] pytest, pytest-asyncio, pytest-cov for testing
    - [x] ruff for linting and formatting
    - [x] mypy for type checking
    - [x] pre-commit for git hooks

=== "Documentation"

    ```bash
    pip install nagents[docs]
    ```

    This includes MkDocs and Material for MkDocs theme.

=== "All Extras"

    ```bash
    pip install nagents[dev,docs]
    ```

## Install from Source

```bash
git clone https://github.com/abi-jey/nagents.git
cd nagents
pip install -e ".[dev]"
```

!!! tip "Editable Install"
    The `-e` flag installs the package in "editable" mode, so changes to the source code are immediately reflected without reinstalling.

## Verify Installation

```python
from nagents import Agent, Provider, ProviderType, SessionManager

print("nagents installed successfully!")
```

??? note "Check Version"
    ```python
    import nagents

    print(nagents.__version__)
    ```

## Environment Setup

!!! warning "API Keys Required"
    You'll need an API key from at least one provider to use nagents.

Create a `.env` file in your project root:

```bash title=".env"
# Choose one or more providers
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
```

Then load it in your code:

```python
import os
from dotenv import load_dotenv

load_dotenv()  # (1)!

api_key = os.getenv("OPENAI_API_KEY")
```

1. Requires `pip install python-dotenv`
