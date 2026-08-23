"""FastAPI entry point for the InfraCost Agent.

Exposes ``POST /agents/infracost/generate``, thinly wrapping ``run_pipeline``
(input validation through output assembly). All business logic lives in
``core/``; this file only translates HTTP in and out.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
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

# Loads GEMINI_API_KEY / OPENROUTER_* / AWS credentials from .env next to this script
# (gitignored, optional); safe here — those vars are read at call time only.
load_dotenv(Path(__file__).resolve().parent / ".env")

# INFO is off by default; without basicConfig the advisors' "an LLM chose ..." log
# lines never appear, hiding whether a request used the LLM or fell back.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class UTF8JSONResponse(JSONResponse):
    """Same as FastAPI's default JSONResponse but declares charset=utf-8 explicitly:
    JSON is UTF-8 by spec, but Windows PowerShell 5.1 defaults to Latin-1 without
    it, mangling enrichment's accented French text; compliant clients pay nothing.
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
    """Run the full pipeline on a repo-analysis payload. 422 if module 1 rejects
    it (body names the failed rule ``error``, ``message``, ``job_id``); 500 if any
    other stage fails (body names the ``stage``)."""
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
