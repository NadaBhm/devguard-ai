"""FastAPI entry point for the InfraCost Agent.

Exposes ``POST /agents/infracost/generate``, thinly wrapping module 9's
``run_pipeline`` (input validation through output assembly, with fallback
LLM enrichment if ``GEMINI_API_KEY`` is unset). All business logic lives in
``core/``; this file only translates HTTP in and out.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from core.input_validator import (
    InputValidationError,
    InvalidStatusError,
    LowConfidenceError,
    MalformedInputError,
    MissingStackDetectionError,
)
from core.pipeline import PipelineStageError, run_pipeline
from models.output_schema import InfraCostOutput

# INFO is off by default in Python (root logger starts at WARNING); without
# this, core.llm_architecture_advisor's and core.llm_deployment_advisor's
# "an LLM chose ..." log lines would silently never appear anywhere,
# leaving no way to see -- while the server runs -- whether a request
# actually used the LLM or fell back to the deterministic path.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class UTF8JSONResponse(JSONResponse):
    """Same as FastAPI's default JSONResponse, but declares ``charset=utf-8``
    explicitly in Content-Type. JSON is UTF-8 by spec (RFC 8259) regardless,
    and most clients assume that correctly — but Windows PowerShell 5.1's
    ``Invoke-RestMethod``/``Invoke-WebRequest`` defaults to Latin-1 when no
    charset is stated, mangling every accented character in ``enrichment``'s
    French text. Being explicit costs nothing for compliant clients and
    fixes this one for free.
    """

    media_type = "application/json; charset=utf-8"


app = FastAPI(title="InfraCost Agent", default_response_class=UTF8JSONResponse)

_ERROR_CODES: dict[type[InputValidationError], str] = {
    InvalidStatusError: "invalid_status",
    MissingStackDetectionError: "missing_stack_detection",
    MalformedInputError: "malformed_input",
    LowConfidenceError: "low_confidence",
}


@app.post("/agents/infracost/generate", response_model=InfraCostOutput)
def generate(raw: dict[str, Any] = Body(...)) -> InfraCostOutput:
    """Run the full InfraCost pipeline on a repo analysis payload.

    Raises:
        HTTPException: 422, if module 1's validation rejects the payload —
            the body names which rule failed (``error``), why
            (``message``), and which job (``job_id``). 500, if any other
            stage fails — the body names exactly which one (``stage``).
    """
    try:
        return run_pipeline(raw)
    except InputValidationError as exc:
        error_code = _ERROR_CODES.get(type(exc), "invalid_input")
        raise HTTPException(
            status_code=422,
            detail={"error": error_code, "message": str(exc), "job_id": exc.job_id},
        ) from exc
    except PipelineStageError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "pipeline_stage_failed", "stage": exc.stage, "message": str(exc)},
        ) from exc
