"""
Tool registration with automatic schema extraction.
"""

import inspect
import logging
import re
from collections.abc import Callable
from typing import Any
from typing import Union
from typing import get_args
from typing import get_origin
from typing import get_type_hints

from ..types import ToolDefinition

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    Registry for callable tools with automatic schema extraction.

    Automatically extracts JSON Schema from function type hints and docstrings.

    Example:
        registry = ToolRegistry()

        def get_weather(city: str, unit: str = "celsius") -> str:
            '''Get the current weather for a city.'''
            return f"Weather in {city}: 20{unit[0].upper()}"

        registry.register(get_weather)
        tools = registry.get_all()
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        func: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
    ) -> ToolDefinition:
        """
        Register a function as a tool.

        Args:
            func: The function to register
            name: Optional override for tool name (defaults to function name)
            description: Optional override for description (defaults to docstring)

        Returns:
            The created ToolDefinition
        """
        tool_name = name or func.__name__

        # Extract schema from type hints
        parameters = self._extract_parameters(func)

        # Get description from docstring or override
        tool_description = description or func.__doc__ or f"Call {tool_name}"
        # Clean up docstring - take first line/paragraph
        tool_description = tool_description.strip().split("\n\n")[0].strip()

        tool_def = ToolDefinition(
            name=tool_name,
            description=tool_description,
            parameters=parameters,
            func=func,
        )

        self._tools[tool_name] = tool_def
        logger.debug(f"Registered tool: {tool_name}")
        return tool_def

    def _extract_parameters(self, func: Callable[..., Any]) -> dict[str, Any]:
        """
        Extract JSON Schema from function type hints.

        Args:
            func: The function to extract parameters from

        Returns:
            JSON Schema dict for function parameters
        """
        try:
            hints = get_type_hints(func)
        except Exception:
            hints = {}

        sig = inspect.signature(func)

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue

            param_type = hints.get(param_name, str)
            properties[param_name] = self._type_to_schema(param_type)

            # Add description from docstring if available
            param_doc = self._extract_param_doc(func, param_name)
            if param_doc:
                properties[param_name]["description"] = param_doc

            # Required if no default value
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }

        if required:
            schema["required"] = required

        return schema

    def _type_to_schema(self, t: Any) -> dict[str, Any]:
        """
        Convert Python type to JSON Schema.

        Args:
            t: Python type annotation

        Returns:
            JSON Schema dict
        """
        origin = get_origin(t)
        args = get_args(t)

        # Handle Optional[X] -> X | None
        if origin is Union:
            # Filter out NoneType
            non_none_args = [a for a in args if a is not type(None)]
            if len(non_none_args) == 1:
                return self._type_to_schema(non_none_args[0])
            # Union of multiple types - use first one
            if non_none_args:
                return self._type_to_schema(non_none_args[0])

        # Handle list[X]
        if origin is list:
            item_type = args[0] if args else str
            return {
                "type": "array",
                "items": self._type_to_schema(item_type),
            }

        # Handle dict[K, V]
        if origin is dict:
            return {"type": "object"}

        # Basic types
        type_mapping = {
            str: {"type": "string"},
            int: {"type": "integer"},
            float: {"type": "number"},
            bool: {"type": "boolean"},
            type(None): {"type": "null"},
        }

        if t in type_mapping:
            return type_mapping[t]

        # Default to string
        return {"type": "string"}

    def _extract_param_doc(self, func: Callable[..., Any], param_name: str) -> str | None:
        """
        Extract parameter description from function docstring.

        Supports Google-style and Sphinx-style docstrings.

        Args:
            func: The function
            param_name: Name of the parameter

        Returns:
            Parameter description if found
        """
        if not func.__doc__:
            return None

        doc = func.__doc__

        # Try Google-style: "param_name: description" or "param_name (type): description"
        patterns = [
            rf"{param_name}\s*(?:\([^)]+\))?\s*:\s*(.+?)(?:\n|$)",  # Google style
            rf":param\s+{param_name}\s*:\s*(.+?)(?:\n|$)",  # Sphinx style
        ]

        for pattern in patterns:
            match = re.search(pattern, doc, re.IGNORECASE)
            if match:
                return match.group(1).strip()

        return None

    def get(self, name: str) -> ToolDefinition | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_all(self) -> list[ToolDefinition]:
        """Get all registered tools."""
        return list(self._tools.values())

    def has_tools(self) -> bool:
        """Check if any tools are registered."""
        return len(self._tools) > 0

    def names(self) -> list[str]:
        """Get all registered tool names."""
        return list(self._tools.keys())
