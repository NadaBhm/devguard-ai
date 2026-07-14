"""Typed exception hierarchy for the InfraCost agent."""


class InfraCostAgentError(Exception):
    """Base exception for all InfraCost agent errors."""


class InputValidationError(InfraCostAgentError):
    """Raised when the input payload from the repo-analysis agent fails validation."""


class JobNotCompletedError(InputValidationError):
    """Raised when the upstream job status is not 'completed'."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"Cannot process job: status is '{status}', expected 'completed'")


class MissingStackDetectionError(InputValidationError):
    """Raised when the required 'stack_detection' field is absent."""

    def __init__(self) -> None:
        super().__init__("Input payload is missing required 'stack_detection' field")


class LowConfidenceStackDetectionError(InputValidationError):
    """Raised when stack detection confidence is below the minimum threshold."""

    def __init__(self, confidence: float, threshold: float = 0.5) -> None:
        self.confidence = confidence
        self.threshold = threshold
        super().__init__(
            f"Stack detection confidence {confidence:.2f} is below the minimum "
            f"threshold {threshold:.2f}"
        )


class PricingDataError(InfraCostAgentError):
    """Raised when data/aws_pricing.json is missing a key cost_estimator needs.

    Never guessed or extrapolated — a missing price must fail loudly rather
    than silently produce a wrong estimate.
    """

    def __init__(self, missing_key_path: str) -> None:
        self.missing_key_path = missing_key_path
        super().__init__(
            f"Missing required pricing data at '{missing_key_path}' in "
            f"data/aws_pricing.json"
        )


class PipelineStageError(InfraCostAgentError):
    """Raised by pipeline.py to identify which stage failed, without masking the cause."""

    def __init__(self, stage: str, original_error: Exception) -> None:
        self.stage = stage
        self.original_error = original_error
        super().__init__(f"Pipeline failed at stage '{stage}': {original_error}")
