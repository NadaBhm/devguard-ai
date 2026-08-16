"""
Chat API - wired to the orchestrator's chat module (T-3.10 / T-3.11).

FIXED (2026-08-09): this router used to call lib.rag.retrieval.ask_repo()
directly, bypassing src/agents/orchestrator/chat.py: no conversation memory,
no job context, and `sources` was always [] because ask_repo never returns a
tuple.

Now it loads the persisted orchestrator state for the job and calls the
orchestrator's chat(), which combines that job context with Nada's RAG
retrieval and this job's conversation history.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pathlib import Path
from sqlalchemy.orm import Session

from .. import auth, models
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


def _load_job_state(job_id: str, db: Session, user_id: str) -> dict | None:
    """Return the persisted AnalysisRun.run_metadata, scoped to the user.

    Returns None if the job doesn't exist (or isn't the user's) - chat()
    falls back to RAG-only answers then.
    """
    run = (
        db.query(models.AnalysisRun)
        .filter(
            models.AnalysisRun.id == job_id,
            models.AnalysisRun.triggered_by == user_id,
        )
        .first()
    )
    if not run or not run.run_metadata:
        return None
    return run.run_metadata


@router.post("/ask", response_model=ChatResponse)
async def ask_question(
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        from src.agents.orchestrator.chat import chat as orchestrator_chat

        state = _load_job_state(req.job_id, db, current_user.id)
        result = orchestrator_chat(req.job_id, req.query, state)

        return ChatResponse(
            answer=result["answer"],
            sources=[],
            used_rag=result["used_rag"],
            used_job_context=result["used_job_context"],
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ingest")
async def ingest_repository(
    req: IngestRequest,
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        from ...lib.rag.ingestion import ingest_repo
        count = ingest_repo(Path(req.repo_path), req.job_id)
        return {"status": "success", "chunks_ingested": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
