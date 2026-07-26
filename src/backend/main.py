from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .database import init_db
from .api import auth, chat
from .websocket import websocket_endpoint
from fastapi import WebSocket

# Try to import jobs and results, but don't fail if they're empty
try:
    from .api import jobs
    jobs_router = jobs.router
except (AttributeError, ImportError):
    jobs_router = None

try:
    from .api import results
    results_router = results.router
except (AttributeError, ImportError):
    results_router = None

app = FastAPI(title="DevGuard AI", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    # init_db()  # Commented out for local testing
    pass  

# REST API routes
app.include_router(auth.router, prefix="/api")
app.include_router(chat.router, prefix="/api")

if jobs_router:
    app.include_router(jobs_router, prefix="/api")
if results_router:
    app.include_router(results_router, prefix="/api")

# WebSocket endpoint for real-time progress + RAG chat
@app.websocket("/ws/jobs/{job_id}")
async def ws_jobs(websocket: WebSocket, job_id: str):
    await websocket_endpoint(websocket, job_id)

@app.get("/")
def root():
    return {"message": "DevGuard AI API"}

@app.get("/health")
def health():
    return {"status": "healthy"}