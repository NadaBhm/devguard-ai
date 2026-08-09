"""
Chat API - wired to the orchestrator's chat module (T-3.10 / T-3.11).

FIXED (2026-08-09): this router used to call lib.rag.retrieval.ask_repo()
directly, completely bypassing src/agents/orchestrator/chat.py. That meant:
  - No conversation memory (T-3.11): every question started from scratch.
  - No job context (T-3.10 / US-2.2.5): the LLM never saw the security
    score, cost estimate, or deployment status - only repo excerpts.
  - The isinstance(result, tuple) branch below was also always False
    (ask_repo returns a plain string, never a tuple), so `sources` was
    always [] regardless of what the RAG actually retrieved.

Now this router:
  1. Loads the persisted orchestrator state for the job (same run_metadata
     jobs.py writes via persistence.update_run_state), so the chat has the
     same job context (security score, cost, deployment status) the
     dashboard would show.
  2. Calls the orchestrator's chat() function, which combines that context
     with Nada's RAG retrieval and this job's conversation history.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pathlib import Path
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    query: str
    job_id: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[str]
    used_rag: bool
    used_job_context: bool


class IngestRequest(BaseModel):
    repo_path: str
    job_id: str


def _load_job_state(job_id: str, db: Session) -> dict | None:
    """
    Reconstruct the orchestrator state chat() needs (codesec_result,
    infracost_result, deployops_result, status) from the persisted
    AnalysisRun row. Returns None if the job doesn't exist yet - chat()
    handles that fine (falls back to RAG-only answers).
    """
    run = db.query(models.AnalysisRun).filter(models.AnalysisRun.id == job_id).first()
    if not run or not run.run_metadata:
        return None
    return run.run_metadata


@router.post("/ask", response_model=ChatResponse)
async def ask_question(req: ChatRequest, db: Session = Depends(get_db)):
    """Ask a question about an analyzed repository, using job context + RAG + memory."""
    try:
        from src.agents.orchestrator.chat import chat as orchestrator_chat

        state = _load_job_state(req.job_id, db)
        result = orchestrator_chat(req.job_id, req.query, state)

        return ChatResponse(
            answer=result["answer"],
            sources=[],  # TODO: have orchestrator.chat surface retrieved chunk paths here
            used_rag=result["used_rag"],
            used_job_context=result["used_job_context"],
        )
    except ValueError as e:
        # e.g. empty message - a client error, not a server error.
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ingest")
async def ingest_repository(req: IngestRequest):
    """Ingest a repository into the RAG vector store."""
    try:
        from ...lib.rag.ingestion import ingest_repo
        count = ingest_repo(Path(req.repo_path), req.job_id)
        return {"status": "success", "chunks_ingested": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
