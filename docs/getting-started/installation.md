# Installation

## Requirements

- Python 3.11 or higher
- An API key from OpenAI, Anthropic, or Google

## Install from PyPI

```bash
pip install nagents
```

## Install with Development Dependencies

```bash
pip install nagents[dev]
```

This includes:

- pytest, pytest-asyncio, pytest-cov for testing
- ruff for linting and formatting
- mypy for type checking
- pre-commit for git hooks

## Install with Documentation Dependencies

```bash
pip install nagents[docs]
```

## Install from Source

```bash
git clone https://github.com/abi-jey/nagents.git
cd nagents
pip install -e ".[dev]"
```

## Verify Installation

```python
from nagents import Agent, Provider, ProviderType

print("nagents installed successfully!")
```
