"""Custom exceptions for DevGuard AI."""


class DevGuardError(Exception):
    """Base exception."""
    pass


class GeminiAPIError(DevGuardError):
    """Gemini API call failed."""
    pass


class StructuredOutputError(DevGuardError):
    """Failed to parse structured JSON from LLM."""
    pass


class AgentExecutionError(DevGuardError):
    """Agent execution failed."""
    pass