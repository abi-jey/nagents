"""Native OpenAI provider with public API and ChatGPT subscription modes.

The subscription transport and local Codex configuration discovery remain in
``codex.openai``. Import the provider here (or from ``nagents.provider``);
``codex`` is an implementation detail, not a separate provider identity.
"""

from .codex.openai import DEFAULT_CODEX_MODEL
from .codex.openai import CodexCredentials
from .codex.openai import OpenAIProvider

__all__ = ["DEFAULT_CODEX_MODEL", "CodexCredentials", "OpenAIProvider"]
