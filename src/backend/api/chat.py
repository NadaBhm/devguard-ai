"""
Chat API - wired to the orchestrator's chat module.

Loads the persisted job state and calls orchestrator.chat(), which combines
pipeline results with RAG retrieval and conversation history.
"""
from typing import Optional, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pathlib import Path
from sqlalchemy.orm import Session

from src.agents.orchestrator.state import OrchestratorState

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


def _load_job_state(job_id: str, db: Session, user_id: str) -> Optional[OrchestratorState]:
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
    if run is None or run.run_metadata is None:
        return None
    return cast(OrchestratorState, run.run_metadata)


@router.post("/ask", response_model=ChatResponse)
async def ask_question(
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    try:
        from src.agents.orchestrator.chat import chat as orchestrator_chat

        state = _load_job_state(req.job_id, db, str(current_user.id))
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
