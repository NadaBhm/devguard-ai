from fastapi import APIRouter

router = APIRouter(prefix="/jobs", tags=["jobs"])

@router.get("/")
async def list_jobs():
    return {"jobs": []}