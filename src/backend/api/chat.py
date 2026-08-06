from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path

router = APIRouter(prefix="/chat", tags=["chat"])

class ChatRequest(BaseModel):
    query: str
    job_id: str

class ChatResponse(BaseModel):
    answer: str
    sources: list[str]

class IngestRequest(BaseModel):
    repo_path: str
    job_id: str

@router.post("/ask", response_model=ChatResponse)
async def ask_question(req: ChatRequest):
    """Ask a question about an ingested repository."""
    try:
        from ...lib.rag.retrieval import ask_repo
        result = ask_repo(req.query, req.job_id)
        
        # Handle both (answer, sources) and just answer
        if isinstance(result, tuple):
            answer, sources = result
            if isinstance(sources, str):
                sources = [sources]  # Wrap string in list
        else:
            answer = result
            sources = []
            
        return ChatResponse(answer=answer, sources=sources)
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