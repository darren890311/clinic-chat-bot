"""Language model providers.

Importing this package registers every adapter. Swapping vendors is an
environment variable, not a code change.
"""

from app.providers.llm import anthropic_provider, openai_provider  # noqa: F401
from app.providers.llm.base import (
    Completion,
    LLMError,
    LLMProvider,
    Message,
    ToolCall,
    ToolDefinition,
    Usage,
    get_provider,
    register,
    registered_providers,
)

__all__ = [
    "Completion",
    "LLMError",
    "LLMProvider",
    "Message",
    "ToolCall",
    "ToolDefinition",
    "Usage",
    "get_provider",
    "register",
    "registered_providers",
]
