from fastapi import APIRouter

router = APIRouter(prefix="/results", tags=["results"])

@router.get("/")
async def list_results():
    return {"results": []}